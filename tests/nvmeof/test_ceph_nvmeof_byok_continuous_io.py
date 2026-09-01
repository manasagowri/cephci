"""Fill existing group2 NVMeoF namespaces to 100% and keep FIO running.

Does not create subsystems, namespaces, or KMIP objects. Discovers
namespaces with ``ns list`` only (parents and clones). Applies
per-namespace host ACLs so each client owns a disjoint set of usable
namespaces, verifies masking, then fills them and runs time-based FIO
until ``io_runtime`` expires (or the process is killed).
"""

import json
import shlex
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _existing_subsystems,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log
from utility.utils import run_fio

LOG = Log(__name__)

FILL_JOB = "/tmp/byok_fill.fio"
CONTINUOUS_JOB = "/tmp/byok_continuous.fio"


def _allow_host(gateway, nqn, host):
    try:
        gateway.host.add(**{"args": {"subsystem": nqn, "host": host}})
    except CommandFailed as exc:
        if "already" not in str(exc).lower():
            raise
        LOG.warning("host %s already allowed on %s", host, nqn)


def _del_host(gateway, nqn, host):
    try:
        gateway.host.delete(**{"args": {"subsystem": nqn, "host": host}})
    except CommandFailed as exc:
        LOG.warning("host del %s on %s: %s", host, nqn, exc)


def _list_namespaces(gateway, nqn):
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    return json.loads(out).get("namespaces", []) if out else []


def _host_names(hosts):
    """Normalize namespace list ``hosts`` to a list of host NQNs."""
    if not hosts:
        return []
    if isinstance(hosts, str):
        return [hosts]
    names = []
    for host in hosts:
        if isinstance(host, dict):
            names.append(host.get("nqn") or host.get("host") or host.get("host_nqn"))
        else:
            names.append(str(host))
    return [name for name in names if name]


def _is_clone(ns):
    return str(ns.get("rbd_image_name") or "").endswith("_clone")


def _ns_usable(ns):
    """True when the namespace has a live RBD image the initiator can map."""
    if ns.get("degraded") in (True, "true", "True", 1, "yes"):
        return False
    bdev = str(ns.get("bdev_name") or "")
    if bdev.endswith("_degraded"):
        return False
    if not (ns.get("rbd_image_name") or "").strip():
        return False
    if str(ns.get("rbd_image_size") or "0") in ("0", ""):
        return False
    return True


def _is_auto_visible(ns):
    return ns.get("auto_visible") not in (False, "no", "No", 0, "false", "False")


def _needs_mask(ns, owner_nqn):
    if _is_auto_visible(ns):
        return True
    return set(_host_names(ns.get("hosts"))) != {owner_nqn}


def _norm_uuid(uuid):
    return str(uuid).lower() if uuid else ""


def _gw_connect_error(exc):
    text = str(exc).lower()
    return "failed to connect" in text or "connect to all addresses" in text


def _change_ns_visibility(gateway, nqn, nsid, tries=6, delay=5):
    """Set auto-visible=no, retrying transient gateway connect failures."""
    last_err = None
    for attempt in range(1, tries + 1):
        try:
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
            return
        except CommandFailed as err:
            last_err = err
            if _gw_connect_error(err):
                LOG.warning(
                    "change_visibility nsid=%s %s attempt %s/%s: %s",
                    nsid,
                    nqn,
                    attempt,
                    tries,
                    err,
                )
                time.sleep(delay)
                continue
            raise
    raise last_err


def _add_ns_host(gateway, nqn, nsid, host, tries=6, delay=5):
    """Allow one host on a namespace after it is no longer auto-visible."""
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            gateway.namespace.add_host(
                **{
                    "args": {
                        "nsid": nsid,
                        "subsystem": nqn,
                        "host": host,
                        "force": True,
                    }
                }
            )
            return
        except CommandFailed as err:
            last_err = err
            text = str(err).lower()
            if "already" in text:
                LOG.warning("add_host already set nsid=%s %s", nsid, nqn)
                return
            if "visible to all hosts" in text or _gw_connect_error(err):
                LOG.warning(
                    "add_host nsid=%s %s attempt %s/%s: %s",
                    nsid,
                    nqn,
                    attempt,
                    tries,
                    err,
                )
                if "visible to all hosts" in text:
                    _change_ns_visibility(gateway, nqn, nsid)
                time.sleep(delay)
                continue
            raise
    raise last_err


