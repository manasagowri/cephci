"""KMIP passphrase-fetch concurrency with rolling gateway restarts.

Reuses the existing 512 encrypted *parent* namespaces on group2, including
their current namespace masks. After nvme discover/connect, starts one
light FIO job per client and keeps it running until every gateway has
been restarted one at a time.

After each restart the test:
- asserts FIO is still running with ``err=0``
- runs ``ns list`` on the restarted gateway **and** a peer
- fails if *both* lists show any namespace in a degraded bdev state
- waits until the restarted gateway lists every namespace that was
  present before the restart, and records that reopen time

Leftover ``orphan_`` clone NS are removed first (default) so OMAP apply
is not aborted by stacked-LUKS ``encryption_load2`` EPERM. Matching KMIP
errors are written under ``<run_dir>/gateway_logs/<host>_kmip_errors.log``.
"""

import json
import os
import re
import shlex
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _existing_subsystems,
    _init_rbd,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _assert_gateways_ready,
    _cleanup_orphan_images,
    _collect_unit_journal,
    _copy_kmip_certs_when_containers_ready,
    _gw_unit_identity,
    _refresh_gateway_ssh,
    _run_log_dir,
    _short_host,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import (
    _connect_client,
    _host_names,
    _is_clone,
    _norm_uuid,
    _ns_record,
    _ns_usable,
    _paths_for_uuids,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

PARENT_JOB = "/tmp/kmip_parent_restart.fio"
PARENT_FIO_LOG = "/tmp/kmip_parent_restart.fio.log"
DEVICE_WAIT_TRIES = 12
DEVICE_WAIT_DELAY = 10
# Safety ceiling so a hung restart loop cannot leave FIO running forever.
DEFAULT_FIO_MAX_RUNTIME = 28800
DEFAULT_NS_REOPEN_TIMEOUT = 5400
# ceph orch daemon restart only schedules the restart; wait for systemd.
DEFAULT_PID_CHANGE_TIMEOUT = 300
DEFAULT_PID_CHANGE_DELAY = 5
FIO_STOP_EXIT_CODES = {0, 128 + 9, 128 + 15, -9, -15}
FIO_ERR_RE = re.compile(r"err=(\d+)")
NS_LIST_RETRYABLE = (
    "unable to find a target",
    "failed to connect",
    "connection refused",
    "timed out",
    "timeout",
    "errno 60",
    "going down",
)
KMIP_ERROR_RE = re.compile(
    r"(kmip.*(error|timeout|fail|exception|traceback|timed out)"
    r"|(error|timeout|fail|exception|traceback|timed out).*kmip"
    r"|wrong passphrase"
    r"|encryption_load2"
    r"|failed to retrieve"
    r"|unable to retrieve)",
    re.IGNORECASE,
)


def _is_parent_image(name):
    """True for original BYOK parent images (not clones, orphans, or plain)."""
    if not name or _is_clone({"rbd_image_name": name}):
        return False
    if name.startswith("orphan_") or name.startswith("plain_"):
        return False
    return name.startswith("byok_c") and "_n" in name


def _parent_ns_records(gateway, subsystems, clients, host_nqns):
    """Reuse existing parent-NS ACLs; do not remask."""
    if not clients:
        raise ValueError("KMIP GW restart test requires a client node")
    records = []
    unmatched = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        out, _ = gateway.namespace.list(
            **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
        )
        listed = json.loads(out).get("namespaces", []) if out else []
        parents = []
        for ns in listed:
            name = ns.get("rbd_image_name") or ""
            if not _is_parent_image(name):
                continue
            if not _ns_usable(ns):
                raise RuntimeError(
                    f"Parent {name} on {nqn} is not usable "
                    f"(degraded={ns.get('degraded')} size={ns.get('rbd_image_size')})"
                )
            hosts = _host_names(ns.get("hosts"))
            matches = [
                hostname
                for hostname, host_nqn in host_nqns.items()
                if host_nqn in hosts
            ]
            if len(matches) != 1:
                unmatched.append(
                    f"{nqn} {name} nsid={ns.get('nsid')} hosts={hosts} "
                    f"client_matches={matches}"
                )
                continue
            record = _ns_record(nqn, ns)
            record["owner"] = matches[0]
            record["owner_nqn"] = host_nqns[matches[0]]
            records.append(record)
            parents.append(name)
        LOG.info("%s: %s encrypted parent namespaces with existing mask", nqn, len(parents))
    if unmatched:
        preview = "\n".join(unmatched[:15])
        extra = f"\n... and {len(unmatched) - 15} more" if len(unmatched) > 15 else ""
        raise RuntimeError(
            f"{len(unmatched)} parent namespaces are not already masked to "
            f"exactly one client; skip remasking cannot proceed:\n{preview}{extra}"
        )
    assigned = {client.hostname: [] for client in clients}
    for record in records:
        assigned[record["owner"]].append(record)
    for client in clients:
        LOG.info(
            "Existing mask: %s owns %s parent namespaces",
            client.hostname,
            len(assigned[client.hostname]),
        )
    return records, assigned


def _wait_for_paths(initiator, uuids, hostname):
    last_paths = []
    for attempt in range(1, DEVICE_WAIT_TRIES + 1):
        last_paths = _paths_for_uuids(initiator, uuids)
        LOG.info(
            "%s parent devices visible=%s expected=%s (attempt %s/%s)",
            hostname,
            len(last_paths),
            len(uuids),
            attempt,
            DEVICE_WAIT_TRIES,
        )
        if len(last_paths) >= len(uuids):
            return last_paths
        time.sleep(DEVICE_WAIT_DELAY)
    raise RuntimeError(
        f"{hostname}: expected {len(uuids)} parent devices, found {len(last_paths)}"
    )


def _write_light_fio_job(node, job_path, devices, global_opts):
    script = (
        "from pathlib import Path\n"
        f"devices = {json.dumps(list(devices))}\n"
        f"opts = {json.dumps(global_opts)}\n"
        "lines = ['[global]']\n"
        "for key, value in opts.items():\n"
        "    lines.append(f'{key}={value}')\n"
        "lines.append('[parent_ns]')\n"
        "lines.append('filename=' + ':'.join(devices))\n"
        f"Path({json.dumps(job_path)}).write_text('\\n'.join(lines) + '\\n')\n"
    )
    node.exec_command(cmd=f"python3 -c {shlex.quote(script)}", sudo=True)


def _fio_opts(config, runtime):
    opts = {
        "ioengine": "libaio",
        "direct": "1",
        "bs": config.get("bs", "64k"),
        "rw": config.get("io_type", "randrw"),
        "iodepth": str(config.get("iodepth", 4)),
        "group_reporting": "1",
        "time_based": "1",
        "runtime": str(runtime),
        "numjobs": "1",
        "overwrite": "1",
        "continue_on_error": "io",
    }
    fio_size = config.get("fio_size", "1G")
    if fio_size:
        opts["size"] = str(fio_size)
    return opts


def _prepare_fio_jobs(mapped, config, runtime):
    for client, _, paths in mapped.values():
        if not paths:
            raise RuntimeError(f"No parent devices on {client.hostname}")
        _write_light_fio_job(client, PARENT_JOB, paths, _fio_opts(config, runtime))


def _run_restart_fio_job(node, job_path):
    """Run FIO with CLI ``--output`` (not valid as a job-file key)."""
    LOG.info("Starting FIO %s on %s", job_path, node.hostname)
    return node.exec_command(
        cmd=(
            f"fio --output={PARENT_FIO_LOG} --status-interval=30 {job_path}"
        ),
        sudo=True,
        long_running=True,
        timeout="notimeout",
    )


def _connect_and_map(gateways, subsystems, clients, assigned, config):
    """Connect both clients and return {hostname: (client, initiator, paths)}."""
    port = config.get("listener_port", DEFAULT_LISTENER_PORT)
    mapped = {}
    for client in clients:
        initiator = NVMeInitiator(client)
        initiator.disconnect_all()
        _connect_client(initiator, gateways, subsystems, port)
        time.sleep(5)
        uuids = [_norm_uuid(ns["uuid"]) for ns in assigned[client.hostname]]
        paths = _wait_for_paths(initiator, uuids, client.hostname)
        mapped[client.hostname] = (client, initiator, paths)
        LOG.info(
            "%s connected with %s parent namespaces",
            client.hostname,
            len(paths),
        )
    return mapped


def _fio_running(node):
    out, _ = node.exec_command(
        cmd="pgrep -ax fio || true", sudo=True, check_ec=False
    )
    text = (out or "").strip()
    return bool(text), text


def _stop_fio(clients):
    """End the in-flight FIO jobs after rolling restarts complete."""
    for client in clients:
        LOG.info("Stopping FIO on %s after rolling gateway restarts", client.hostname)
        client.exec_command(
            cmd="pkill -TERM -x fio || true", sudo=True, check_ec=False
        )
    time.sleep(5)
    for client in clients:
        running, text = _fio_running(client)
        if not running:
            continue
        LOG.warning("%s: FIO still running after SIGTERM, sending SIGKILL: %s", client.hostname, text)
        client.exec_command(
            cmd="pkill -KILL -x fio || true", sudo=True, check_ec=False
        )


def _assert_fio_running(clients, label):
    dead = []
    for client in clients:
        running, text = _fio_running(client)
        if running:
            LOG.info("%s: FIO still running %s: %s", client.hostname, label, text)
            continue
        dead.append(client.hostname)
    if dead:
        raise RuntimeError(f"{label}: FIO is not running on {dead}")


def _fio_io_errors(clients):
    """Parse FIO ``err=`` from the per-client log while the job is running."""
    errors = []
    for client in clients:
        out, _ = client.exec_command(
            cmd=f"grep -E 'err=' {PARENT_FIO_LOG} || true",
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
    """FIO must still be running and must not have reported ``err!=0``."""
    _assert_fio_running(clients, label)
    io_errors = _fio_io_errors(clients)
    if io_errors:
        raise RuntimeError(
            f"{label}: FIO reported IO errors:\n" + "\n".join(io_errors)
        )


def _kmip_error_lines(text):
    return [line for line in (text or "").splitlines() if KMIP_ERROR_RE.search(line)]


def _scan_and_save_kmip_errors(gateways, since_seconds):
    """Write per-GW journals when KMIP errors are present. Return hit summaries."""
    run_dir = _run_log_dir()
    out_dir = os.path.join(run_dir, "gateway_logs") if run_dir else None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    hits = []
    for gw in gateways:
        host = _short_host(gw)
        journal = _collect_unit_journal(gw, since_seconds)
        matches = _kmip_error_lines(journal)
        if not matches:
            LOG.info("%s: no KMIP timeout/error lines in gateway journal", host)
            continue
        preview = "\n".join(matches[:20])
        hits.append(f"{host}: {len(matches)} KMIP error/timeout line(s)\n{preview}")
        if not out_dir:
            continue
        path = os.path.join(out_dir, f"{host}_kmip_errors.log")
        header = (
            f"# host={host} kmip error/timeout lines from last "
            f"{int(since_seconds)}s of nvmeof unit journal\n"
        )
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(header)
            fh.write("\n".join(matches))
            fh.write("\n")
        LOG.error("Saved KMIP error journal %s (%s hits)", path, len(matches))
    return hits


def _transient_ns_list(text):
    lowered = str(text or "").lower()
    return any(token in lowered for token in NS_LIST_RETRYABLE)


def _list_namespaces(gateway, nqn):
    """List namespaces for ``nqn``. Empty is valid during OMAP apply."""
    try:
        out, err = gateway.namespace.list(
            **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
        )
    except Exception as exc:
        raise CommandFailed(str(exc)) from exc
    text = f"{out or ''}\n{err or ''}"
    if _transient_ns_list(text):
        raise CommandFailed(text)
    if not out or not str(out).strip():
        raise CommandFailed(f"empty ns list stdout for {nqn}")
    try:
        return json.loads(out).get("namespaces", []) or []
    except (json.JSONDecodeError, TypeError) as exc:
        raise CommandFailed(f"invalid ns list JSON for {nqn}: {exc}") from exc


def _ns_is_degraded(ns):
    """True when a *listed* namespace is in a degraded/unusable bdev state."""
    if ns.get("degraded") in (True, "true", "True", 1, "yes"):
        return True
    bdev = str(ns.get("bdev_name") or "")
    if bdev.endswith("_degraded") or not bdev.strip():
        return True
    if str(ns.get("rbd_image_size") or "0") in ("0", ""):
        return True
    return False


def _scan_gateway_namespaces(gateway, subsystems):
    """Return listed NS, degraded entries, and per-NQN list errors (no raise)."""
    listed = {}
    degraded = []
    list_errors = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        try:
            namespaces = _list_namespaces(gateway, nqn)
        except Exception as exc:
            list_errors.append(f"{nqn}: {exc}")
            continue
        listed[nqn] = namespaces
        for ns in namespaces:
            if not _ns_is_degraded(ns):
                continue
            name = ns.get("rbd_image_name") or "?"
            degraded.append(
                f"{nqn} {name} nsid={ns.get('nsid')} "
                f"degraded={ns.get('degraded')} size={ns.get('rbd_image_size')} "
                f"bdev={ns.get('bdev_name')}"
            )
    return listed, degraded, list_errors


def _expected_ns_snapshot(gateway, subsystems):
    """Record every listed image name per NQN before the first restart."""
    snapshot = {}
    total = 0
    listed, degraded, errors = _scan_gateway_namespaces(gateway, subsystems)
    if errors:
        preview = "\n".join(errors[:10])
        raise RuntimeError(
            f"Could not snapshot namespaces before restart:\n{preview}"
        )
    if degraded:
        preview = "\n".join(degraded[:10])
        raise RuntimeError(
            f"Namespaces already degraded before restart:\n{preview}"
        )
    for nqn, namespaces in listed.items():
        names = {
            ns.get("rbd_image_name")
            for ns in namespaces
            if ns.get("rbd_image_name")
        }
        snapshot[nqn] = names
        total += len(names)
        LOG.info("%s: snapshot %s namespaces before restart", nqn, len(names))
    LOG.info("Namespace snapshot before restart: %s images across %s NQNs", total, len(snapshot))
    return snapshot, total


def _missing_expected(listed, expected):
    missing = []
    for nqn, names in expected.items():
        have = {
            ns.get("rbd_image_name")
            for ns in listed.get(nqn, [])
            if ns.get("rbd_image_name")
        }
        for name in sorted(names):
            if name not in have:
                missing.append(f"{nqn} {name}")
    return missing


def _wait_restart_namespaces(
    gateway, peer, expected, subsystems, clients, timeout, delay
):
    """Wait until the restarted GW lists every pre-restart namespace.

    Each poll lists the restarted GW and a peer. Fail if both lists show
    any degraded namespace, or if FIO stops / reports ``err!=0``.
    Return elapsed seconds until the restarted GW is complete.
    """
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
            "%s reopen attempt %s at %ss: restarted listed=%s/%s degraded=%s "
            "list_errors=%s; peer %s listed=%s degraded=%s list_errors=%s",
            host,
            attempt,
            elapsed,
            restarted_count,
            expected_total,
            len(restarted_degraded),
            len(restarted_errors),
            peer_host or "-",
            peer_count,
            len(peer_degraded),
            len(peer_errors),
        )
        if restarted_degraded:
            LOG.warning(
                "%s has %s degraded namespace(s):\n%s",
                host,
                len(restarted_degraded),
                "\n".join(restarted_degraded[:10]),
            )
        if peer_degraded:
            LOG.warning(
                "%s has %s degraded namespace(s):\n%s",
                peer_host,
                len(peer_degraded),
                "\n".join(peer_degraded[:10]),
            )

        restarted_clean = not restarted_degraded and not restarted_errors
        peer_clean = peer is None or (not peer_degraded and not peer_errors)
        if not restarted_clean and not peer_clean:
            raise RuntimeError(
                f"After restarting {host}, both {host} and {peer_host} show "
                f"degraded namespaces or ns-list failures. "
                f"{host} degraded={len(restarted_degraded)} "
                f"list_errors={restarted_errors[:5]!r}; "
                f"{peer_host} degraded={len(peer_degraded)} "
                f"list_errors={peer_errors[:5]!r}"
            )

        if not missing and not restarted_errors and not restarted_degraded:
            LOG.info(
                "%s: ns list has all %s namespaces %ss after gateway ready "
                "(attempt %s); peer %s also has no degraded NS",
                host,
                expected_total,
                elapsed,
                attempt,
                peer_host or "-",
            )
            return elapsed

        if time.time() >= deadline:
            preview = "\n".join(missing[:20])
            extra = f"\n... and {len(missing) - 20} more" if len(missing) > 20 else ""
            raise RuntimeError(
                f"{host}: ns list did not return all {expected_total} namespaces "
                f"after {elapsed}s ({expected_total - last_missing}/{expected_total} "
                f"listed, {len(restarted_degraded)} degraded):\n{preview}{extra}"
            )
        time.sleep(delay)


def _fmt_seconds(seconds):
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _log_reopen_summary(timings):
    """Log per-gateway namespace reopen timings collected during the run."""
    if not timings:
        LOG.info("No gateway restart timings to summarize")
        return
    header = (
        f"{'gateway':<12} {'pid_ready':>10} {'ns_all':>10} "
        f"{'total':>10} {'ns':>8} {'result':<8}"
    )
    lines = [
        "========== KMIP GW restart NS reopen summary ==========",
        header,
        "-" * len(header),
    ]
    for item in timings:
        lines.append(
            f"{item['host']:<12} {_fmt_seconds(item['pid_ready_s']):>10} "
            f"{_fmt_seconds(item['ns_usable_s']):>10} "
            f"{_fmt_seconds(item['total_s']):>10} "
            f"{item['parents']:>8} {item['result']:<8}"
        )
    lines.append("=" * len(header))
    lines.append(
        "pid_ready = process back after restart; "
        "ns_all = restarted GW ns list contains every pre-restart namespace; "
        "total = restart command until ns list is complete"
    )
    LOG.info("\n%s", "\n".join(lines))


def _wait_pid_changed(gateway, before, timeout, delay):
    """Wait until systemd MainPID/timestamp change after ``orch daemon restart``.

    ``ceph orch daemon restart`` returns as soon as the restart is scheduled.
    ``gateway info`` can still succeed against the old process, so PID is the
    signal that the unit actually bounced.
    """
    host = gateway.node.hostname
    deadline = time.time() + timeout
    last = before
    while True:
        try:
            last = _gw_unit_identity(gateway, require_running=False)
        except Exception as exc:
            LOG.info("%s: unit identity while waiting for restart: %s", host, exc)
        else:
            pid = last.get("main_pid")
            state = last.get("active_state")
            ts = last.get("active_ts")
            if (
                pid
                and pid != "0"
                and pid != before["main_pid"]
                and ts
                and ts != "0"
                and ts != before.get("active_ts")
                and state == "active"
            ):
                LOG.info(
                    "%s: MainPID %s -> %s (state=%s) after orch restart",
                    host,
                    before["main_pid"],
                    pid,
                    state,
                )
                return last
            LOG.info(
                "%s: waiting for restart (pid=%s state=%s, was pid=%s)",
                host,
                pid,
                state,
                before["main_pid"],
            )
        if time.time() >= deadline:
            detail = (
                f"pid={last.get('main_pid')!r} state={last.get('active_state')!r}"
            )
            raise RuntimeError(
                f"{host}: MainPID {before['main_pid']} did not change within "
                f"{timeout}s after orch daemon restart ({detail})"
            )
        time.sleep(delay)


def _restart_one_gateway(
    nvme_service, gateway, expected_ns, subsystems, clients, config, timings
):
    """Restart one GW, verify FIO + dual ns list, wait until this GW lists all NS."""
    host = gateway.node.hostname
    peers = [gw for gw in nvme_service.gateways if gw is not gateway]
    peer = peers[0] if peers else None
    try:
        _refresh_gateway_ssh(gateway)
    except Exception as exc:
        LOG.warning("SSH refresh to %s before restart failed: %s", host, exc)
    before = _gw_unit_identity(gateway)
    started = time.time()
    expected_total = sum(len(names) for names in expected_ns.values())
    timing = {
        "host": host,
        "parents": expected_total,
        "pid_ready_s": 0,
        "ns_usable_s": 0,
        "total_s": 0,
        "result": "failed",
    }
    timings.append(timing)
    LOG.info("========== restart gateway %s pid=%s ==========", host, before["main_pid"])
    nvme_service.restart_daemon(gateway, wait_sec=0)
    try:
        after = _wait_pid_changed(
            gateway,
            before,
            timeout=int(config.get("gw_pid_timeout", DEFAULT_PID_CHANGE_TIMEOUT)),
            delay=int(config.get("gw_pid_delay", DEFAULT_PID_CHANGE_DELAY)),
        )
    except Exception:
        timing["total_s"] = int(time.time() - started)
        raise
    pid_ready_s = int(time.time() - started)
    timing["pid_ready_s"] = pid_ready_s
    LOG.info(
        "%s restarted %s -> %s in %ss",
        host,
        before["main_pid"],
        after["main_pid"],
        pid_ready_s,
    )
    _copy_kmip_certs_when_containers_ready([gateway.node])
    gateway.load_gateway_info(
        tries=int(config.get("gw_ready_tries", 24)),
        delay=int(config.get("gw_ready_delay", 10)),
    )
    _assert_fio_healthy(clients, f"immediately after {host} process is back")
    ns_started = time.time()
    try:
        ns_usable_s = _wait_restart_namespaces(
            gateway,
            peer,
            expected_ns,
            subsystems,
            clients,
            timeout=int(config.get("ns_reopen_timeout", DEFAULT_NS_REOPEN_TIMEOUT)),
            delay=int(config.get("ns_reopen_delay", 10)),
        )
    except Exception:
        timing["ns_usable_s"] = int(time.time() - ns_started)
        timing["total_s"] = int(time.time() - started)
        raise
    timing["ns_usable_s"] = ns_usable_s
    timing["total_s"] = int(time.time() - started)
    LOG.info(
        "%s ns list complete in %ss after gateway ready (%ss since restart)",
        host,
        ns_usable_s,
        timing["total_s"],
    )
    kmip_hits = _scan_and_save_kmip_errors(
        [gateway], time.time() - started + 5
    )
    if kmip_hits:
        raise RuntimeError(
            f"KMIP timeout/error after restarting {host}:\n" + "\n".join(kmip_hits)
        )
    _assert_fio_healthy(clients, f"after {host} ns list complete")
    timing["result"] = "ok"
    LOG.info("%s restart verified; FIO still running with err=0", host)


def _rolling_gateway_restarts(
    nvme_service, expected_ns, subsystems, clients, config, timings, stop_state
):
    """Restart each gateway in turn while FIO continues on the clients."""
    delay = int(config.get("gw_restart_delay", 30))
    after = int(config.get("io_after_runtime", 60))
    gateways = list(nvme_service.gateways)
    try:
        LOG.info(
            "Waiting %ss for FIO to be in-flight, then restarting %s gateways one at a time",
            delay,
            len(gateways),
        )
        time.sleep(delay)
        _assert_fio_healthy(clients, "before first gateway restart")
        for index, gateway in enumerate(gateways, start=1):
            LOG.info("Gateway restart %s/%s", index, len(gateways))
            _restart_one_gateway(
                nvme_service,
                gateway,
                expected_ns,
                subsystems,
                clients,
                config,
                timings,
            )
            others = [gw for gw in nvme_service.gateways if gw is not gateway]
            if others:
                _assert_gateways_ready(
                    others,
                    f"peers after {gateway.node.hostname} restart",
                    tries=int(config.get("gw_ready_tries", 24)),
                    delay=int(config.get("gw_ready_delay", 10)),
                )
        LOG.info("All gateways restarted; leaving FIO running for %ss more", after)
        time.sleep(after)
        _assert_fio_healthy(clients, "after all gateway restarts")
        stop_state["stopped"] = True
    finally:
        _stop_fio(clients)


def _fio_max_runtime(config):
    """Safety ceiling only; FIO is stopped when rolling restarts finish."""
    runtime = int(config.get("fio_max_runtime", DEFAULT_FIO_MAX_RUNTIME))
    LOG.info(
        "FIO safety-cap runtime %ss; job will be stopped after all gateway restarts",
        runtime,
    )
    return runtime


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Light IO on parent NS, rolling GW restart, fail on KMIP journal errors.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)
    started = time.time()
    timings = []
    stop_state = {"stopped": False}

    try:
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        for gw in nvme_service.gateways:
            gw.load_gateway_info()
        gateway = nvme_service.gateways[0]
        gateways = nvme_service.gateways
        clients = ceph_cluster.get_nodes(role="client")
        if not clients:
            raise ValueError("KMIP GW restart test requires a client node")
        if len(gateways) < 2:
            raise ValueError("KMIP GW restart test requires at least two gateways")

        subsystems = _existing_subsystems(gateway, config)
        if config.get("cleanup_orphan_images", True):
            LOG.info("Removing leftover orphan_ clone NS/images before GW restart")
            rbd_obj = _init_rbd(kwargs)
            _cleanup_orphan_images(gateway, rbd_obj, subsystems, config)
            _assert_gateways_ready(
                gateways,
                "after orphan cleanup",
                tries=int(config.get("gw_ready_tries", 24)),
                delay=int(config.get("gw_ready_delay", 10)),
            )

        host_nqns = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            initiator.disconnect_all()
            host_nqns[client.hostname] = initiator.initiator_nqn()
            LOG.info("Client %s host NQN %s", client.hostname, host_nqns[client.hostname])

        LOG.info("Reusing existing parent-NS masks; skipping host/ACL changes")
        records, assigned = _parent_ns_records(
            gateway, subsystems, clients, host_nqns
        )
        expected = int(
            config.get(
                "expected_parent_ns",
                int(config.get("subsystems", 32))
                * int(config.get("namespaces_per_subsystem", 16)),
            )
        )
        if len(records) != expected:
            raise RuntimeError(
                f"Expected {expected} encrypted parent namespaces, found {len(records)}"
            )
        expected_ns, expected_total = _expected_ns_snapshot(gateway, subsystems)

        mapped = _connect_and_map(
            gateways, subsystems, clients, assigned, config
        )
        runtime = _fio_max_runtime(config)
        _prepare_fio_jobs(mapped, config, runtime)

        errors = []
        with parallel(timeout=runtime + 600) as p:
            for client, _, _ in mapped.values():
                p.spawn(_run_restart_fio_job, client, PARENT_JOB)
            p.spawn(
                _rolling_gateway_restarts,
                nvme_service,
                expected_ns,
                subsystems,
                clients,
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
                        LOG.info("FIO exit %s after stop; treating as success", result)
                        continue
                    errors.append(result)
        if errors:
            raise RuntimeError(f"FIO {PARENT_JOB} failed with {errors}")
        io_errors = _fio_io_errors(clients)
        if io_errors:
            raise RuntimeError("FIO reported IO errors:\n" + "\n".join(io_errors))
        _log_reopen_summary(timings)
        LOG.info(
            "KMIP passphrase-fetch rolling GW restart passed: %s parents, "
            "%s total namespaces, no degraded NS on at least one GW per restart, "
            "FIO err=0, completed in %ss",
            len(records),
            expected_total,
            int(time.time() - started),
        )
        return 0
    except Exception as err:
        _log_reopen_summary(timings)
        LOG.exception("KMIP passphrase-fetch GW restart test failed: %s", err)
        return 1
    finally:
        try:
            for node in ceph_cluster.get_nodes(role="client"):
                NVMeInitiator(node).disconnect_all()
        except Exception as exc:
            LOG.warning("Cleanup after KMIP GW restart test: %s", exc)
