"""Post-BYOK feature tests: FIO fill, HA, namespace masking, DHCHAP.

Tests run sequentially on disjoint random subsystems so HA failover,
host ACLs, and DHCHAP keys do not collide. A summary of targets and
results is logged at the end.
"""

import json
import random
import time

from ceph.ceph import CommandFailed
from ceph.parallel import parallel
from tests.nvmeof.workflows.ha import HighAvailability
from tests.nvmeof.workflows.initiator import NVMeInitiator
from utility.log import Log
from utility.utils import run_fio

LOG = Log(__name__)

DEFAULT_LISTENER_PORT = 4420


def _client_nodes(ceph_cluster):
    clients = ceph_cluster.get_nodes(role="client")
    if not clients:
        raise ValueError("feature tests require at least one client node")
    return clients


def _ns_map(gateway, nqn):
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    listed = json.loads(out).get("namespaces", []) if out else []
    return {
        ns.get("rbd_image_name"): ns
        for ns in listed
        if ns.get("rbd_image_name")
    }


def _parent_images(sub):
    return [ns["image"] for ns in sub.get("namespaces", [])]


def _pick_subs(subsystems, count, rng):
    if count > len(subsystems):
        raise ValueError(f"Need {count} subsystems, have {len(subsystems)}")
    return rng.sample(list(subsystems), count)


def _allow_host(gateway, nqn, host):
    try:
        gateway.host.add(**{"args": {"subsystem": nqn, "host": host}})
    except CommandFailed as exc:
        if "already" not in str(exc).lower():
            raise


def _del_host(gateway, nqn, host):
    try:
        gateway.host.delete(**{"args": {"subsystem": nqn, "host": host}})
    except CommandFailed as exc:
        LOG.warning("host del %s on %s: %s", host, nqn, exc)


def _connect(initiator, gateway, nqn, port, auth_mode="", host_key=None, subsys_key=None):
    initiator.disconnect_all()
    initiator.auth_mode = auth_mode
    initiator.host_key = host_key
    initiator.subsys_key = subsys_key
    initiator.connect_targets(
        gateway,
        {"nqn": nqn, "listener_port": port},
    )


def _nvme_nsids(node):
    """Return NSIDs visible on a client from ``nvme list -o json``."""
    out, _ = node.exec_command(cmd="nvme list --output-format=json", sudo=True)
    try:
        devices = json.loads(out).get("Devices") or []
    except (json.JSONDecodeError, TypeError):
        return set()
    nsids = set()
    for device in devices:
        if device.get("NameSpace") is not None:
            nsids.add(int(device["NameSpace"]))
            continue
        for subsys in device.get("Subsystems") or []:
            for ns in subsys.get("Namespaces") or []:
                if ns.get("NSID") is not None:
                    nsids.add(int(ns["NSID"]))
    return nsids


def _run_fio_paths(client, paths, size, test_name, runtime=None):
    if not paths:
        raise RuntimeError(f"No NVMe devices on {client.hostname} for {test_name}")
    io_args = {
        "size": size,
        "client_node": client,
        "long_running": True,
        "cmd_timeout": "notimeout",
        "test_name": test_name,
    }
    if runtime:
        io_args["runtime"] = runtime
    errors = []
    with parallel() as p:
        for path in paths:
            p.spawn(
                run_fio,
                **{**io_args, "device_name": path},
            )
        for op in p:
            if isinstance(op, int) and op != 0:
                errors.append(op)
    if errors:
        raise RuntimeError(f"{test_name} FIO failed with codes {errors}")


def _devices_for_nqn(initiator):
    return initiator.list_devices()


def test_fio_fill(gateway, clients, picked, config, port):
    """Fill 50% of selected parent namespaces via initiator FIO."""
    client = clients[0]
    initiator = NVMeInitiator(client)
    fill = str(config.get("fio_fill_percent", 50)) + "%"
    nqns = []
    images = []
    try:
        initiator.disconnect_all()
        for sub, image_names in picked:
            nqn = sub["group_nqn"]
            nqns.append(nqn)
            images.extend(image_names)
            _allow_host(gateway, nqn, repr("*"))
            initiator.connect_targets(
                gateway, {"nqn": nqn, "listener_port": port}
            )
        paths = _devices_for_nqn(initiator)
        sample = paths[: max(1, len(images))]
        LOG.info(
            "FIO fill %s on %s devices from %s",
            fill,
            len(sample),
            nqns,
        )
        _run_fio_paths(client, sample, fill, "byok-fio-fill-50pct")
        return {
            "name": "fio_fill_50pct",
            "subsystems": nqns,
            "namespaces": images,
            "initiators": [client.hostname],
            "result": "PASS",
            "detail": f"FIO size={fill} on {len(sample)} devices",
        }
    finally:
        initiator.disconnect_all()


