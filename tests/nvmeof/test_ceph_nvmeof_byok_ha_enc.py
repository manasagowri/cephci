"""HA failover/failback at scale with encrypted namespaces (HA ENC).

Reuses the existing encrypted parent namespaces (v1: 512). Cleans leftover
``orphan_`` clone NS/images, adds a second KMIP endpoint per subsystem
(same server name, standby IP), then:

* Scenario A: ``systemctl stop`` GW1, time ANA failover, start GW1, time
  failback + KMIP re-fetch while light FIO runs.
* Scenario B: stop KMIP-1 then GW1; FIO must continue via the standby
  endpoint; restore KMIP-1 and GW1 with the same failback checks.

FIO is one job per client (``fio_size`` default 1G) and is restarted
between scenarios. Fail if KMIP uuids cannot be mirrored onto the standby
before FIO starts.
"""

import csv
import json
import os
import re
import shutil
import tempfile
import threading
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    _assign_kmip_endpoints,
    _existing_subsystems,
    _init_rbd,
    _sorted_kmip_nodes,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _assert_gateways_ready,
    _cleanup_orphan_images,
    _copy_kmip_certs_when_containers_ready,
    _refresh_gateway_ssh,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import _ns_usable
from tests.nvmeof.test_ceph_nvmeof_byok_kmip_gw_restart import (
    FIO_STOP_EXIT_CODES,
    _assert_fio_running,
    _connect_and_map,
    _fio_opts,
    _fmt_seconds,
    _parent_ns_records,
    _scan_and_save_kmip_errors,
    _stop_fio,
    _write_light_fio_job,
)
from tests.nvmeof.workflows.byok_kmip import (
    DEFAULT_KMIP_CLI_IMAGE,
    DEFAULT_KMIP_IMAGE,
    KMIP_CONTAINER_NAME,
    _fetch_kmip_value,
    _kmip_objects,
    _kmip_value_matches,
    _wait_for_container,
    kmip_cli,
    kmip_server_name,
    load_passphrases_all,
    parse_kmip_uuid,
    short_hostname,
)
from tests.nvmeof.workflows.ha import HighAvailability
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import (
    ana_states,
    check_and_set_nvme_cli_image,
    check_gateway_availability,
    get_optimized_state,
)
from utility.log import Log
from utility.utils import log_json_dump

LOG = Log(__name__)

HA_ENC_JOB = "/tmp/kmip_ha_enc.fio"
HA_ENC_FIO_LOG = "/tmp/kmip_ha_enc.fio.log"
STANDBY_CONTAINER = "kmip-ha-standby"
DEFAULT_STANDBY_PORT = 5697
DEFAULT_FIO_MAX_RUNTIME = 28800
DEFAULT_FAILOVER_TIMEOUT = 300
DEFAULT_NS_REOPEN_TIMEOUT = 5400
FIO_ERR_RE = re.compile(r"err=(\d+)")
_IO_POLL_INTERVAL = 2  # seconds between IO-health polls inside _IoPauseTracker
KMIP_RETRIEVE_RE = re.compile(
    r"(Retrieved |Successfully retrieved|retrieved key|retrieve.*success)",
    re.IGNORECASE,
)
NS_LIST_RETRYABLE = (
    "unable to find a target",
    "failed to connect",
    "connection refused",
    "timed out",
    "timeout",
    "errno 60",
    "going down",
)


def _log_dump(data):
    try:
        return log_json_dump(data)
    except Exception:
        return str(data)


def _ana_id(gateway):
    return int(gateway.ana_group_id)


def _ana_name(gateway):
    return (gateway.ana_group or {}).get("name") or gateway.hostname


def _transient_ns_list(text):
    lowered = str(text or "").lower()
    return any(token in lowered for token in NS_LIST_RETRYABLE)


def _list_namespaces(gateway, nqn):
    """List namespaces for ``nqn``. Raise CommandFailed on a transient CLI miss."""
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
        raise CommandFailed(f"empty ns list for {nqn}")
    try:
        return json.loads(out).get("namespaces", []) or []
    except (json.JSONDecodeError, TypeError) as exc:
        raise CommandFailed(f"invalid ns list JSON for {nqn}: {exc}") from exc


def _parent_ns_records_retry(gateway, subsystems, clients, host_nqns, tries=8, delay=15):
    last = None
    for attempt in range(1, tries + 1):
        try:
            return _parent_ns_records(gateway, subsystems, clients, host_nqns)
        except Exception as exc:
            last = exc
            LOG.warning(
                "Parent NS inventory attempt %s/%s: %s", attempt, tries, exc
            )
            if attempt < tries:
                time.sleep(delay)
    raise RuntimeError(f"Could not inventory parent namespaces: {last}")


def _parent_has_encryption(ns):
    entries = ns.get("encryption_entries") or []
    if not entries:
        return False
    return all(
        str(item.get("format") or "").strip() and str(item.get("key_id") or "").strip()
        for item in entries
    )