def _apply_ns_mask(gateway, ns):
    """Hide a namespace and allow only the owner initiator."""
    nqn, nsid, owner = ns["nqn"], ns["nsid"], ns["owner_nqn"]
    _change_ns_visibility(gateway, nqn, nsid)
    for extra in ns.get("current_hosts") or []:
        if extra and extra != owner:
            try:
                gateway.namespace.del_host(
                    **{"args": {"nsid": nsid, "subsystem": nqn, "host": extra}}
                )
            except CommandFailed as err:
                LOG.warning("del_host %s nsid=%s %s: %s", extra, nsid, nqn, err)
    _add_ns_host(gateway, nqn, nsid, owner)


def _configure_subsystem_hosts(gateway, subsystems, host_nqns):
    """Replace allow-any with the two client host NQNs on every subsystem."""
    for sub in subsystems:
        nqn = sub["group_nqn"]
        _del_host(gateway, nqn, repr("*"))
        for host_nqn in host_nqns.values():
            _allow_host(gateway, nqn, host_nqn)


def _ns_record(nqn, ns, owner="", owner_nqn=""):
    uuid = ns.get("uuid")
    if not uuid:
        raise RuntimeError(f"Namespace nsid={ns.get('nsid')} on {nqn} has no uuid")
    return {
        "nqn": nqn,
        "nsid": ns["nsid"],
        "uuid": uuid,
        "image": ns.get("rbd_image_name"),
        "current_hosts": _host_names(ns.get("hosts")),
        "owner": owner,
        "owner_nqn": owner_nqn,
    }


def _collect_usable_namespaces(gateway, subsystems):
    """List live parent and clone namespaces from ``ns list``. Skip degraded."""
    usable = []
    skipped = 0
    clones = 0
    parents = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        listed = _list_namespaces(gateway, nqn)
        if not listed:
            LOG.warning("No namespaces listed on %s", nqn)
            continue
        for ns in listed:
            if not _ns_usable(ns):
                skipped += 1
                continue
            record = _ns_record(nqn, ns)
            record["clone"] = _is_clone(ns)
            if record["clone"]:
                clones += 1
            else:
                parents += 1
            usable.append(record)
    LOG.info(
        "Usable namespaces=%s parents=%s clones=%s skipped_degraded=%s",
        len(usable),
        parents,
        clones,
        skipped,
    )
    return usable


def _assign_namespaces(gateway, subsystems, clients, host_nqns):
    """Round-robin every usable parent and clone namespace to one client."""
    assigned = {client.hostname: [] for client in clients}
    all_ns = _collect_usable_namespaces(gateway, subsystems)
    if not all_ns:
        raise RuntimeError("No usable namespaces found on group2 subsystems")
    for index, ns in enumerate(all_ns):
        client = clients[index % len(clients)]
        ns["owner"] = client.hostname
        ns["owner_nqn"] = host_nqns[client.hostname]
        assigned[client.hostname].append(ns)
    for client in clients:
        owned = assigned[client.hostname]
        LOG.info(
            "Masking assignment: %s owns %s namespaces (%s clones)",
            client.hostname,
            len(owned),
            sum(1 for ns in owned if ns.get("clone")),
        )
    return assigned, all_ns