def test_namespace_masking(gateway, clients, picked, port):
    """Hide selected NS, allow each to one initiator, verify visibility and IO."""
    if len(clients) < 2:
        raise RuntimeError("namespace masking needs two client nodes")
    client_a, client_b = clients[0], clients[1]
    init_a = NVMeInitiator(client_a)
    init_b = NVMeInitiator(client_b)
    host_a = init_a.initiator_nqn()
    host_b = init_b.initiator_nqn()
    assigned = {client_a.hostname: [], client_b.hostname: []}
    nqns = []
    try:
        for sub, image_names in picked:
            nqn = sub["group_nqn"]
            nqns.append(nqn)
            _del_host(gateway, nqn, repr("*"))
            _allow_host(gateway, nqn, host_a)
            _allow_host(gateway, nqn, host_b)
            ns_map = _ns_map(gateway, nqn)
            half = max(1, len(image_names) // 2)
            for i, image in enumerate(image_names):
                ns_obj = ns_map.get(image)
                if not ns_obj:
                    LOG.warning("Skipping missing image %s on %s", image, nqn)
                    continue
                nsid = ns_obj["nsid"]
                gateway.namespace.change_visibility(
                    **{
                        "args": {
                            "nsid": nsid,
                            "subsystem": nqn,
                            "auto-visible": "no",
                            "force": True,
                        }
                    }
                )
                host_nqn = host_a if i < half else host_b
                owner = client_a.hostname if i < half else client_b.hostname
                gateway.namespace.add_host(
                    **{
                        "args": {
                            "nsid": nsid,
                            "subsystem": nqn,
                            "host": host_nqn,
                            "force": True,
                        }
                    }
                )
                assigned[owner].append(f"{nqn}:nsid={nsid}:{image}")

        init_a.disconnect_all()
        init_b.disconnect_all()
        for nqn in nqns:
            init_a.connect_targets(gateway, {"nqn": nqn, "listener_port": port})
        for nqn in nqns:
            init_b.connect_targets(gateway, {"nqn": nqn, "listener_port": port})

        nsids_a = _nvme_nsids(client_a)
        nsids_b = _nvme_nsids(client_b)
        LOG.info("Masking visible NSIDs client_a=%s client_b=%s", nsids_a, nsids_b)
        if not nsids_a and not nsids_b:
            raise RuntimeError("Neither initiator saw masked namespaces")
        paths_a = init_a.list_devices()
        paths_b = init_b.list_devices()
        if paths_a:
            _run_fio_paths(client_a, paths_a[:2], "100M", "byok-masking-a", runtime=15)
        if paths_b:
            _run_fio_paths(client_b, paths_b[:2], "100M", "byok-masking-b", runtime=15)
        return {
            "name": "namespace_masking",
            "subsystems": nqns,
            "namespaces": assigned,
            "initiators": [client_a.hostname, client_b.hostname],
            "result": "PASS",
            "detail": f"client_a nsids={sorted(nsids_a)} client_b nsids={sorted(nsids_b)}",
        }
    finally:
        init_a.disconnect_all()
        init_b.disconnect_all()


def test_dhchap(
    gateway, clients, sub, passphrases_by_node, config, port, pool
):
    """Enable DHCHAP on one subsystem, add namespaces, connect with auth, run IO."""
    from tests.nvmeof.workflows.byok_kmip import short_hostname

    client = clients[0]
    initiator = NVMeInitiator(client)
    nqn = sub["group_nqn"]
    host_nqn = initiator.initiator_nqn()
    extra_images = []
    try:
        _del_host(gateway, nqn, repr("*"))
        key, _ = initiator.gen_dhchap_key(n=nqn)
        key = str(key).strip()
        gateway.subsystem.change_key(
            **{"args": {"subsystem": nqn, "dhchap-key": key}}
        )
        _allow_host(gateway, nqn, host_nqn)
        try:
            gateway.host.change_key(
                **{
                    "args": {
                        "subsystem": nqn,
                        "host": host_nqn,
                        "dhchap-key": key,
                    }
                }
            )
        except CommandFailed:
            gateway.host.add(
                **{
                    "args": {
                        "subsystem": nqn,
                        "host": host_nqn,
                        "dhchap-key": key,
                    }
                }
            )

        keys = None
        kmip_node = sub.get("kmip_node")
        if passphrases_by_node and kmip_node:
            keys = passphrases_by_node.get(kmip_node)
            if not keys:
                host = short_hostname(kmip_node)
                for node, mapped in passphrases_by_node.items():
                    if short_hostname(node) == host:
                        keys = mapped
                        break
        extra = int(config.get("dhchap_extra_ns", 2))
        size = config.get("image_size", "50G")
        parent_key = keys["parent_luks1"] if keys else None
        for idx in range(1, extra + 1):
            image = f"byok_c{sub['num']:02d}_dhchap_{idx}"
            extra_images.append(image)
            add_args = {
                "nqn": nqn,
                "rbd_pool": pool,
                "rbd_image_name": image,
                "size": size,
                "rbd-create-image": True,
            }
            if parent_key:
                add_args["encryption-format"] = "luks1"
                add_args["key-id"] = parent_key["uuid"]
            gateway.namespace.add(**{"args": add_args})

        _connect(
            initiator,
            gateway,
            nqn,
            port,
            auth_mode="unidirectional",
            host_key=key,
        )
        paths = _devices_for_nqn(initiator)
        _run_fio_paths(client, paths[:2], "100M", "byok-dhchap", runtime=15)
        return {
            "name": "dhchap",
            "subsystems": [nqn],
            "namespaces": extra_images + _parent_images(sub)[:2],
            "initiators": [client.hostname],
            "result": "PASS",
            "detail": f"unidirectional DHCHAP, extra images={extra_images}",
        }
    finally:
        initiator.disconnect_all()


def test_ha_with_io(nvme_service, gateway, clients, picked, config, port):
    """Run FIO on selected subsystems while failing one gateway."""
    client = clients[0]
    initiator = NVMeInitiator(client)
    nqns = [sub["group_nqn"] for sub, _ in picked]
    images = [img for _, names in picked for img in names]
    fail_gw = (
        nvme_service.gateways[1]
        if len(nvme_service.gateways) > 1
        else nvme_service.gateways[0]
    )
    ha = HighAvailability(
        nvme_service.ceph_cluster,
        config.get("gw_nodes") or [],
        **{
            "rbd_pool": config.get("rbd_pool", "rbd"),
            "gw_group": config.get("gw_group", ""),
            "nvme_service": nvme_service,
            "initiators": [],
            "fault-injection-methods": [],
        },
    )
    ha.gateways = nvme_service.gateways
    stopped = False
    try:
        for nqn in nqns:
            _allow_host(gateway, nqn, repr("*"))
        initiator.disconnect_all()
        for nqn in nqns:
            initiator.connect_targets(
                gateway, {"nqn": nqn, "listener_port": port}
            )
        paths = _devices_for_nqn(initiator)[:4]
        LOG.info(
            "HA: FIO then stop gateway %s unit %s",
            fail_gw.hostname,
            fail_gw.system_unit_id,
        )
        # Kick IO, then fail a peer gateway while it is in flight.
        with parallel() as p:
            p.spawn(
                _run_fio_paths,
                client,
                paths,
                "10%",
                "byok-ha-io",
                90,
            )
            time.sleep(15)
            LOG.info("Stopping NVMeoF on %s", fail_gw.hostname)
            ha.system_control(fail_gw, "stop", wait_for_active_state=False)
            stopped = True
            time.sleep(20)
            LOG.info("Starting NVMeoF on %s", fail_gw.hostname)
            ha.system_control(fail_gw, "start", wait_for_active_state=True)
            stopped = False
        return {
            "name": "ha_during_io",
            "subsystems": nqns,
            "namespaces": images,
            "initiators": [client.hostname],
            "result": "PASS",
            "detail": f"systemctl stop/start on {fail_gw.hostname} while FIO ran",
        }
    finally:
        if stopped:
            try:
                ha.system_control(fail_gw, "start", wait_for_active_state=True)
            except Exception as exc:
                LOG.warning("HA cleanup start failed: %s", exc)
        initiator.disconnect_all()


def run_feature_tests(
    nvme_service,
    gateway,
    subsystems,
    ceph_cluster,
    config,
    passphrases_by_node=None,
):
    """Run FIO fill, masking, DHCHAP, then HA. Return list of result dicts."""
    ft = config.get("feature_test") or {}
    seed = ft.get("seed", random.randint(1, 10**9))
    rng = random.Random(seed)
    LOG.info("BYOK feature tests RNG seed=%s (sequential; disjoint subsystems)", seed)

    clients = _client_nodes(ceph_cluster)
    port = config.get("listener_port", DEFAULT_LISTENER_PORT)
    pool = config.get("rbd_pool", "rbd")

    remaining = list(subsystems)
    fio_count = int(ft.get("fio_subsystems", 2))
    mask_count = int(ft.get("masking_subsystems", 2))
    dhchap_count = 1
    ha_count = int(ft.get("ha_subsystems", 2))
    ns_each = int(ft.get("ns_per_subsystem", 4))

    fio_subs = _pick_subs(remaining, fio_count, rng)
    remaining = [s for s in remaining if s not in fio_subs]
    mask_subs = _pick_subs(remaining, mask_count, rng)
    remaining = [s for s in remaining if s not in mask_subs]
    dhchap_sub = _pick_subs(remaining, dhchap_count, rng)[0]
    remaining = [s for s in remaining if s is not dhchap_sub]
    ha_subs = _pick_subs(remaining, ha_count, rng)

    def _with_ns(subs):
        picked = []
        for sub in subs:
            names = _parent_images(sub)
            if not names:
                ns_map = _ns_map(gateway, sub["group_nqn"])
                names = [name for name in ns_map if not str(name).endswith("_clone")]
            chosen = names[:ns_each] if names else []
            picked.append((sub, chosen))
        return picked

    results = []

    def _run(name, fn):
        LOG.info("========== feature test: %s ==========", name)
        try:
            results.append(fn())
        except Exception as exc:
            LOG.exception("%s failed: %s", name, exc)
            results.append(
                {
                    "name": name,
                    "subsystems": [],
                    "namespaces": [],
                    "initiators": [],
                    "result": "FAIL",
                    "detail": str(exc),
                }
            )

    _run(
        "fio_fill_50pct",
        lambda: test_fio_fill(
            gateway, clients, _with_ns(fio_subs), ft, port
        ),
    )
    _run(
        "namespace_masking",
        lambda: test_namespace_masking(
            gateway, clients, _with_ns(mask_subs), port
        ),
    )
    _run(
        "dhchap",
        lambda: test_dhchap(
            gateway,
            clients,
            dhchap_sub,
            passphrases_by_node,
            {**config, **ft},
            port,
            pool,
        ),
    )
    _run(
        "ha_during_io",
        lambda: test_ha_with_io(
            nvme_service, gateway, clients, _with_ns(ha_subs), config, port
        ),
    )

    _log_summary(results, seed)
    failed = [item for item in results if item["result"] != "PASS"]
    if failed:
        names = [item["name"] for item in failed]
        raise RuntimeError(f"Feature tests failed: {names}")
    return results


def _log_summary(results, seed):
    LOG.info("========== BYOK feature test summary (seed=%s) ==========", seed)
    LOG.info(
        "Execution order was sequential: fio_fill -> namespace_masking -> "
        "dhchap -> ha_during_io"
    )
    for item in results:
        LOG.info(
            "  [%s] %s | subsystems=%s | namespaces=%s | initiators=%s | %s",
            item["result"],
            item["name"],
            item.get("subsystems"),
            item.get("namespaces"),
            item.get("initiators"),
            item.get("detail"),
        )
    passed = sum(1 for item in results if item["result"] == "PASS")
    LOG.info(
        "Feature tests: %s/%s passed",
        passed,
        len(results),
    )