def _parent_ns_errors(gateway, records):
    """Return usability errors for parent NS, including missing encryption_entries.

    Transient ``ns list`` failures are returned as errors so the wait loop
    retries instead of aborting the whole test (H9X1Q6).
    """
    by_nqn = {}
    for record in records:
        by_nqn.setdefault(record["nqn"], []).append(record)
    errors = []
    for nqn, batch in by_nqn.items():
        try:
            listed_ns = _list_namespaces(gateway, nqn)
        except Exception as exc:
            errors.append(f"{nqn}: ns list failed: {exc}")
            continue
        listed = {}
        for ns in listed_ns:
            name = ns.get("rbd_image_name")
            if name:
                listed[name] = ns
        for record in batch:
            ns = listed.get(record["image"])
            if not ns:
                errors.append(f"{nqn} {record['image']}: missing from ns list")
                continue
            if not _ns_usable(ns):
                errors.append(
                    f"{nqn} {record['image']}: not usable "
                    f"(degraded={ns.get('degraded')} size={ns.get('rbd_image_size')} "
                    f"bdev={ns.get('bdev_name')})"
                )
                continue
            if not _parent_has_encryption(ns):
                errors.append(
                    f"{nqn} {record['image']}: missing encryption_entries "
                    f"({ns.get('encryption_entries')})"
                )
    return errors


def _wait_parents_usable(gateway, records, timeout, delay):
    """Wait until every parent NS is usable with encryption_entries."""
    host = gateway.node.hostname
    started = time.time()
    deadline = started + timeout
    attempt = 0
    last_errors = []
    while True:
        attempt += 1
        last_errors = _parent_ns_errors(gateway, records)
        elapsed = int(time.time() - started)
        usable = len(records) - len(last_errors)
        if not last_errors:
            LOG.info(
                "%s: all %s parent namespaces usable with encryption_entries "
                "%ss after wait start (attempt %s)",
                host,
                len(records),
                elapsed,
                attempt,
            )
            return elapsed
        LOG.warning(
            "%s: %s/%s parent namespaces usable %ss after wait start (attempt %s)",
            host,
            usable,
            len(records),
            elapsed,
            attempt,
        )
        if time.time() >= deadline:
            preview = "\n".join(last_errors[:20])
            extra = (
                f"\n... and {len(last_errors) - 20} more"
                if len(last_errors) > 20
                else ""
            )
            raise RuntimeError(
                f"{host}: parent namespaces did not re-open after {elapsed}s "
                f"({usable}/{len(records)} usable):\n{preview}{extra}"
            )
        time.sleep(delay)


class _IoPauseTracker:
    """Background thread that timestamps when IO pauses and when it recovers.

    Detection heuristic (polled every ``_IO_POLL_INTERVAL`` seconds):
    * IO is considered *unhealthy* when FIO has exited on any client OR when
      the FIO log contains a non-zero ``err=`` line that was not present at the
      previous poll.
    * IO is considered *healthy* again when all clients have a running FIO
      process and no new ``err!=0`` lines appear for two consecutive polls.

    Usage::

        tracker = _IoPauseTracker(clients, HA_ENC_FIO_LOG)
        tracker.start()
        # ... run scenario ...
        pause_s = tracker.stop()   # returns None if no pause was detected
    """

    def __init__(self, clients, fio_log):
        self._clients = clients
        self._fio_log = fio_log
        self._stop_evt = threading.Event()
        self._thread = None
        # wall-clock timestamps (float); None = not yet observed
        self.paused_at = None
        self.resumed_at = None
        # last known err-line count per client to detect new errors
        self._prev_err_counts = {c.hostname: 0 for c in clients}

    # ------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the background thread to stop and return io_pause_s (or None)."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=_IO_POLL_INTERVAL * 4)
        if self.paused_at is None:
            return None
        if self.resumed_at is None:
            # FIO never fully recovered before stop() was called — use now
            pause_s = int(time.time() - self.paused_at)
        else:
            pause_s = int(self.resumed_at - self.paused_at)
        return max(pause_s, 0)

    # ------------------------------------------------------------------
    def _err_count(self, client):
        """Return count of non-zero err= lines seen so far in the FIO log."""
        out, _ = client.exec_command(
            cmd=f"grep -c 'err=[^0]' {self._fio_log} || echo 0",
            sudo=True,
            check_ec=False,
        )
        try:
            return int((out or "0").strip())
        except ValueError:
            return 0

    def _fio_alive(self, client):
        out, _ = client.exec_command(
            cmd="pgrep -ax fio || true", sudo=True, check_ec=False
        )
        return bool((out or "").strip())

    def _all_healthy(self):
        """True when every client has FIO running and no new err= lines."""
        for client in self._clients:
            if not self._fio_alive(client):
                return False
            new_count = self._err_count(client)
            if new_count > self._prev_err_counts.get(client.hostname, 0):
                self._prev_err_counts[client.hostname] = new_count
                return False
        return True

    def _any_unhealthy(self):
        """True as soon as any client is unhealthy."""
        for client in self._clients:
            if not self._fio_alive(client):
                return True
            new_count = self._err_count(client)
            if new_count > self._prev_err_counts.get(client.hostname, 0):
                self._prev_err_counts[client.hostname] = new_count
                return True
        return False

    def _run(self):
        healthy_streak = 0  # consecutive healthy polls needed to declare resumed
        while not self._stop_evt.is_set():
            try:
                if self.paused_at is None:
                    # waiting for first sign of IO trouble
                    if self._any_unhealthy():
                        self.paused_at = time.time()
                        LOG.info(
                            "IO pause detected at %.1f", self.paused_at
                        )
                        healthy_streak = 0
                elif self.resumed_at is None:
                    # IO is paused; waiting for recovery
                    if self._all_healthy():
                        healthy_streak += 1
                        if healthy_streak >= 2:
                            self.resumed_at = time.time()
                            pause_s = int(self.resumed_at - self.paused_at)
                            LOG.info(
                                "IO resumed at %.1f (pause ~%ss)",
                                self.resumed_at,
                                pause_s,
                            )
                    else:
                        healthy_streak = 0
            except Exception as exc:
                LOG.debug("IoPauseTracker poll error: %s", exc)
            self._stop_evt.wait(timeout=_IO_POLL_INTERVAL)