def _reuse_ns_assignment(gateway, subsystems, clients, host_nqns):
    """Rebuild ownership from existing ACLs. Remask only incomplete NS."""
    assigned = {client.hostname: [] for client in clients}
    to_mask = []
    next_index = 0
    usable = 0
    skipped = 0
    clones = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        listed = _list_namespaces(gateway, nqn)
        if not listed:
            LOG.warning("No namespaces listed on %s", nqn)
            continue
        for raw in listed:
            if not _ns_usable(raw):
                skipped += 1
                continue
            usable += 1
            is_clone = _is_clone(raw)
            if is_clone:
                clones += 1
            hosts = _host_names(raw.get("hosts"))
            matches = [
                hostname
                for hostname, host_nqn in host_nqns.items()
                if host_nqn in hosts
            ]
            if len(matches) == 1 and not _is_auto_visible(raw):
                owner = matches[0]
                record = _ns_record(
                    nqn, raw, owner=owner, owner_nqn=host_nqns[owner]
                )
                record["clone"] = is_clone
                assigned[owner].append(record)
                continue
            owner = (
                matches[0]
                if len(matches) == 1
                else clients[next_index % len(clients)].hostname
            )
            if len(matches) != 1:
                next_index += 1
            ns = _ns_record(
                nqn, raw, owner=owner, owner_nqn=host_nqns[owner]
            )
            ns["clone"] = is_clone
            assigned[owner].append(ns)
            to_mask.append(ns)
    if not usable:
        raise RuntimeError("No usable namespaces found on group2 subsystems")
    LOG.info(
        "Reused namespace ACLs: remask=%s already_masked=%s "
        "clones=%s skipped_degraded=%s",
        len(to_mask),
        usable - len(to_mask),
        clones,
        skipped,
    )
    for client in clients:
        owned = assigned[client.hostname]
        LOG.info(
            "Resume assignment: %s owns %s namespaces (%s clones)",
            client.hostname,
            len(owned),
            sum(1 for ns in owned if ns.get("clone")),
        )
    return assigned, to_mask


def _reconcile_namespace_masking(gateway, subsystems, assigned, clients, host_nqns):
    """Mask usable NS that appeared after assignment or stayed auto-visible."""
    owned = {
        _norm_uuid(ns["uuid"]): ns
        for namespaces in assigned.values()
        for ns in namespaces
    }
    next_index = sum(len(namespaces) for namespaces in assigned.values())
    to_mask = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        for raw in _list_namespaces(gateway, nqn):
            if not _ns_usable(raw):
                continue
            uuid = _norm_uuid(raw.get("uuid"))
            ns = owned.get(uuid)
            if ns is None:
                client = clients[next_index % len(clients)]
                next_index += 1
                ns = _ns_record(
                    nqn,
                    raw,
                    owner=client.hostname,
                    owner_nqn=host_nqns[client.hostname],
                )
                ns["clone"] = _is_clone(raw)
                assigned[client.hostname].append(ns)
                owned[uuid] = ns
                to_mask.append(ns)
                LOG.info(
                    "Late namespace %s nsid=%s uuid=%s clone=%s assigned to %s",
                    nqn,
                    ns["nsid"],
                    ns["uuid"],
                    ns["clone"],
                    client.hostname,
                )
                continue
            ns["nsid"] = raw["nsid"]
            ns["current_hosts"] = _host_names(raw.get("hosts"))
            if _needs_mask(raw, ns["owner_nqn"]):
                to_mask.append(ns)
    if to_mask:
        LOG.info("Re-applying namespace masking to %s namespaces", len(to_mask))
        _apply_namespace_masking(gateway, to_mask)
    return assigned


def _apply_namespace_masking(gateway, all_ns):
    LOG.info("Applying namespace masking to %s namespaces", len(all_ns))
    with parallel(max_workers=2) as p:
        for ns in all_ns:
            p.spawn(_apply_ns_mask, gateway, ns)
        for _ in p:
            pass


def _verify_gateway_masking(gateway, subsystems, assigned):
    """Confirm every usable NS is hidden and allowed only for its owner."""
    owned = {
        _norm_uuid(ns["uuid"]): ns
        for namespaces in assigned.values()
        for ns in namespaces
    }
    errors = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        for actual in _list_namespaces(gateway, nqn):
            if not _ns_usable(actual):
                continue
            uuid = _norm_uuid(actual.get("uuid"))
            ns = owned.get(uuid)
            kind = "clone" if _is_clone(actual) else "parent"
            if not ns:
                errors.append(
                    f"{nqn} nsid={actual.get('nsid')} uuid={uuid} "
                    f"{kind} but not assigned"
                )
                continue
            if _is_auto_visible(actual):
                errors.append(
                    f"{nqn} nsid={actual.get('nsid')} {kind} auto_visible="
                    f"{actual.get('auto_visible')}"
                )
            hosts = set(_host_names(actual.get("hosts")))
            if hosts != {ns["owner_nqn"]}:
                errors.append(
                    f"{nqn} nsid={actual.get('nsid')} {kind} hosts={sorted(hosts)} "
                    f"expected={ns['owner_nqn']}"
                )
    if errors:
        raise RuntimeError(
            "Gateway namespace masking check failed:\n" + "\n".join(errors[:20])
        )
    LOG.info("Gateway namespace masking verified for %s owners", len(assigned))


