"""SC-09 CROSS ENC: encrypted namespaces plus DHCHAP at scale.

Creates 32 new group2 subsystems (``crossenc1..32``), attaches existing
KMIP endpoints, adds 8 LUKS2 namespaces each (256 total), and enables
bidirectional DHCHAP with a unique host key per subsystem. Existing
``cnode*`` subsystems and the 512 encrypted parents are left alone.

DHCHAP needs the NVMeoF service encryption PSK, which is separate from
RBD LUKS/KMIP. If group2 was deployed for BYOK without it, the test
copies a PEM to each gateway, applies ``enable_encryption`` +
``encryption_key_path``, ``ceph orch redeploy``s (apply alone does not
bounce daemons), waits for PID change, and recopies KMIP certs before
DHCHAP.

One initiator host NQN is reused with a different DHCHAP secret per
subsystem (16 NQNs per client). Leftover ``crossenc*`` DHCHAP sessions,
subsystems, namespaces, and ``crossenc_*`` RBD images from a prior run
are disconnected and removed from group2 first; ``cnode*`` is left alone.
After write+verify FIO, GW1 is restarted and reconnect + KMIP re-fetch
is timed. Ten subsystems are then probed with a wrong DHCHAP key;
rejection must not take down the other sessions.
"""

import json
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.ceph_admin.common import config_dict_to_string
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _add_kmip_endpoints,
    _assign_kmip_endpoints,
    _bind_actual_nqns,
    _group_nqn,
    _init_rbd,
    _keys_for_node,
    _listed_ns_map,
    _ns_add,
    _rbd_image_names,
    _sorted_kmip_nodes,
    _verify_listeners,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _copy_kmip_certs_when_containers_ready,
    _gw_unit_identity,
    _refresh_gateway_ssh,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import (
    _del_host,
    _norm_uuid,
    _ns_record,
    _paths_for_uuids,
)
from tests.nvmeof.test_ceph_nvmeof_byok_kmip_gw_restart import (
    DEFAULT_FIO_MAX_RUNTIME,
    DEFAULT_NS_REOPEN_TIMEOUT,
    DEFAULT_PID_CHANGE_TIMEOUT,
    FIO_ERR_RE,
    FIO_STOP_EXIT_CODES,
    _expected_ns_snapshot,
    _fio_running,
    _fmt_seconds,
    _missing_expected,
    _scan_and_save_kmip_errors,
    _scan_gateway_namespaces,
    _stop_fio,
    _wait_for_paths,
    _wait_pid_changed,
    _write_light_fio_job,
)
from tests.nvmeof.workflows.byok_kmip import (
    DEFAULT_KMIP_CLI_IMAGE,
    KMIP_PORT,
    load_passphrases_all,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import (
    check_and_set_nvme_cli_image,
    get_network_mask,
)
from utility.log import Log

LOG = Log(__name__)

CROSS_NQN_PREFIX = "nqn.2016-06.io.spdk:crossenc"
CROSS_JOB = "/tmp/kmip_cross_enc.fio"
CROSS_FIO_LOG = "/tmp/kmip_cross_enc.fio.log"
DEFAULT_NS_PER_SUB = 8
DEFAULT_IMAGE_SIZE = "5G"
DEFAULT_SERIAL_BASE = 100
DEFAULT_BAD_KEY_SUBS = 10
RESUME_WRONG_KEY_PROBE = "wrong_key_probe"
DEFAULT_DHCHAP_SETTLE_SEC = 3
DEFAULT_DHCHAP_HOST_ADD_TRIES = 5
ALREADY_CONNECTED = ("already connected", "already exists", "duplicate")
_ALREADY = ("already", "exists", "duplicate")
_HOST_MISSING = (
    "not found",
    "no such",
    "doesn't exist",
    "does not exist",
    "couldn't find",
)
_TRANSPORT_GLITCH = (
    "code -1",
    "exit code:  -1",
    "inferring fsid",
    "sockettimeout",
    "pipe timeout",
    "timed out",
)


def _image_name(sub_num, ns_index):
    return f"crossenc_c{sub_num:02d}_n{ns_index:02d}_luks2"


def _gen_dhchap_key(initiator, nqn):
    out, _ = initiator.gen_dhchap_key(n=nqn)
    key = str(out or "").strip().splitlines()[-1].strip()
    if not key:
        raise RuntimeError(f"Empty DHCHAP key for {nqn}")
    return key


def _add_crossenc_subsystems(gateway, config):
    """Create ``crossenc1..N`` subsystems with auto-listeners. Idempotent."""
    count = int(config.get("subsystems", 32))
    group = config.get("gw_group")
    prefix = config.get("nqn_prefix", CROSS_NQN_PREFIX)
    network_mask = config.get("network_mask")
    serial_base = int(config.get("serial_base", DEFAULT_SERIAL_BASE))
    max_ns = int(config.get("max_namespaces", DEFAULT_NS_PER_SUB * 2))
    created = []

    def _add(num):
        nqn = f"{prefix}{num}"
        group_nqn = _group_nqn(nqn, group)
        args = {
            "nqn": nqn,
            "serial_number": str(serial_base + num),
            "max_namespaces": max_ns,
        }
        if network_mask:
            args["network-mask"] = network_mask
        LOG.info("Adding CROSS ENC subsystem %s serial=%s", nqn, args["serial_number"])
        try:
            gateway.subsystem.add(**{"args": args})
        except CommandFailed as exc:
            if "already" not in str(exc).lower():
                raise
            LOG.info("Subsystem %s already present; reusing it", nqn)
        return {"num": num, "nqn": nqn, "group_nqn": group_nqn}

    with parallel() as p:
        for num in range(1, count + 1):
            p.spawn(_add, num)
        for item in p:
            created.append(item)
    created.sort(key=lambda item: item["num"])
    _bind_actual_nqns(gateway, created)
    return created


def _add_luks2_namespaces(gateway, subsystems, config, passphrases_by_node):
    """Add 8 LUKS2 encrypted namespaces per CROSS ENC subsystem."""
    pool = config.get("rbd_pool", "rbd")
    ns_per_sub = int(config.get("namespaces_per_subsystem", DEFAULT_NS_PER_SUB))
    size = config.get("image_size", DEFAULT_IMAGE_SIZE)
    expected = []

    def _add_one(sub, ns_index, ns_map):
        image = _image_name(sub["num"], ns_index)
        nqn = sub["group_nqn"]
        if image in ns_map:
            LOG.info("%s already present on %s; skipping", image, nqn)
            return {"image": image, "nqn": nqn, "ns_index": ns_index}
        keys = _keys_for_node(passphrases_by_node, sub["kmip_node"])
        key = keys["parent_luks2"]
        LOG.info(
            "ns add %s image=%s format=luks2 key-id=%s",
            nqn,
            image,
            key["uuid"],
        )
        _ns_add(
            gateway,
            {
                "nqn": nqn,
                "rbd_pool": pool,
                "rbd_image_name": image,
                "size": size,
                "rbd-create-image": True,
                "encryption-format": "luks2",
                "key-id": key["uuid"],
            },
        )
        return {"image": image, "nqn": nqn, "ns_index": ns_index}

    for sub in subsystems:
        ns_map = _listed_ns_map(gateway, sub["group_nqn"])
        ns_meta = []
        with parallel() as p:
            for ns_index in range(1, ns_per_sub + 1):
                p.spawn(_add_one, sub, ns_index, ns_map)
            for result in p:
                ns_meta.append(result)
        ns_meta.sort(key=lambda item: item["ns_index"])
        sub["namespaces"] = ns_meta
        expected.extend(ns_meta)
    return expected


def _cli_text(exc):
    return str(exc or "").lower()


def _is_already(exc):
    return any(token in _cli_text(exc) for token in _ALREADY)


def _is_host_missing(exc):
    return any(token in _cli_text(exc) for token in _HOST_MISSING)


def _is_transport_glitch(exc):
    """True when SSH/cephadm dropped with no NVMeoF Failure/EINVAL body."""
    text = _cli_text(exc)
    if any(token in text for token in ("einval", "failure ", "error einval")):
        return False
    return any(token in text for token in _TRANSPORT_GLITCH)


def _host_key_args(nqn, host_nqn, host_key):
    return {
        "args": {
            "subsystem": nqn,
            "host": host_nqn,
            "dhchap-key": host_key,
        }
    }


def _host_change_key(gateway, nqn, host_nqn, host_key):
    gateway.host.change_key(**_host_key_args(nqn, host_nqn, host_key))


def _configure_dhchap(
    gateway,
    nqn,
    host_nqn,
    host_key,
    subsys_key,
    settle_sec=DEFAULT_DHCHAP_SETTLE_SEC,
    tries=DEFAULT_DHCHAP_HOST_ADD_TRIES,
):
    """Bidirectional DHCHAP: subsystem key + unique host key. Idempotent.

    ``host add`` can hang on OMAP lock and drop the installer SSH session
    (exit -1, only cephadm ``Inferring fsid``). Retry, and use
    ``host change_key`` when the add actually committed.
    """
    _del_host(gateway, nqn, repr("*"))
    if settle_sec:
        time.sleep(settle_sec)
    gateway.subsystem.change_key(
        **{"args": {"subsystem": nqn, "dhchap-key": subsys_key}}
    )
    if settle_sec:
        time.sleep(settle_sec)

    last = None
    for attempt in range(1, int(tries) + 1):
        try:
            gateway.host.add(**_host_key_args(nqn, host_nqn, host_key))
            return
        except CommandFailed as exc:
            last = exc
            if _is_already(exc):
                LOG.info(
                    "Host %s already on %s; applying dhchap-key via change_key",
                    host_nqn,
                    nqn,
                )
                _host_change_key(gateway, nqn, host_nqn, host_key)
                return
            if not _is_transport_glitch(exc):
                raise
            LOG.warning(
                "host add %s attempt %s/%s dropped (%s); settling then retry",
                nqn,
                attempt,
                tries,
                exc,
            )
            time.sleep(settle_sec * attempt)
            try:
                _host_change_key(gateway, nqn, host_nqn, host_key)
                LOG.info(
                    "host change_key on %s succeeded after dropped host add",
                    nqn,
                )
                return
            except CommandFailed as ck_exc:
                if attempt >= int(tries):
                    raise
                if _is_host_missing(ck_exc) or _is_transport_glitch(ck_exc):
                    LOG.warning(
                        "change_key after dropped add on %s: %s", nqn, ck_exc
                    )
                    continue
                raise
    raise last


def _assign_clients(subsystems, clients):
    """Split NQNs across clients (16/16 with two clients)."""
    assigned = {client.hostname: [] for client in clients}
    for index, sub in enumerate(subsystems):
        client = clients[index % len(clients)]
        assigned[client.hostname].append(sub)
        sub["owner"] = client.hostname
    for client in clients:
        LOG.info(
            "%s owns %s CROSS ENC subsystems",
            client.hostname,
            len(assigned[client.hostname]),
        )
    return assigned


def _connect_nqn(initiator, gateways, nqn, port, host_key, subsys_key):
    """Connect one NQN on every gateway with bidirectional DHCHAP."""
    for gateway in gateways:
        try:
            initiator.connect(
                **{
                    "transport": "tcp",
                    "traddr": gateway.node.ip_address,
                    "trsvcid": str(port),
                    "nqn": nqn,
                    "dhchap-secret": host_key,
                    "dhchap-ctrl-secret": subsys_key,
                    "ctrl-loss-tmo": 3600,
                }
            )
        except CommandFailed as exc:
            text = str(exc).lower()
            if any(token in text for token in ALREADY_CONNECTED):
                LOG.info(
                    "%s already connected to %s via %s",
                    initiator.node.hostname,
                    nqn,
                    gateway.node.hostname,
                )
                continue
            raise


def _nqn_connected(initiator, nqn):
    out, _ = initiator.node.exec_command(
        cmd="nvme list-subsys",
        sudo=True,
        check_ec=False,
    )
    return nqn in (out or "")


def _disconnect_nqn(initiator, nqn, tries=8, delay=2):
    """Disconnect ``nqn`` and wait until list-subsys no longer shows it."""
    host = initiator.node.hostname
    for attempt in range(1, tries + 1):
        try:
            initiator.disconnect(**{"nqn": nqn})
        except CommandFailed as exc:
            LOG.warning("disconnect %s on %s: %s", nqn, host, exc)
        if not _nqn_connected(initiator, nqn):
            LOG.info("%s: %s disconnected (attempt %s)", host, nqn, attempt)
            return
        LOG.warning(
            "%s: %s still in list-subsys after disconnect attempt %s/%s",
            host,
            nqn,
            attempt,
            tries,
        )
        time.sleep(delay)
    raise RuntimeError(f"{host} still connected to {nqn} after {tries} disconnects")


def _cross_records(gateway, subsystems):
    """Build ns records for every CROSS ENC image."""
    records = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        out, _ = gateway.namespace.list(
            **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
        )
        listed = json.loads(out).get("namespaces", []) if out else []
        wanted = {item["image"] for item in sub.get("namespaces") or []}
        found = []
        for ns in listed:
            name = ns.get("rbd_image_name") or ""
            if name not in wanted:
                continue
            record = _ns_record(nqn, ns)
            record["owner"] = sub["owner"]
            record["host_key"] = sub["host_key"]
            record["subsys_key"] = sub["subsys_key"]
            records.append(record)
            found.append(name)
        missing = wanted - set(found)
        if missing:
            raise RuntimeError(f"{nqn} missing CROSS ENC namespaces: {sorted(missing)}")
        LOG.info("%s: %s CROSS ENC namespaces listed", nqn, len(found))
    return records


def _fio_opts(config, runtime):
    opts = {
        "ioengine": "libaio",
        "direct": "1",
        "bs": config.get("bs", "64k"),
        "rw": config.get("io_type", "randwrite"),
        "iodepth": str(config.get("iodepth", 4)),
        "group_reporting": "1",
        "time_based": "1",
        "runtime": str(runtime),
        "numjobs": "1",
        "overwrite": "1",
        "continue_on_error": "io",
        "verify": config.get("verify", "crc32c"),
        "do_verify": "1",
    }
    fio_size = config.get("fio_size", "1G")
    if fio_size:
        opts["size"] = str(fio_size)
    return opts


def _run_cross_fio(node, job_path):
    LOG.info("Starting FIO %s on %s", job_path, node.hostname)
    return node.exec_command(
        cmd=f"fio --output={CROSS_FIO_LOG} --status-interval=30 {job_path}",
        sudo=True,
        long_running=True,
        timeout="notimeout",
    )


def _fio_io_errors(clients):
    errors = []
    for client in clients:
        out, _ = client.exec_command(
            cmd=f"grep -E 'err=' {CROSS_FIO_LOG} || true",
            sudo=True,
            check_ec=False,
        )
        text = (out or "").strip()
        if not text:
            continue
        for line in text.splitlines():
            match = FIO_ERR_RE.search(line)
            if match and int(match.group(1)) != 0:
                errors.append(f"{client.hostname}: {line.strip()}")
    return errors


def _assert_fio_healthy(clients, label):
    dead = []
    for client in clients:
        running, text = _fio_running(client)
        if running:
            LOG.info("%s: FIO still running %s: %s", client.hostname, label, text)
            continue
        dead.append(client.hostname)
    if dead:
        raise RuntimeError(f"{label}: FIO is not running on {dead}")
    io_errors = _fio_io_errors(clients)
    if io_errors:
        raise RuntimeError(f"{label}: FIO reported IO errors:\n" + "\n".join(io_errors))


def _wait_crossenc_namespaces(
    gateway, peer, expected, subsystems, clients, timeout, delay
):
    """Wait until the restarted GW lists every CROSS ENC namespace."""
    host = gateway.node.hostname
    peer_host = peer.node.hostname if peer else None
    started = time.time()
    deadline = started + timeout
    attempt = 0
    expected_total = sum(len(names) for names in expected.values())
    last_missing = expected_total
    while True:
        attempt += 1
        elapsed = int(time.time() - started)
        _assert_fio_healthy(clients, f"{elapsed}s after {host} restart")
        restarted_listed, restarted_degraded, restarted_errors = (
            _scan_gateway_namespaces(gateway, subsystems)
        )
        peer_listed, peer_degraded, peer_errors = (
            _scan_gateway_namespaces(peer, subsystems) if peer else ({}, [], [])
        )
        restarted_count = sum(len(nss) for nss in restarted_listed.values())
        peer_count = sum(len(nss) for nss in peer_listed.values())
        missing = _missing_expected(restarted_listed, expected)
        last_missing = len(missing)
        LOG.info(
            "%s CROSS ENC reopen attempt %s at %ss: restarted listed=%s/%s "
            "degraded=%s; peer %s listed=%s degraded=%s",
            host,
            attempt,
            elapsed,
            restarted_count,
            expected_total,
            len(restarted_degraded),
            peer_host or "-",
            peer_count,
            len(peer_degraded),
        )
        both_degraded = bool(restarted_degraded) and bool(peer_degraded)
        both_failed = bool(restarted_errors) and (
            peer is None or bool(peer_errors)
        )
        if both_degraded or both_failed:
            raise RuntimeError(
                f"{host}: degraded/list errors on restarted GW and peer "
                f"after {elapsed}s"
            )
        if not missing and not restarted_degraded:
            return elapsed
        if time.time() >= deadline:
            preview = "\n".join(missing[:20])
            raise RuntimeError(
                f"{host}: CROSS ENC ns list {expected_total - last_missing}/"
                f"{expected_total} after {elapsed}s:\n{preview}"
            )
        time.sleep(delay)


def _wrong_host_key(initiator, good_key, nqn):
    """Generate a well-formed DHCHAP key that is not ``good_key``.

    Flipping the last character of a real key (often a trailing ``:``)
    produces an invalid secret. nvme-cli then ignores ``--dhchap-secret``
    and can reuse the kernel keyring from the previous good connect.
    """
    for attempt in range(1, 11):
        key = _gen_dhchap_key(initiator, f"{nqn}:wrong:{attempt}")
        if key != good_key and key.startswith("DHHC-") and key.endswith(":"):
            return key
    raise RuntimeError(f"Could not generate a valid wrong DHCHAP key for {nqn}")


def _listed_subsystem_nqns(gateway):
    out, _ = gateway.subsystem.list(**{"base_cmd_args": {"format": "json"}})
    listed = json.loads(out).get("subsystems", []) if out else []
    return [item.get("nqn") for item in listed if item.get("nqn")]


def _is_crossenc_nqn(nqn, prefix):
    return bool(nqn) and (nqn == prefix or nqn.startswith(prefix))


def _connected_nqns(initiator):
    out, _ = initiator.node.exec_command(
        cmd="nvme list-subsys",
        sudo=True,
        check_ec=False,
    )
    nqns = []
    for line in (out or "").splitlines():
        if "NQN=" in line:
            nqns.append(line.split("NQN=", 1)[1].strip().split()[0])
            continue
        for token in line.replace(",", " ").split():
            if token.startswith("nqn."):
                nqns.append(token)
    return nqns


def _disconnect_crossenc_initiators(clients, prefix):
    """Drop leftover CROSS ENC DHCHAP sessions; leave other NQNs alone."""
    for client in clients:
        initiator = NVMeInitiator(client)
        leftover = [
            nqn for nqn in _connected_nqns(initiator) if _is_crossenc_nqn(nqn, prefix)
        ]
        if not leftover:
            LOG.info("%s: no leftover CROSS ENC initiator sessions", client.hostname)
            continue
        LOG.info(
            "%s: disconnecting leftover CROSS ENC NQNs: %s",
            client.hostname,
            leftover,
        )
        for nqn in leftover:
            try:
                _disconnect_nqn(initiator, nqn)
            except RuntimeError as exc:
                LOG.warning("%s", exc)


def _delete_crossenc_subsystem(gateway, nqn):
    LOG.info("Deleting leftover CROSS ENC subsystem %s", nqn)
    try:
        out, err = gateway.subsystem.delete(
            **{"args": {"subsystem": nqn, "force": True}}
        )
    except CommandFailed as exc:
        if _is_host_missing(exc):
            LOG.info("Subsystem %s already gone", nqn)
            return
        if _is_transport_glitch(exc):
            LOG.warning("subsystem del %s dropped (%s); retrying once", nqn, exc)
            time.sleep(5)
            try:
                gateway.subsystem.delete(
                    **{"args": {"subsystem": nqn, "force": True}}
                )
                return
            except CommandFailed as retry_exc:
                if _is_host_missing(retry_exc):
                    LOG.info("Subsystem %s already gone after retry", nqn)
                    return
                raise
        raise
    combined = f"{out or ''} {err or ''}".lower()
    if combined.strip() and "success" not in combined and not any(
        token in combined for token in ("not found", "no such", "does not exist")
    ):
        LOG.warning("subsystem del %s: %s %s", nqn, out, err)


def _delete_crossenc_images(rbd_obj, pool):
    existing = _rbd_image_names(rbd_obj, pool)
    images = sorted(name for name in existing if name.startswith("crossenc_"))
    if not images:
        LOG.info("No leftover CROSS ENC RBD images in %s", pool)
        return
    LOG.info(
        "Removing %s leftover CROSS ENC RBD image(s) from %s",
        len(images),
        pool,
    )
    with parallel() as p:
        for image in images:
            LOG.info("rbd rm %s/%s", pool, image)
            p.spawn(
                rbd_obj.exec_cmd,
                cmd=f"rbd rm {pool}/{image}",
                check_ec=False,
            )
        for _ in p:
            pass
    remaining = sorted(
        name
        for name in _rbd_image_names(rbd_obj, pool)
        if name.startswith("crossenc_")
    )
    if remaining:
        LOG.warning(
            "Retrying rbd rm for %s CROSS ENC image(s) still present",
            len(remaining),
        )
        time.sleep(10)
        for image in remaining:
            rbd_obj.exec_cmd(cmd=f"rbd rm {pool}/{image}", check_ec=False)
        remaining = sorted(
            name
            for name in _rbd_image_names(rbd_obj, pool)
            if name.startswith("crossenc_")
        )
    if remaining:
        raise RuntimeError(
            "CROSS ENC RBD images still present after rm: "
            f"{remaining[:20]}"
        )


def _cleanup_prior_crossenc(gateway, clients, rbd_obj, config):
    """Disconnect leftover CROSS ENC sessions and remove those NQNs from group2."""
    prefix = config.get("nqn_prefix", CROSS_NQN_PREFIX)
    pool = config.get("rbd_pool", "rbd")
    timeout = int(config.get("cleanup_timeout", 600))
    LOG.info(
        "Cleaning leftover CROSS ENC (prefix %s) from initiators and group2",
        prefix,
    )
    _disconnect_crossenc_initiators(clients, prefix)
    leftover = [
        nqn
        for nqn in _listed_subsystem_nqns(gateway)
        if _is_crossenc_nqn(nqn, prefix)
    ]
    if not leftover:
        LOG.info("No leftover CROSS ENC subsystems on group2")
    else:
        LOG.info(
            "Removing %s leftover CROSS ENC subsystem(s): %s",
            len(leftover),
            leftover,
        )
        for nqn in leftover:
            _delete_crossenc_subsystem(gateway, nqn)
        deadline = time.time() + timeout
        while True:
            still = [
                nqn
                for nqn in _listed_subsystem_nqns(gateway)
                if _is_crossenc_nqn(nqn, prefix)
            ]
            if not still:
                break
            if time.time() >= deadline:
                raise RuntimeError(
                    "CROSS ENC subsystems still listed after cleanup: "
                    f"{still}"
                )
            LOG.warning(
                "Waiting for CROSS ENC subsystem delete: %s still listed",
                still,
            )
            time.sleep(10)
    _delete_crossenc_images(rbd_obj, pool)
    LOG.info(
        "Prior CROSS ENC DHCHAP subsystems, namespaces, and images removed"
    )


def _listed_host_nqns(gateway, nqn):
    out, _ = gateway.host.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    hosts = json.loads(out).get("hosts", []) if out else []
    return [item.get("nqn") for item in hosts if item.get("nqn")]


def _existing_crossenc_subsystems(gateway, config):
    """Rebuild CROSS ENC subsystem dicts from group2 without creating anything."""
    prefix = config.get("nqn_prefix", CROSS_NQN_PREFIX)
    group = config.get("gw_group")
    count = int(config.get("subsystems", 32))
    listed = set(_listed_subsystem_nqns(gateway))
    subsystems = []
    missing = []
    for num in range(1, count + 1):
        nqn = f"{prefix}{num}"
        group_nqn = _group_nqn(nqn, group)
        actual = group_nqn if group_nqn in listed else (nqn if nqn in listed else None)
        if not actual:
            missing.append(group_nqn)
            continue
        ns_map = _listed_ns_map(gateway, actual)
        subsystems.append(
            {
                "num": num,
                "nqn": nqn,
                "group_nqn": actual,
                "namespaces": [
                    {"image": name, "nqn": actual, "ns_index": idx}
                    for idx, name in enumerate(sorted(ns_map), 1)
                ],
            }
        )
    if missing:
        raise RuntimeError(
            f"resume_from={RESUME_WRONG_KEY_PROBE} missing subsystems: {missing}"
        )
    return subsystems


def _assign_clients_from_hosts(gateway, subsystems, host_nqns):
    """Assign each NQN to the client whose initiator NQN is already allowed."""
    nqn_to_host = {nqn: hostname for hostname, nqn in host_nqns.items()}
    assigned = {hostname: [] for hostname in host_nqns}
    for sub in subsystems:
        hosts = _listed_host_nqns(gateway, sub["group_nqn"])
        owners = []
        for host_nqn in hosts:
            owner = nqn_to_host.get(host_nqn)
            if owner and owner not in owners:
                owners.append(owner)
        if len(owners) != 1:
            raise RuntimeError(
                f"{sub['group_nqn']}: expected one CROSS ENC client, found {owners} ({hosts})"
            )
        sub["owner"] = owners[0]
        assigned[sub["owner"]].append(sub)
    for hostname, subs in assigned.items():
        LOG.info("%s owns %s CROSS ENC subsystems", hostname, len(subs))
    return assigned


def _rekey_existing_dhchap(gateway, subsystems, initiators, host_nqns, settle_sec):
    """Replace DHCHAP secrets in place; host list does not return the live keys."""
    for sub in subsystems:
        owner = sub["owner"]
        initiator = initiators[owner]
        host_nqn = host_nqns[owner]
        sub["subsys_key"] = _gen_dhchap_key(initiator, sub["group_nqn"])
        sub["host_key"] = _gen_dhchap_key(
            initiator, f"{host_nqn}:{sub['group_nqn']}"
        )
        if sub["host_key"] == sub["subsys_key"]:
            sub["host_key"] = _gen_dhchap_key(
                initiator, f"{host_nqn}:host:{sub['num']}"
            )
        LOG.info("Re-keying DHCHAP on %s for %s", sub["group_nqn"], owner)
        gateway.subsystem.change_key(
            **{"args": {"subsystem": sub["group_nqn"], "dhchap-key": sub["subsys_key"]}}
        )
        _host_change_key(gateway, sub["group_nqn"], host_nqn, sub["host_key"])
        if settle_sec:
            time.sleep(settle_sec)


def _run_wrong_key_resume(gateway, gateways, clients, config, started):
    """Reconnect existing CROSS ENC NQNs and run only the wrong-key probe."""
    port = config.get("listener_port", DEFAULT_LISTENER_PORT)
    ns_per_sub = int(config.get("namespaces_per_subsystem", DEFAULT_NS_PER_SUB))
    expected_ns = int(config.get("subsystems", 32)) * ns_per_sub
    settle = float(config.get("dhchap_settle_sec", DEFAULT_DHCHAP_SETTLE_SEC))

    subsystems = _existing_crossenc_subsystems(gateway, config)
    initiators = {}
    host_nqns = {}
    for client in clients:
        initiator = NVMeInitiator(client)
        host_nqns[client.hostname] = initiator.initiator_nqn()
        initiators[client.hostname] = initiator
        LOG.info("Client %s host NQN %s", client.hostname, host_nqns[client.hostname])

    assigned = _assign_clients_from_hosts(gateway, subsystems, host_nqns)
    _rekey_existing_dhchap(gateway, subsystems, initiators, host_nqns, settle)

    LOG.info("Reconnecting existing CROSS ENC DHCHAP sessions")
    for client in clients:
        initiator = initiators[client.hostname]
        for sub in assigned[client.hostname]:
            _connect_nqn(
                initiator,
                gateways,
                sub["group_nqn"],
                port,
                sub["host_key"],
                sub["subsys_key"],
            )
    time.sleep(5)

    records = _cross_records(gateway, subsystems)
    if len(records) != expected_ns:
        raise RuntimeError(
            f"Expected {expected_ns} CROSS ENC namespaces, found {len(records)}"
        )
    mapped = {}
    for client in clients:
        initiator = initiators[client.hostname]
        owned = [ns for ns in records if ns["owner"] == client.hostname]
        uuids = [_norm_uuid(ns["uuid"]) for ns in owned]
        paths = _wait_for_paths(initiator, uuids, client.hostname)
        expected_paths = len(assigned[client.hostname]) * ns_per_sub
        if len(paths) != expected_paths:
            raise RuntimeError(
                f"{client.hostname}: expected {expected_paths} CROSS ENC "
                f"devices, found {len(paths)}"
            )
        mapped[client.hostname] = (client, initiator, paths)
        LOG.info(
            "%s authenticated and can see %s CROSS ENC namespaces",
            client.hostname,
            len(paths),
        )

    _wrong_key_probe(
        gateways,
        initiators,
        assigned,
        mapped,
        records,
        port,
        int(config.get("bad_dhchap_subsystems", DEFAULT_BAD_KEY_SUBS)),
    )
    LOG.info(
        "Wrong-key probe resume passed for %s namespaces in %ss",
        len(records),
        int(time.time() - started),
    )
    return 0


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """CROSS ENC v1. Returns 0 on success, 1 on failure."""
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)
    started = time.time()
    timings = {}
    clients = []
    stop_state = {"stopped": False}
    preserve_sessions = (
        str(config.get("resume_from") or "").strip() == RESUME_WRONG_KEY_PROBE
    )

    try:
        rbd_obj = _init_rbd(kwargs)
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        for gw in nvme_service.gateways:
            gw.load_gateway_info()
        gateway = nvme_service.gateways[0]
        gateways = nvme_service.gateways
        clients = ceph_cluster.get_nodes(role="client")
        if not clients:
            raise ValueError("CROSS ENC requires a client node")
        if len(gateways) < 2:
            raise ValueError("CROSS ENC requires at least two gateways")
        if preserve_sessions:
            LOG.info(
                "Resuming CROSS ENC at wrong-key probe; skipping cleanup, "
                "recreate, FIO, and GW restart"
            )
            return _run_wrong_key_resume(
                gateway, gateways, clients, config, started
            )
        if config.get("cleanup_prior", True):
            t0 = time.time()
            _cleanup_prior_crossenc(gateway, clients, rbd_obj, config)
            timings["cleanup_prior"] = round(time.time() - t0, 1)
            LOG.info("Prior CROSS ENC cleanup finished in %ss", timings["cleanup_prior"])

        if config.get("ensure_encryption", True):
            LOG.info(
                "Ensuring group2 has enable_encryption + encryption_key_path "
                "for DHCHAP; orch apply will not bounce GWs"
            )
            before_pids = {
                gw.node.hostname: _gw_unit_identity(gw)
                for gw in nvme_service.gateways
            }
            if nvme_service.ensure_encryption_key():
                t0 = time.time()
                pid_timeout = int(
                    config.get("pid_change_timeout", DEFAULT_PID_CHANGE_TIMEOUT)
                )
                pid_delay = int(config.get("gw_restart_delay", 30))
                LOG.info(
                    "Waiting for group2 PID change after orch redeploy, "
                    "not after orch apply"
                )
                for gw in nvme_service.gateways:
                    _wait_pid_changed(
                        gw,
                        before_pids[gw.node.hostname],
                        pid_timeout,
                        pid_delay,
                    )
                nvme_service.wait_for_gateways(tries=36, delay=10)
                gw_nodes = [gw.node for gw in nvme_service.gateways]
                _copy_kmip_certs_when_containers_ready(gw_nodes)
                timings["encryption_redeploy"] = round(time.time() - t0, 1)
                LOG.info(
                    "Group2 encryption redeployed; GWs ready and KMIP certs "
                    "copied in %ss",
                    timings["encryption_redeploy"],
                )
            gateway = nvme_service.gateways[0]
            gateways = nvme_service.gateways

        if not config.get("network_mask"):
            config["network_mask"] = get_network_mask(gateways)
        port = config.get("listener_port", DEFAULT_LISTENER_PORT)
        ns_per_sub = int(config.get("namespaces_per_subsystem", DEFAULT_NS_PER_SUB))
        expected_ns = int(config.get("subsystems", 32)) * ns_per_sub

        kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
        passphrases_by_node = load_passphrases_all(
            kmip_nodes,
            cli_image=config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE),
        )

        subsystems = _add_crossenc_subsystems(gateway, config)
        _verify_listeners(gateway, subsystems, nvme_service.gw_nodes, port)
        _assign_kmip_endpoints(
            subsystems,
            kmip_nodes,
            int(config.get("subsystems_per_kmip", 2)),
        )
        _add_kmip_endpoints(
            gateway, subsystems, int(config.get("kmip_port", KMIP_PORT))
        )
        _add_luks2_namespaces(gateway, subsystems, config, passphrases_by_node)

        assigned = _assign_clients(subsystems, clients)
        host_nqns = {}
        initiators = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            initiator.configure()
            initiator.disconnect_all()
            host_nqns[client.hostname] = initiator.initiator_nqn()
            initiators[client.hostname] = initiator
            LOG.info(
                "Client %s host NQN %s",
                client.hostname,
                host_nqns[client.hostname],
            )

        settle = float(
            config.get("dhchap_settle_sec", DEFAULT_DHCHAP_SETTLE_SEC)
        )
        host_add_tries = int(
            config.get("dhchap_host_add_tries", DEFAULT_DHCHAP_HOST_ADD_TRIES)
        )
        LOG.info(
            "Configuring bidirectional DHCHAP on %s CROSS ENC subsystems "
            "(settle=%ss, host_add_tries=%s)",
            len(subsystems),
            settle,
            host_add_tries,
        )
        for sub in subsystems:
            owner = sub["owner"]
            initiator = initiators[owner]
            host_nqn = host_nqns[owner]
            sub["subsys_key"] = _gen_dhchap_key(initiator, sub["group_nqn"])
            sub["host_key"] = _gen_dhchap_key(
                initiator, f"{host_nqn}:{sub['group_nqn']}"
            )
            if sub["host_key"] == sub["subsys_key"]:
                sub["host_key"] = _gen_dhchap_key(
                    initiator, f"{host_nqn}:host:{sub['num']}"
                )
            _configure_dhchap(
                gateway,
                sub["group_nqn"],
                host_nqn,
                sub["host_key"],
                sub["subsys_key"],
                settle_sec=settle,
                tries=host_add_tries,
            )
            if settle:
                time.sleep(settle)

        LOG.info("Connecting 32 DHCHAP sessions (unique host key per subsystem)")
        for client in clients:
            initiator = initiators[client.hostname]
            for sub in assigned[client.hostname]:
                _connect_nqn(
                    initiator,
                    gateways,
                    sub["group_nqn"],
                    port,
                    sub["host_key"],
                    sub["subsys_key"],
                )
        time.sleep(5)

        records = _cross_records(gateway, subsystems)
        if len(records) != expected_ns:
            raise RuntimeError(
                f"Expected {expected_ns} CROSS ENC namespaces, found {len(records)}"
            )
        mapped = {}
        for client in clients:
            initiator = initiators[client.hostname]
            owned = [ns for ns in records if ns["owner"] == client.hostname]
            uuids = [_norm_uuid(ns["uuid"]) for ns in owned]
            paths = _wait_for_paths(initiator, uuids, client.hostname)
            expected_paths = len(assigned[client.hostname]) * ns_per_sub
            if len(paths) != expected_paths:
                raise RuntimeError(
                    f"{client.hostname}: expected {expected_paths} CROSS ENC "
                    f"devices, found {len(paths)}"
                )
            mapped[client.hostname] = (client, initiator, paths)
            LOG.info(
                "%s authenticated and can see %s CROSS ENC namespaces",
                client.hostname,
                len(paths),
            )

        runtime = int(config.get("fio_max_runtime", DEFAULT_FIO_MAX_RUNTIME))
        for client, _, paths in mapped.values():
            _write_light_fio_job(client, CROSS_JOB, paths, _fio_opts(config, runtime))

        expected_snapshot, snapshot_total = _expected_ns_snapshot(gateway, subsystems)
        LOG.info("CROSS ENC snapshot before GW1 restart: %s images", snapshot_total)

        errors = []
        with parallel(timeout=runtime + 600) as p:
            for client, _, _ in mapped.values():
                p.spawn(_run_cross_fio, client, CROSS_JOB)
            p.spawn(
                _restart_gw1_and_negative,
                nvme_service,
                gateway,
                gateways,
                subsystems,
                expected_snapshot,
                clients,
                initiators,
                assigned,
                records,
                mapped,
                config,
                timings,
                stop_state,
            )
            for result in p:
                if isinstance(result, Exception):
                    if stop_state["stopped"]:
                        LOG.info("FIO ended after stop: %s", result)
                        continue
                    for client in clients:
                        client.exec_command(
                            cmd="pkill -9 -x fio || true", sudo=True, check_ec=False
                        )
                    raise result
                if isinstance(result, int) and result not in FIO_STOP_EXIT_CODES:
                    if stop_state["stopped"]:
                        continue
                    errors.append(result)
        if errors:
            raise RuntimeError(f"FIO {CROSS_JOB} failed with {errors}")
        io_errors = _fio_io_errors(clients)
        if io_errors:
            raise RuntimeError("FIO reported IO errors:\n" + "\n".join(io_errors))

        LOG.info(
            "CROSS ENC passed: %s LUKS2 NS, DHCHAP reconnect+refetch %s, "
            "FIO err=0, completed in %ss",
            len(records),
            _fmt_seconds(timings.get("reconnect_s", 0)),
            int(time.time() - started),
        )
        return 0
    except Exception as err:
        LOG.exception("CROSS ENC test failed: %s", err)
        return 1
    finally:
        try:
            _stop_fio(clients)
        except Exception as exc:
            LOG.warning("FIO stop after CROSS ENC: %s", exc)
        if preserve_sessions:
            LOG.info("Leaving initiator sessions in place after wrong-key resume")
        else:
            try:
                for node in ceph_cluster.get_nodes(role="client"):
                    NVMeInitiator(node).disconnect_all()
            except Exception as exc:
                LOG.warning("Cleanup after CROSS ENC: %s", exc)