def _prepare_ha_fio(mapped, config, runtime):
    opts = _fio_opts(config, runtime)
    for client, _, paths in mapped.values():
        if not paths:
            raise RuntimeError(f"No parent devices on {client.hostname}")
        _write_light_fio_job(client, HA_ENC_JOB, paths, opts)


def _run_ha_fio_job(node, job_path):
    """Run FIO with CLI ``--output`` (not valid as a job-file key)."""
    LOG.info("Starting FIO %s on %s", job_path, node.hostname)
    return node.exec_command(
        cmd=f"fio --output={HA_ENC_FIO_LOG} --status-interval=30 {job_path}",
        sudo=True,
        long_running=True,
        timeout="notimeout",
    )


def _fio_io_errors(clients):
    """Parse FIO ``err=`` from the per-client log after the job stops."""
    errors = []
    for client in clients:
        out, _ = client.exec_command(
            cmd=f"grep -E 'err=' {HA_ENC_FIO_LOG} || true",
            sudo=True,
            check_ec=False,
        )
        text = (out or "").strip()
        if not text:
            LOG.warning("%s: no err= lines in %s", client.hostname, HA_ENC_FIO_LOG)
            continue
        for line in text.splitlines():
            match = FIO_ERR_RE.search(line)
            if match and int(match.group(1)) != 0:
                errors.append(f"{client.hostname}: {line.strip()}")
        LOG.info("%s FIO err= lines:\n%s", client.hostname, text)
    return errors


def _assert_cluster_health(orch, label):
    out, err = orch.shell(args=["ceph", "health"])
    text = f"{out or ''}\n{err or ''}".strip()
    LOG.info("%s: ceph health: %s", label, text)
    if "HEALTH_ERR" in text:
        raise RuntimeError(f"{label}: cluster health is HEALTH_ERR: {text}")


def _journal_text(gateway, since_seconds):
    unit = gateway.system_unit_id
    since = max(int(since_seconds), 1)
    out, _ = gateway.node.exec_command(
        cmd=(
            f"journalctl -u {unit} --since '{since} seconds ago' --no-pager || true"
        ),
        sudo=True,
        check_ec=False,
    )
    return out or ""


def _assert_kmip_refetch(gateway, since_seconds):
    """Fail on KMIP journal errors; require a retrieve success after failback."""
    host = gateway.node.hostname
    hits = _scan_and_save_kmip_errors([gateway], since_seconds)
    if hits:
        raise RuntimeError(
            f"KMIP timeout/error after failback of {host}:\n" + "\n".join(hits)
        )
    journal = _journal_text(gateway, since_seconds)
    if not KMIP_RETRIEVE_RE.search(journal):
        raise RuntimeError(
            f"{host}: no KMIP retrieve success in gateway journal after failback"
        )
    LOG.info("%s: KMIP retrieve confirmed in gateway journal", host)


def _int_uuid(uuid):
    try:
        return int(str(uuid).strip())
    except (TypeError, ValueError):
        return None