def _initiator_uuids(initiator):
    return {_norm_uuid(uuid) for uuid in initiator.fetch_lsblk_nvme_devices()}


def _connect_client(initiator, gateways, subsystems, port):
    """Connect every subsystem through every gateway so ANA-optimized NS appear."""
    if not gateways:
        raise RuntimeError("No NVMeoF gateways to connect")
    LOG.info(
        "Connecting %s subsystems via %s gateways",
        len(subsystems),
        len(gateways),
    )
    for gateway in gateways:
        LOG.info(
            "connect-all via %s (%s)",
            gateway.node.hostname,
            gateway.node.ip_address,
        )
        initiator.connect_targets(
            gateway,
            {"nqn": "connect-all", "listener_port": port},
        )


def _paths_for_uuids(initiator, uuids):
    """Return /dev paths whose lsblk WWN matches the given UUID set."""
    wanted = {_norm_uuid(uuid) for uuid in uuids}
    paths = []
    for dev in initiator.fetch_lsblk_nvme_devices_dict():
        wwn = dev.get("wwn") or ""
        if not wwn.startswith("uuid."):
            continue
        uuid = _norm_uuid(wwn.removeprefix("uuid."))
        if uuid not in wanted:
            continue
        name = dev.get("name")
        if not name:
            continue
        paths.append(name if str(name).startswith("/dev/") else f"/dev/{name}")
    return paths


def _verify_initiator_masking(mapped, assigned, tries=4, delay=10):
    """Each client must see only its assigned parent and clone UUIDs."""
    expected = {
        hostname: {_norm_uuid(ns["uuid"]) for ns in namespaces if ns.get("uuid")}
        for hostname, namespaces in assigned.items()
    }
    actual = {}
    last_error = None
    for attempt in range(1, tries + 1):
        actual = {}
        failed = None
        for hostname, (_, initiator, _) in mapped.items():
            uuids = _initiator_uuids(initiator)
            actual[hostname] = uuids
            missing = expected[hostname] - uuids
            extra = uuids - expected[hostname]
            clone_expected = sum(
                1 for ns in assigned[hostname] if ns.get("clone")
            )
            LOG.info(
                "%s visible=%s expected=%s clones_assigned=%s "
                "missing=%s extra=%s (attempt %s/%s)",
                hostname,
                len(uuids),
                len(expected[hostname]),
                clone_expected,
                len(missing),
                len(extra),
                attempt,
                tries,
            )
            if missing or extra:
                failed = (
                    f"Namespace masking failed on {hostname}: "
                    f"missing={sorted(missing)[:8]} extra={sorted(extra)[:8]}"
                )
                last_error = RuntimeError(failed)
                break
        if failed is None:
            break
        if attempt < tries:
            time.sleep(delay)
    else:
        raise last_error or RuntimeError("Initiator namespace masking failed")
    hostnames = list(actual)
    if len(hostnames) >= 2:
        overlap = actual[hostnames[0]] & actual[hostnames[1]]
        if overlap:
            raise RuntimeError(
                f"Clients share namespaces after masking: {sorted(overlap)[:8]}"
            )
    LOG.info("Initiator namespace masking verified: no shared parent or clone namespaces")