def _restart_gw1_and_negative(
    nvme_service,
    gateway,
    gateways,
    subsystems,
    expected_snapshot,
    clients,
    initiators,
    assigned,
    records,
    mapped,
    config,
    timings,
    stop_state,
):
    """Restart GW1 under FIO, then run the wrong-DHCHAP-key check."""
    delay = int(config.get("gw_restart_delay", 30))
    after = int(config.get("io_after_runtime", 60))
    port = config.get("listener_port", DEFAULT_LISTENER_PORT)
    peer = next((gw for gw in gateways if gw is not gateway), None)
    host = gateway.node.hostname
    try:
        LOG.info("Waiting %ss for FIO, then restarting %s", delay, host)
        time.sleep(delay)
        _assert_fio_healthy(clients, "before GW1 restart")
        try:
            _refresh_gateway_ssh(gateway)
        except Exception as exc:
            LOG.warning("SSH refresh to %s before restart failed: %s", host, exc)
        before = _gw_unit_identity(gateway)
        started = time.time()
        LOG.info("========== CROSS ENC restart %s pid=%s ==========", host, before["main_pid"])
        nvme_service.restart_daemon(gateway, wait_sec=0)
        after_id = _wait_pid_changed(
            gateway,
            before,
            timeout=int(config.get("gw_pid_timeout", DEFAULT_PID_CHANGE_TIMEOUT)),
            delay=int(config.get("gw_pid_delay", 5)),
        )
        timings["pid_ready_s"] = int(time.time() - started)
        LOG.info(
            "%s restarted %s -> %s in %ss",
            host,
            before["main_pid"],
            after_id["main_pid"],
            timings["pid_ready_s"],
        )
        _copy_kmip_certs_when_containers_ready([gateway.node])
        gateway.load_gateway_info(
            tries=int(config.get("gw_ready_tries", 24)),
            delay=int(config.get("gw_ready_delay", 10)),
        )
        ns_s = _wait_crossenc_namespaces(
            gateway,
            peer,
            expected_snapshot,
            subsystems,
            clients,
            timeout=int(config.get("ns_reopen_timeout", DEFAULT_NS_REOPEN_TIMEOUT)),
            delay=int(config.get("ns_reopen_delay", 10)),
        )
        timings["ns_all_s"] = ns_s
        for client in clients:
            initiator = initiators[client.hostname]
            owned = [ns for ns in records if ns["owner"] == client.hostname]
            uuids = [_norm_uuid(ns["uuid"]) for ns in owned]
            paths = _wait_for_paths(initiator, uuids, client.hostname)
            LOG.info(
                "%s DHCHAP re-auth: %s CROSS ENC devices after GW1 restart",
                client.hostname,
                len(paths),
            )
        timings["reconnect_s"] = int(time.time() - started)
        LOG.info(
            "GW1 reconnect + KMIP re-fetch completed in %s (ns list %s, pid %s)",
            _fmt_seconds(timings["reconnect_s"]),
            _fmt_seconds(ns_s),
            _fmt_seconds(timings["pid_ready_s"]),
        )
        kmip_hits = _scan_and_save_kmip_errors([gateway], time.time() - started + 5)
        if kmip_hits:
            raise RuntimeError(
                f"KMIP timeout/error after restarting {host}:\n" + "\n".join(kmip_hits)
            )
        _assert_fio_healthy(clients, "after GW1 ns list complete")
        LOG.info("Leaving FIO running %ss after GW1 reopen", after)
        time.sleep(after)
        _assert_fio_healthy(clients, "after extra FIO window")
        stop_state["stopped"] = True
        _stop_fio(clients)

        _wrong_key_probe(
            gateways,
            initiators,
            assigned,
            mapped,
            records,
            port,
            int(config.get("bad_dhchap_subsystems", DEFAULT_BAD_KEY_SUBS)),
        )
    except Exception:
        stop_state["stopped"] = True
        _stop_fio(clients)
        raise