def _copy_cert_dir(src_node, src_dir, dst_node, dst_dir):
    """Copy every file in ``src_dir`` from ``src_node`` onto ``dst_node``."""
    out, _ = src_node.exec_command(cmd=f"ls -1 {src_dir}", sudo=True)
    names = [name.strip() for name in (out or "").splitlines() if name.strip()]
    if not names:
        raise RuntimeError(
            f"No KMIP cert files in {short_hostname(src_node)}:{src_dir}"
        )
    dst_node.exec_command(cmd=f"rm -rf {dst_dir} && mkdir -p {dst_dir}", sudo=True)
    tmp = tempfile.mkdtemp(prefix="kmip-ha-certs-")
    try:
        for name in names:
            local = os.path.join(tmp, name)
            src_node.download_file(
                src=f"{src_dir}/{name}", dst=local, sudo=True
            )
            dst_node.upload_file(src=local, dst=f"{dst_dir}/{name}", sudo=True)
        dst_node.exec_command(cmd=f"chmod 644 {dst_dir}/*", sudo=True, check_ec=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    LOG.info(
        "Copied %s KMIP cert file(s) %s:%s -> %s:%s",
        len(names),
        short_hostname(src_node),
        src_dir,
        short_hostname(dst_node),
        dst_dir,
    )


def _export_container_certs(node, container_name, dest_dir):
    node.exec_command(cmd=f"rm -rf {dest_dir} && mkdir -p {dest_dir}", sudo=True)
    node.exec_command(
        cmd=f"podman cp {container_name}:/kmip/certs/. {dest_dir}/",
        sudo=True,
    )


def _open_standby_port(node, port):
    node.exec_command(
        cmd=(
            f"firewall-cmd --add-port={port}/tcp || true; "
            f"firewall-cmd --add-port={port}/tcp --permanent || true"
        ),
        sudo=True,
        check_ec=False,
    )


def _create_passphrase(node, spec, cli_image, port, certs_dir, hostname, uuid=None):
    extra = f" --uuid {uuid}" if uuid is not None else ""
    out, _ = kmip_cli(
        node,
        f'create-passphrase --name "{spec["name"]}" '
        f'--value "{spec["value"]}"{extra}',
        cli_image=cli_image,
        hostname=hostname,
        port=port,
        certs_dir=certs_dir,
    )
    return parse_kmip_uuid(out)


def _needed_uuid_matches(node, needed, cli_image, port, certs_dir, hostname):
    """True when every primary passphrase uuid exists with a matching value."""
    for key, spec in needed.items():
        try:
            value = _fetch_kmip_value(
                node,
                spec["uuid"],
                cli_image=cli_image,
                port=port,
                certs_dir=certs_dir,
                hostname=hostname,
            )
        except Exception as exc:
            LOG.info(
                "%s: uuid %s (%s) missing on standby: %s",
                short_hostname(node),
                spec["uuid"],
                key,
                exc,
            )
            return False
        if not _kmip_value_matches(value, spec["value"]):
            LOG.info(
                "%s: uuid %s (%s) value mismatch on standby",
                short_hostname(node),
                spec["uuid"],
                key,
            )
            return False
    return True


def _seed_replica_keys(node, needed, cli_image, port, certs_dir, hostname):
    """Create primary passphrases on an empty replica with matching uuids."""
    by_int = {}
    others = []
    for spec in needed.values():
        number = _int_uuid(spec["uuid"])
        if number is None:
            others.append(spec)
        else:
            by_int[number] = spec

    for spec in others:
        uuid = _create_passphrase(
            node, spec, cli_image, port, certs_dir, hostname, uuid=spec["uuid"]
        )
        if str(uuid) != str(spec["uuid"]):
            raise RuntimeError(
                f"{short_hostname(node)}: created uuid {uuid} != required {spec['uuid']} "
                f"for {spec['name']}"
            )

    if not by_int:
        return
    max_uuid = max(by_int)
    for index in range(1, max_uuid + 1):
        if index in by_int:
            spec = by_int[index]
            try:
                uuid = _create_passphrase(
                    node,
                    spec,
                    cli_image,
                    port,
                    certs_dir,
                    hostname,
                    uuid=index,
                )
            except Exception as exc:
                LOG.info(
                    "create-passphrase --uuid %s failed (%s); trying sequential create",
                    index,
                    exc,
                )
                uuid = _create_passphrase(
                    node, spec, cli_image, port, certs_dir, hostname
                )
        else:
            filler = {
                "name": f"ha_enc_filler_{index}",
                "value": f"filler_{short_hostname(node)}_{index}",
            }
            uuid = _create_passphrase(
                node, filler, cli_image, port, certs_dir, hostname
            )
        created = _int_uuid(uuid)
        if created != index:
            raise RuntimeError(
                f"{short_hostname(node)}:{port} expected uuid {index}, created {uuid}. "
                "Dummy KMIP cannot mirror key-ids onto the HA standby."
            )


def _deploy_kmip_replica(primary, standby, needed, config):
    """Run a TLS-identical KMIP replica of ``primary`` on ``standby``:standby_port."""
    cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)
    image = config.get("kmip_image", DEFAULT_KMIP_IMAGE)
    port = int(config.get("kmip_standby_port", DEFAULT_STANDBY_PORT))
    name = kmip_server_name(primary)
    export_dir = f"/tmp/kmip-export-{name}"
    cert_dir = f"/tmp/kmip-ha-certs-{name}"
    hostname = "127.0.0.1"

    if _needed_uuid_matches(
        standby, needed, cli_image, port, cert_dir, hostname
    ):
        LOG.info(
            "Reusing KMIP HA replica %s on %s:%s for %s",
            STANDBY_CONTAINER,
            short_hostname(standby),
            port,
            name,
        )
        return port

    LOG.info(
        "Deploying KMIP HA replica of %s on %s:%s (same TLS name %s)",
        short_hostname(primary),
        short_hostname(standby),
        port,
        name,
    )
    _export_container_certs(primary, KMIP_CONTAINER_NAME, export_dir)
    _copy_cert_dir(primary, export_dir, standby, cert_dir)
    _open_standby_port(standby, port)
    standby.exec_command(
        cmd=f"podman rm -f {STANDBY_CONTAINER}", sudo=True, check_ec=False
    )
    standby.exec_command(
        cmd=(
            f"podman run -d --name {STANDBY_CONTAINER} "
            f"-p {port}:5696 "
            f"-v {cert_dir}:/kmip/certs:Z "
            f"{image}"
        ),
        sudo=True,
    )
    _wait_for_container(standby, STANDBY_CONTAINER)
    time.sleep(5)
    _seed_replica_keys(standby, needed, cli_image, port, cert_dir, hostname)
    if not _needed_uuid_matches(
        standby, needed, cli_image, port, cert_dir, hostname
    ):
        listed = _kmip_objects(
            standby,
            cli_image=cli_image,
            port=port,
            certs_dir=cert_dir,
            hostname=hostname,
        )
        raise RuntimeError(
            f"KMIP HA replica on {short_hostname(standby)}:{port} does not "
            f"mirror {name} key uuids. listed="
            f"{[(item.get('uuid'), item.get('name')) for item in listed]}"
        )
    LOG.info(
        "KMIP HA replica ready: %s -> %s:%s name=%s",
        short_hostname(primary),
        short_hostname(standby),
        port,
        name,
    )
    return port