def _sample_masking_io(mapped, sample, runtime):
    """Short FIO on a few devices per client to prove masked NS accept IO."""
    errors = []
    with parallel() as p:
        for client, _, paths in mapped.values():
            targets = paths[:sample]
            if not targets:
                raise RuntimeError(f"No devices on {client.hostname} for sample IO")
            LOG.info(
                "Masking sample FIO on %s devices of %s",
                len(targets),
                client.hostname,
            )
            for path in targets:
                p.spawn(
                    run_fio,
                    device_name=path,
                    client_node=client,
                    size="100M",
                    runtime=runtime,
                    long_running=True,
                    cmd_timeout="notimeout",
                    test_name=f"byok-masking-sample-{client.hostname}",
                )
        for result in p:
            if isinstance(result, int) and result != 0:
                errors.append(result)
    if errors:
        raise RuntimeError(f"Masking sample FIO failed with {errors}")
    LOG.info("Namespace masking sample IO passed")


def _size_to_gib(size):
    text = str(size).strip().upper()
    try:
        if text.endswith("T"):
            return float(text[:-1]) * 1024
        if text.endswith("G"):
            return float(text[:-1])
        if text.endswith("M"):
            return float(text[:-1]) / 1024
        return float(text)
    except ValueError:
        return 50.0


def _write_fio_job(node, job_path, devices, global_opts):
    """Write one FIO job file with a section per device."""
    script = (
        "import json\n"
        "from pathlib import Path\n"
        f"devices = {json.dumps(list(devices))}\n"
        f"opts = {json.dumps(global_opts)}\n"
        "lines = ['[global]']\n"
        "for key, value in opts.items():\n"
        "    lines.append(f'{key}={value}')\n"
        "for index, device in enumerate(devices):\n"
        "    lines.append(f'[ns{index}]')\n"
        "    lines.append(f'filename={device}')\n"
        f"Path({json.dumps(job_path)}).write_text('\\n'.join(lines) + '\\n')\n"
    )
    node.exec_command(cmd=f"python3 -c {shlex.quote(script)}", sudo=True)


def _run_fio_job(node, job_path):
    LOG.info("Starting FIO %s on %s", job_path, node.hostname)
    return node.exec_command(
        cmd=f"fio {job_path}",
        sudo=True,
        long_running=True,
        timeout="notimeout",
    )