def _wrong_key_probe(
    gateways, initiators, assigned, mapped, records, port, bad_count
):
    """Connect 10 NQNs with a bad DHCHAP key; other sessions must stay up."""
    targets = []
    for subs in assigned.values():
        targets.extend(subs)
    targets = targets[:bad_count]
    if len(targets) < bad_count:
        raise RuntimeError(f"Need {bad_count} subsystems for wrong-key probe")
    protected = {sub["group_nqn"] for sub in targets}
    LOG.info(
        "Wrong DHCHAP key against %s subsystems: %s",
        len(targets),
        [sub["group_nqn"] for sub in targets],
    )
    for sub in targets:
        initiator = initiators[sub["owner"]]
        _disconnect_nqn(initiator, sub["group_nqn"])
        bad_key = _wrong_host_key(initiator, sub["host_key"], sub["group_nqn"])
        LOG.info(
            "Probing %s with a valid different host key (good suffix %r, bad %r)",
            sub["group_nqn"],
            sub["host_key"][-8:],
            bad_key[-8:],
        )
        # NVMeCLI.connect() uses Cli.execute(check_ec=False), so a rejected
        # nvme connect (exit 1, "Key was rejected by service") never raises
        # CommandFailed. Judge the attempt by the SSH exit code and whether
        # the NQN actually comes back in list-subsys.
        connect_args = {
            "transport": "tcp",
            "traddr": gateways[0].node.ip_address,
            "trsvcid": str(port),
            "nqn": sub["group_nqn"],
            "dhchap-secret": bad_key,
            "dhchap-ctrl-secret": sub["subsys_key"],
            "ctrl-loss-tmo": 3600,
        }
        out, err, exit_code, _ = initiator.node.exec_command(
            cmd=f"nvme connect {config_dict_to_string(connect_args)}",
            sudo=True,
            check_ec=False,
            verbose=True,
            pretty_print=True,
        )
        connected = _nqn_connected(initiator, sub["group_nqn"])
        if exit_code == 0 or connected:
            raise RuntimeError(
                f"{sub['group_nqn']}: connect with incorrect DHCHAP key succeeded "
                f"(exit {exit_code}, connected={connected}, "
                f"good suffix {sub['host_key'][-8:]!r}, bad {bad_key[-8:]!r}, "
                f"stdout={out!r}, stderr={err!r})"
            )
        LOG.info(
            "Rejected bad DHCHAP key for %s (exit %s, stderr %r)",
            sub["group_nqn"],
            exit_code,
            (err or "").strip(),
        )

    for hostname, (client, initiator, _) in mapped.items():
        still_up = [
            ns for ns in records if ns["owner"] == hostname and ns["nqn"] not in protected
        ]
        if not still_up:
            continue
        uuids = [_norm_uuid(ns["uuid"]) for ns in still_up]
        paths = _paths_for_uuids(initiator, uuids)
        if len(paths) < len(uuids):
            raise RuntimeError(
                f"{client.hostname}: bad DHCHAP rejects reduced adjacent "
                f"sessions ({len(paths)}/{len(uuids)} devices still visible)"
            )
        LOG.info(
            "%s: %s adjacent authenticated namespaces still visible after rejects",
            client.hostname,
            len(paths),
        )
    LOG.info("Wrong DHCHAP key rejected; adjacent CROSS ENC sessions unaffected")