def _listed_endpoint_text(gateway, nqn):
    out, err = gateway.subsystem.list_kmip_server_endpoints(
        **{"base_cmd_args": {"format": "json"}, "args": {"nqn": nqn}}
    )
    return f"{out or ''}\n{err or ''}"


def _add_standby_endpoint(gateway, nqn, name, address, port):
    listed = _listed_endpoint_text(gateway, nqn)
    if address in listed and str(port) in listed:
        LOG.info(
            "KMIP standby endpoint %s %s:%s already listed for %s",
            name,
            address,
            port,
            nqn,
        )
        return
    LOG.info(
        "add_kmip_server_endpoint %s %s %s %s (standby, same name)",
        nqn,
        name,
        address,
        port,
    )
    try:
        gateway.subsystem.add_kmip_server_endpoint(
            **{"positional_args": [nqn, name, address, port]}
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to add KMIP standby endpoint {name} {address}:{port} "
            f"on {nqn} (same server name as the primary). CLI error: {exc}"
        ) from exc
    listed = _listed_endpoint_text(gateway, nqn)
    if address not in listed:
        raise RuntimeError(
            f"KMIP standby endpoint {name} {address}:{port} not listed for "
            f"{nqn}: {listed}"
        )


def _ensure_dual_kmip_endpoints(gateway, subsystems, kmip_nodes, config):
    """Pair KMIP i with i+1, deploy a uuid-mirrored replica, add second endpoint."""
    if len(kmip_nodes) < 2:
        raise RuntimeError("HA ENC dual KMIP endpoints need at least two KMIP nodes")
    cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)
    passphrases_by_node = load_passphrases_all(kmip_nodes, cli_image=cli_image)
    pairs = []
    for index, primary in enumerate(kmip_nodes):
        standby = kmip_nodes[(index + 1) % len(kmip_nodes)]
        needed = passphrases_by_node.get(primary)
        if not needed:
            host = short_hostname(primary)
            for node, keys in passphrases_by_node.items():
                if short_hostname(node) == host:
                    needed = keys
                    break
        if not needed:
            raise RuntimeError(
                f"No KMIP passphrases loaded for {short_hostname(primary)}"
            )
        port = _deploy_kmip_replica(primary, standby, needed, config)
        name = kmip_server_name(primary)
        owned = [sub for sub in subsystems if sub.get("kmip_node") is primary]
        if not owned:
            owned = [
                sub
                for sub in subsystems
                if short_hostname(sub.get("kmip_node")) == short_hostname(primary)
            ]
        for sub in owned:
            _add_standby_endpoint(
                gateway, sub["group_nqn"], name, standby.ip_address, port
            )
        pairs.append(
            {
                "primary": short_hostname(primary),
                "standby": short_hostname(standby),
                "name": name,
                "port": port,
                "subsystems": len(owned),
            }
        )
        LOG.info(
            "Dual KMIP: %s (%s) + %s:%s on %s subsystem(s)",
            pairs[-1]["primary"],
            name,
            pairs[-1]["standby"],
            port,
            len(owned),
        )
    return pairs, passphrases_by_node