def _run_fio_all_clients(mapped, job_path):
    """Run the same FIO job file on every client at once."""
    errors = []
    with parallel() as p:
        for client, _, _ in mapped.values():
            p.spawn(_run_fio_job, client, job_path)
        for result in p:
            if isinstance(result, int) and result != 0:
                errors.append(result)
    if errors:
        raise RuntimeError(f"FIO {job_path} failed with {errors}")


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Mask namespaces per client, verify, fill, then keep FIO running.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    try:
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        gateway = nvme_service.gateways[0]
        clients = ceph_cluster.get_nodes(role="client")
        if not clients:
            raise ValueError("continuous IO requires at least one client node")
        if config.get("ns_masking", True) and len(clients) < 2:
            raise ValueError("namespace masking requires two client nodes")

        subsystems = _existing_subsystems(gateway, config)
        port = config.get("listener_port", DEFAULT_LISTENER_PORT)
        fill_pct = str(config.get("fill_percent", 100)).rstrip("%") + "%"
        fill_bs = config.get("fill_bs", "1M")
        iodepth = str(config.get("iodepth", 16))
        io_runtime = int(config.get("io_runtime", 86400))
        continuous_bs = config.get("continuous_bs", "64k")
        continuous_rw = config.get("io_type", "randrw")
        image_size = config.get("image_size", "50G")
        size_gib = _size_to_gib(image_size)
        sample = int(config.get("masking_sample_ns", 2))
        sample_runtime = int(config.get("masking_sample_runtime", 15))

        host_nqns = {}
        initiators = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            initiator.disconnect_all()
            initiators[client.hostname] = initiator
            host_nqns[client.hostname] = initiator.initiator_nqn()
            LOG.info("Client %s host NQN %s", client.hostname, host_nqns[client.hostname])

        resume_from = config.get("resume_from")
        assigned = None
        ns_uuids = {client.hostname: set() for client in clients}
        if config.get("ns_masking", True):
            _configure_subsystem_hosts(gateway, subsystems, host_nqns)
            if resume_from in ("io", "fill"):
                LOG.info(
                    "resume_from=%s: reuse existing host ACLs; "
                    "skip bulk namespace masking",
                    resume_from,
                )
                assigned, to_mask = _reuse_ns_assignment(
                    gateway, subsystems, clients, host_nqns
                )
                if to_mask:
                    _apply_namespace_masking(gateway, to_mask)
            else:
                assigned, all_ns = _assign_namespaces(
                    gateway, subsystems, clients, host_nqns
                )
                _apply_namespace_masking(gateway, all_ns)
                assigned = _reconcile_namespace_masking(
                    gateway, subsystems, assigned, clients, host_nqns
                )
            _verify_gateway_masking(gateway, subsystems, assigned)
            for hostname, namespaces in assigned.items():
                ns_uuids[hostname] = {
                    _norm_uuid(ns["uuid"]) for ns in namespaces if ns.get("uuid")
                }
        else:
            usable = _collect_usable_namespaces(gateway, subsystems)
            all_uuids = {_norm_uuid(ns["uuid"]) for ns in usable}
            for client in clients:
                ns_uuids[client.hostname] = all_uuids
            for sub in subsystems:
                _allow_host(gateway, sub["group_nqn"], repr("*"))

        mapped = {}
        for client in clients:
            initiator = initiators[client.hostname]
            _connect_client(initiator, nvme_service.gateways, subsystems, port)
            time.sleep(5)
            paths = _paths_for_uuids(initiator, ns_uuids[client.hostname])
            mapped[client.hostname] = (client, initiator, paths)
            LOG.info(
                "%s connected via %s gateways, %s namespaces",
                client.hostname,
                len(nvme_service.gateways),
                len(paths),
            )

        if assigned is not None:
            _verify_initiator_masking(mapped, assigned)
            for hostname, (client, initiator, _) in list(mapped.items()):
                mapped[hostname] = (
                    client,
                    initiator,
                    _paths_for_uuids(initiator, ns_uuids[hostname]),
                )
            _sample_masking_io(mapped, sample, sample_runtime)

        total_ns = sum(len(paths) for _, _, paths in mapped.values())
        total_gib = total_ns * size_gib
        low_hours = total_gib / (6 * 3600) if total_gib else 0
        high_hours = total_gib / (3 * 3600) if total_gib else 0
        LOG.info(
            "Fill plan: %s namespaces x %s = %.0f GiB. "
            "Expected fill time about %.1f-%.1f hours at 3-6 GiB/s aggregate.",
            total_ns,
            image_size,
            total_gib,
            low_hours,
            high_hours,
        )

        fill_started = time.time()
        for client, _, paths in mapped.values():
            if not paths:
                raise RuntimeError(f"No NVMe devices on {client.hostname}")
            _write_fio_job(
                client,
                FILL_JOB,
                paths,
                {
                    "ioengine": "libaio",
                    "direct": "1",
                    "bs": fill_bs,
                    "rw": "write",
                    "iodepth": iodepth,
                    "group_reporting": "1",
                    "size": fill_pct,
                    "numjobs": "1",
                },
            )
        _run_fio_all_clients(mapped, FILL_JOB)
        elapsed = time.time() - fill_started
        LOG.info(
            "100%% fill completed in %.1f minutes (%.2f hours) for %s namespaces",
            elapsed / 60,
            elapsed / 3600,
            total_ns,
        )

        if not config.get("continuous", True):
            return 0

        LOG.info(
            "Starting continuous FIO rw=%s bs=%s runtime=%ss on all mapped namespaces",
            continuous_rw,
            continuous_bs,
            io_runtime,
        )
        for client, _, paths in mapped.values():
            _write_fio_job(
                client,
                CONTINUOUS_JOB,
                paths,
                {
                    "ioengine": "libaio",
                    "direct": "1",
                    "bs": continuous_bs,
                    "rw": continuous_rw,
                    "iodepth": iodepth,
                    "group_reporting": "1",
                    "time_based": "1",
                    "runtime": str(io_runtime),
                    "numjobs": "1",
                },
            )
        _run_fio_all_clients(mapped, CONTINUOUS_JOB)
        LOG.info("Continuous FIO completed after %s seconds", io_runtime)
        return 0
    except Exception as err:
        LOG.exception("NVMeoF BYOK continuous IO failed: %s", err)
        return 1