def _wait_ana_failover(ha, gateway, timeout):
    """Wait until GW1 is UNAVAILABLE and its ANA group is ACTIVE on exactly one peer."""
    hostname = gateway.hostname
    ana_id = _ana_id(gateway)
    failed_name = _ana_name(gateway)
    started = time.time()
    deadline = started + timeout
    last = None
    while time.time() < deadline:
        try:
            states = ana_states(ha.nvme_service, ha.orch, ha.gateway_group)
            unavailable = check_gateway_availability(
                ha.nvme_service,
                ana_id,
                ha.orch,
                state="UNAVAILABLE",
                anastates=states,
            )
            active = get_optimized_state(ha.nvme_service, ha.orch, ana_id)
            elapsed = int(time.time() - started)
            LOG.info(
                "%s failover wait %ss: unavailable=%s optimized=%s",
                hostname,
                elapsed,
                unavailable,
                _log_dump(active),
            )
            if unavailable and active and len(active) == 1:
                winner = list(active[0])[0]
                if winner == failed_name:
                    last = f"{hostname} still optimized on itself"
                else:
                    LOG.info(
                        "%s ANA failover complete in %ss; optimized on %s",
                        hostname,
                        elapsed,
                        winner,
                    )
                    return elapsed, winner
            if len(active) > 1:
                raise RuntimeError(
                    f"{hostname}: more than one optimized path during failover: "
                    f"{_log_dump(active)}"
                )
            last = f"unavailable={unavailable} active={_log_dump(active)}"
        except RuntimeError:
            raise
        except Exception as exc:
            last = str(exc)
            LOG.warning("%s failover poll error: %s", hostname, exc)
        time.sleep(5)
    elapsed = int(time.time() - started)
    raise TimeoutError(
        f"{hostname}: ANA failover did not complete in {elapsed}s ({last})"
    )


def _wait_ana_failback(ha, gateway, timeout):
    """Wait until GW1 is AVAILABLE and optimized for its own ANA group."""
    hostname = gateway.hostname
    ana_id = _ana_id(gateway)
    restored_name = _ana_name(gateway)
    started = time.time()
    deadline = started + timeout
    last = None
    while time.time() < deadline:
        try:
            states = ana_states(ha.nvme_service, ha.orch, ha.gateway_group)
            available = check_gateway_availability(
                ha.nvme_service,
                ana_id,
                ha.orch,
                state="AVAILABLE",
                anastates=states,
            )
            active = get_optimized_state(ha.nvme_service, ha.orch, ana_id)
            elapsed = int(time.time() - started)
            LOG.info(
                "%s failback wait %ss: available=%s optimized=%s",
                hostname,
                elapsed,
                available,
                _log_dump(active),
            )
            if available and active and len(active) == 1:
                winner = list(active[0])[0]
                if restored_name in active[0] or winner == restored_name:
                    LOG.info(
                        "%s reclaimed ANA-optimized in %ss", hostname, elapsed
                    )
                    return elapsed
            if len(active) > 1:
                last = f"more than one optimized path: {_log_dump(active)}"
            else:
                last = f"available={available} active={_log_dump(active)}"
        except Exception as exc:
            last = str(exc)
            LOG.warning("%s failback poll error: %s", hostname, exc)
        time.sleep(10)
    elapsed = int(time.time() - started)
    raise TimeoutError(
        f"{hostname}: ANA failback did not complete in {elapsed}s ({last})"
    )


def _stop_gateway(ha, gateway):
    try:
        _refresh_gateway_ssh(gateway)
    except Exception as exc:
        LOG.warning("SSH refresh to %s before stop failed: %s", gateway.hostname, exc)
    LOG.info("systemctl stop NVMeoF on %s", gateway.hostname)
    if not ha.system_control(gateway, "stop", wait_for_active_state=False):
        raise RuntimeError(f"{gateway.hostname}: systemctl stop did not reach inactive")


def _start_gateway(ha, gateway, config):
    try:
        _refresh_gateway_ssh(gateway)
    except Exception as exc:
        LOG.warning("SSH refresh to %s before start failed: %s", gateway.hostname, exc)
    LOG.info("systemctl start NVMeoF on %s", gateway.hostname)
    if not ha.system_control(gateway, "start", wait_for_active_state=True):
        raise RuntimeError(f"{gateway.hostname}: systemctl start did not reach active")
    _copy_kmip_certs_when_containers_ready([gateway.node])
    gateway.load_gateway_info(
        tries=int(config.get("gw_ready_tries", 24)),
        delay=int(config.get("gw_ready_delay", 10)),
    )


def _stop_kmip(node):
    LOG.info("Stopping KMIP container %s on %s", KMIP_CONTAINER_NAME, short_hostname(node))
    node.exec_command(cmd=f"podman stop {KMIP_CONTAINER_NAME}", sudo=True)


def _start_kmip(node):
    LOG.info("Starting KMIP container %s on %s", KMIP_CONTAINER_NAME, short_hostname(node))
    node.exec_command(cmd=f"podman start {KMIP_CONTAINER_NAME}", sudo=True)
    _wait_for_container(node, KMIP_CONTAINER_NAME)
    time.sleep(3)


def _restore_after_failure(stop_state, ha, gateway, config, kmip_node=None):
    """Best-effort restore GW1/KMIP-1 when a scenario fails mid-flight."""
    if stop_state.get("stopped"):
        return
    if kmip_node is not None:
        try:
            _start_kmip(kmip_node)
        except Exception as exc:
            LOG.warning("Failed to restart KMIP-1 after scenario error: %s", exc)
    try:
        _start_gateway(ha, gateway, config)
    except Exception as exc:
        LOG.warning(
            "Failed to restart %s after scenario error: %s", gateway.hostname, exc
        )


def _run_with_fio(mapped, clients, config, worker, *args):
    """Start light FIO, run ``worker``, stop FIO, require err=0 / clean stop.

    An ``_IoPauseTracker`` is started alongside the worker.  When the worker
    function is one of the scenario helpers it must accept a ``tracker``
    keyword argument so it can call ``tracker.stop()`` to collect the pause
    duration before appending to ``timings``.
    """
    runtime = int(config.get("fio_max_runtime", DEFAULT_FIO_MAX_RUNTIME))
    _prepare_ha_fio(mapped, config, runtime)
    stop_state = {"stopped": False}
    tracker = _IoPauseTracker(clients, HA_ENC_FIO_LOG)
    errors = []
    try:
        tracker.start()
        with parallel(timeout=runtime + 600) as p:
            for client, _, _ in mapped.values():
                p.spawn(_run_ha_fio_job, client, HA_ENC_JOB)
            p.spawn(worker, clients, stop_state, *args, tracker=tracker)
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
            raise RuntimeError(f"FIO {HA_ENC_JOB} failed with {errors}")
        io_errors = _fio_io_errors(clients)
        if io_errors:
            raise RuntimeError("FIO reported IO errors:\n" + "\n".join(io_errors))
    except Exception:
        tracker.stop()
        _stop_fio(clients)
        raise


def _scenario_a(clients, stop_state, ha, gateway, records, config, timings, tracker=None):
    delay = int(config.get("gw_failover_delay", 30))
    failover_timeout = int(config.get("failover_timeout", DEFAULT_FAILOVER_TIMEOUT))
    failback_timeout = int(config.get("ns_reopen_timeout", DEFAULT_NS_REOPEN_TIMEOUT))
    ns_delay = int(config.get("ns_reopen_delay", 10))
    try:
        LOG.info(
            "Scenario A: waiting %ss for FIO, then systemctl stop %s",
            delay,
            gateway.hostname,
        )
        time.sleep(delay)
        _assert_fio_running(clients, "before GW1 failover")
        started = time.time()
        _stop_gateway(ha, gateway)
        failover_s, winner = _wait_ana_failover(ha, gateway, failover_timeout)
        _assert_fio_running(clients, "after GW1 ANA failover")
        failback_started = time.time()
        _start_gateway(ha, gateway, config)
        _wait_ana_failback(ha, gateway, failback_timeout)
        _wait_parents_usable(gateway, records, failback_timeout, ns_delay)
        _assert_kmip_refetch(gateway, time.time() - failback_started + 5)
        _assert_fio_running(clients, "after GW1 failback")
        _assert_cluster_health(ha.orch, "after scenario A")
        failback_s = int(time.time() - failback_started)
        io_pause_s = tracker.stop() if tracker else None
        timings.append(
            {
                "scenario": "A",
                "failover_s": failover_s,
                "failback_s": failback_s,
                "io_pause_s": io_pause_s,
                "optimized_on": winner,
                "result": "ok",
            }
        )
        LOG.info(
            "Scenario A passed: failover %s on %s, failback %s, IO pause %s (total %s)",
            _fmt_seconds(failover_s),
            winner,
            _fmt_seconds(failback_s),
            _fmt_seconds(io_pause_s) if io_pause_s is not None else "none",
            _fmt_seconds(int(time.time() - started)),
        )
        stop_state["stopped"] = True
    finally:
        _restore_after_failure(stop_state, ha, gateway, config)
        _stop_fio(clients)


def _scenario_b(
    clients, stop_state, ha, gateway, records, kmip1, config, timings, tracker=None
):
    delay = int(config.get("gw_failover_delay", 30))
    failover_timeout = int(config.get("failover_timeout", DEFAULT_FAILOVER_TIMEOUT))
    failback_timeout = int(config.get("ns_reopen_timeout", DEFAULT_NS_REOPEN_TIMEOUT))
    ns_delay = int(config.get("ns_reopen_delay", 10))
    kmip_started = False
    try:
        LOG.info(
            "Scenario B: waiting %ss for FIO, then stop KMIP-1 %s and GW1 %s",
            delay,
            short_hostname(kmip1),
            gateway.hostname,
        )
        time.sleep(delay)
        _assert_fio_running(clients, "before KMIP-1 + GW1 failover")
        started = time.time()
        _stop_kmip(kmip1)
        _stop_gateway(ha, gateway)
        failover_s, winner = _wait_ana_failover(ha, gateway, failover_timeout)
        _assert_fio_running(clients, "after KMIP-1 + GW1 failover")
        failback_started = time.time()
        _start_kmip(kmip1)
        kmip_started = True
        _start_gateway(ha, gateway, config)
        _wait_ana_failback(ha, gateway, failback_timeout)
        _wait_parents_usable(gateway, records, failback_timeout, ns_delay)
        _assert_kmip_refetch(gateway, time.time() - failback_started + 5)
        _assert_fio_running(clients, "after KMIP-1 + GW1 failback")
        _assert_cluster_health(ha.orch, "after scenario B")
        failback_s = int(time.time() - failback_started)
        io_pause_s = tracker.stop() if tracker else None
        timings.append(
            {
                "scenario": "B",
                "failover_s": failover_s,
                "failback_s": failback_s,
                "io_pause_s": io_pause_s,
                "optimized_on": winner,
                "result": "ok",
            }
        )
        LOG.info(
            "Scenario B passed: failover %s on %s, failback %s, IO pause %s (total %s)",
            _fmt_seconds(failover_s),
            winner,
            _fmt_seconds(failback_s),
            _fmt_seconds(io_pause_s) if io_pause_s is not None else "none",
            _fmt_seconds(int(time.time() - started)),
        )
        stop_state["stopped"] = True
    finally:
        _restore_after_failure(
            stop_state, ha, gateway, config, kmip_node=None if kmip_started else kmip1
        )
        _stop_fio(clients)


def _log_timing_table(timings, pairs):
    header = (
        f"{'scenario':<10} {'failover':>10} {'failback':>10} "
        f"{'io_pause':>10} {'optimized_on':<16} {'result':<8}"
    )
    lines = [
        "========== HA ENC failover/failback summary ==========",
        header,
        "-" * len(header),
    ]
    if not timings:
        lines.append("(no scenario timings collected)")
    for item in timings:
        pause = item.get("io_pause_s")
        pause_str = _fmt_seconds(pause) if pause is not None else "-"
        lines.append(
            f"{item['scenario']:<10} {_fmt_seconds(item['failover_s']):>10} "
            f"{_fmt_seconds(item['failback_s']):>10} "
            f"{pause_str:>10} "
            f"{str(item.get('optimized_on') or '-'):<16} {item['result']:<8}"
        )
    lines.append("=" * len(header))
    if pairs:
        lines.append("KMIP dual endpoints (same name, standby IP:port):")
        for pair in pairs:
            lines.append(
                f"  {pair['name']}: {pair['primary']} + {pair['standby']}:"
                f"{pair['port']} ({pair['subsystems']} subsystem(s))"
            )
    LOG.info("\n%s", "\n".join(lines))


HA_ENC_CSV = "/tmp/ha_enc_timings.csv"
_CSV_FIELDS = [
    "scenario",
    "failover_s",
    "failback_s",
    "io_pause_s",
    "optimized_on",
    "result",
]


def _write_timing_csv(timings):
    """Write per-scenario timings to a local CSV file for post-run analysis."""
    try:
        with open(HA_ENC_CSV, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=_CSV_FIELDS, extrasaction="ignore"
            )
            writer.writeheader()
            for item in timings:
                writer.writerow(item)
        LOG.info("HA ENC timings written to %s", HA_ENC_CSV)
    except Exception as exc:
        LOG.warning("Could not write timing CSV: %s", exc)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """HA ENC v1: ANA failover/failback on existing encrypted parent NS.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)
    started = time.time()
    timings = []
    pairs = []

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
            raise ValueError("HA ENC requires a client node")
        if len(gateways) < 2:
            raise ValueError("HA ENC requires at least two gateways")

        ha_config = dict(config)
        ha_config["nvme_service"] = nvme_service
        ha = HighAvailability(ceph_cluster, config["gw_nodes"], **ha_config)
        ha.gateways = gateways

        subsystems = _existing_subsystems(gateway, config)
        kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
        _assign_kmip_endpoints(
            subsystems,
            kmip_nodes,
            int(config.get("subsystems_per_kmip", 2)),
        )

        if config.get("cleanup_orphan_images", True):
            LOG.info("HA ENC preflight: removing leftover orphan_ clone NS/images")
            _cleanup_orphan_images(gateway, rbd_obj, subsystems, config)
            _assert_gateways_ready(
                gateways,
                "after orphan cleanup",
                tries=int(config.get("gw_ready_tries", 24)),
                delay=int(config.get("gw_ready_delay", 10)),
            )

        pairs, _ = _ensure_dual_kmip_endpoints(
            gateway, subsystems, kmip_nodes, config
        )

        host_nqns = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            initiator.disconnect_all()
            host_nqns[client.hostname] = initiator.initiator_nqn()
            LOG.info("Client %s host NQN %s", client.hostname, host_nqns[client.hostname])

        records, assigned = _parent_ns_records_retry(
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

        mapped = _connect_and_map(gateways, subsystems, clients, assigned, config)
        _assert_cluster_health(ha.orch, "before HA ENC scenarios")

        _run_with_fio(
            mapped,
            clients,
            config,
            _scenario_a,
            ha,
            gateway,
            records,
            config,
            timings,
        )
        _run_with_fio(
            mapped,
            clients,
            config,
            _scenario_b,
            ha,
            gateway,
            records,
            kmip_nodes[0],
            config,
            timings,
        )
        _log_timing_table(timings, pairs)
        _write_timing_csv(timings)
        LOG.info(
            "HA ENC passed: %s parents, scenarios A and B in %ss",
            len(records),
            int(time.time() - started),
        )
        return 0
    except Exception as err:
        _log_timing_table(timings, pairs)
        _write_timing_csv(timings)
        LOG.exception("HA ENC test failed: %s", err)
        return 1
    finally:
        try:
            for node in ceph_cluster.get_nodes(role="client"):
                NVMeInitiator(node).disconnect_all()
        except Exception as exc:
            LOG.warning("Cleanup after HA ENC test: %s", exc)
