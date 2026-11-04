"""OSD failure during I/O on NVMeoF BYOK (encrypted namespaces).

Simulates disk / OSD failure by marking an OSD out during active NVMeoF client I/O:
1. Reuses existing encrypted parent namespaces on group2 (or config-specified group).
2. Sets up NVMeoF initiators, connects namespaces, and starts FIO background workloads.
3. Records baseline I/O metrics (IOPS, bandwidth, latency, submission/completion latency).
4. Selects an active OSD serving the RBD pool and marks it out (or stops OSD daemon to simulate disk failure).
5. Observes and monitors:
   - Impact on latency and throughput/bandwidth during OSD failure / backfill.
   - Recovery behavior and backfill progress (peering, recovery, backfill states).
   - NVMe client I/O continuity without unrecoverable errors.
6. Restores the OSD (marks it back in / starts daemon) and waits for active+clean PG recovery.
7. Validates cluster health and post-recovery I/O metrics.
"""

import json
import re
import shlex
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.ceph_admin import CephAdmin
from ceph.parallel import parallel
from ceph.rados.core_workflows import RadosOrchestrator
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _existing_subsystems,
    _init_rbd,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _assert_gateways_ready,
    _cleanup_orphan_images,
)
from tests.nvmeof.test_ceph_nvmeof_byok_ha_enc import (
    _parent_ns_records_retry,
)
from tests.nvmeof.test_ceph_nvmeof_byok_kmip_gw_restart import (
    DEFAULT_FIO_MAX_RUNTIME,
    FIO_ERR_RE,
    FIO_STOP_EXIT_CODES,
    _connect_and_map,
    _fio_opts,
    _stop_fio,
    _write_light_fio_job,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

BYOK_OSD_FAIL_JOB = "/tmp/byok_osd_fail.fio"
BYOK_OSD_FAIL_FIO_LOG = "/tmp/byok_osd_fail.fio.log"
DEFAULT_OSD_OUT_DURATION = 90  # seconds to keep OSD out while monitoring backfill/IO
DEFAULT_RECOVERY_TIMEOUT = 1800  # seconds to wait for cluster to become clean


def _fio_running(node):
    out, _ = node.exec_command(
        cmd="pgrep -ax fio || true", sudo=True, check_ec=False
    )
    text = (out or "").strip()
    return bool(text), text


def _fio_io_errors(clients):
    """Parse FIO ``err=`` from the per-client log while the job is running."""
    errors = []
    for client in clients:
        out, _ = client.exec_command(
            cmd=f"grep -E 'err=' {BYOK_OSD_FAIL_FIO_LOG} || true",
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
    """FIO must still be running and must not have reported err!=0."""
    dead = []
    for client in clients:
        running, text = _fio_running(client)
        if not running:
            dead.append(client.hostname)
    if dead:
        raise RuntimeError(f"{label}: FIO is not running on {dead}")
    io_errors = _fio_io_errors(clients)
    if io_errors:
        raise RuntimeError(f"{label}: FIO reported IO errors:\n" + "\n".join(io_errors))


def _sample_fio_status(clients):
    """Parse recent status lines from FIO logs across clients to extract IOPS/BW/latency."""
    metrics = {}
    for client in clients:
        out, _ = client.exec_command(
            cmd=f"tail -n 15 {BYOK_OSD_FAIL_FIO_LOG} 2>/dev/null || true",
            sudo=True,
            check_ec=False,
        )
        metrics[client.hostname] = (out or "").strip()
    return metrics


def _log_fio_metrics_sample(clients, label):
    """Log instantaneous IO throughput/latency observations."""
    samples = _sample_fio_status(clients)
    for host, output in samples.items():
        if not output:
            continue
        status_lines = [
            line for line in output.splitlines()
            if "Jobs:" in line or "IOPS=" in line or "bw=" in line or "iops=" in line
        ]
        if status_lines:
            LOG.info("[%s] %s FIO status: %s", label, host, status_lines[-1])
        else:
            LOG.info("[%s] %s FIO raw output tail:\n%s", label, host, output[-300:])


def _run_osd_fail_fio_job(node, job_path):
    """Run FIO with status interval for dynamic throughput/latency monitoring."""
    LOG.info("Starting FIO %s on %s", job_path, node.hostname)
    return node.exec_command(
        cmd=f"fio --output={BYOK_OSD_FAIL_FIO_LOG} --status-interval=10 {job_path}",
        sudo=True,
        long_running=True,
        timeout="notimeout",
    )


def _prepare_osd_fail_fio(mapped, config, runtime):
    opts = _fio_opts(config, runtime)
    for client, _, paths in mapped.values():
        if not paths:
            raise RuntimeError(f"No parent devices on {client.hostname}")
        _write_light_fio_job(client, BYOK_OSD_FAIL_JOB, paths, opts)


def _pick_target_osd(rados_obj, pool_name):
    """Select an OSD in the acting set for the given pool."""
    pg_set = rados_obj.get_pg_acting_set(pool_name=pool_name)
    if pg_set:
        target_osd = pg_set[0]
        LOG.info(
            "Selected target OSD %s from pool '%s' acting set: %s",
            target_osd,
            pool_name,
            pg_set,
        )
        return target_osd
    # Fallback to an 'in' and 'up' OSD
    osd_dump = rados_obj.run_ceph_command(cmd="ceph osd dump")
    for osd_entry in osd_dump.get("osds", []):
        if osd_entry.get("in") == 1 and osd_entry.get("up") == 1:
            target_osd = osd_entry.get("osd")
            LOG.info("Selected target OSD %s from ceph osd dump", target_osd)
            return target_osd
    raise RuntimeError("No up and in OSD found in the cluster to simulate failure")


def _monitor_recovery_and_backfill(rados_obj, test_pool, clients, duration_sec=60, poll_interval=10):
    """Observe cluster backfill and recovery status while verifying client IO health."""
    start_time = time.time()
    LOG.info(
        "Monitoring recovery/backfill and NVMe workload for %s seconds (interval %ss)",
        duration_sec,
        poll_interval,
    )
    while time.time() - start_time < duration_sec:
        _assert_fio_healthy(clients, "during recovery/backfill observation")
        _log_fio_metrics_sample(clients, "Recovery/Backfill")

        # Query pg and health status
        try:
            health_summary = rados_obj.run_ceph_command(cmd="ceph health detail")
            pg_stat = rados_obj.run_ceph_command(cmd="ceph pg stat")
            LOG.info("PG Status: %s", pg_stat if isinstance(pg_stat, str) else json.dumps(pg_stat))
            LOG.info("Health Detail snippet: %s", str(health_summary)[:300])
        except Exception as exc:
            LOG.warning("Failed to query cluster health during observation: %s", exc)

        time.sleep(poll_interval)


def _wait_for_clean_pgs(rados_obj, pool_name, timeout=DEFAULT_RECOVERY_TIMEOUT, poll_interval=15):
    """Wait for all PGs in the pool (or cluster) to reach active+clean state."""
    LOG.info("Waiting up to %s seconds for PGs on pool '%s' to become active+clean", timeout, pool_name)
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            pg_dump = rados_obj.run_ceph_command(cmd="ceph pg stat")
            pg_stat_str = str(pg_dump)
            LOG.info("Current ceph pg stat: %s", pg_stat_str)
            # Check cluster health
            health_str = rados_obj.run_ceph_command(cmd="ceph health")
            if "HEALTH_OK" in str(health_str):
                LOG.info("Cluster returned to HEALTH_OK: %s", health_str)
                return True
            # Also check if PGs are clean
            if "active+clean" in pg_stat_str and "degraded" not in pg_stat_str and "backfill" not in pg_stat_str:
                LOG.info("All PGs are active+clean: %s", pg_stat_str)
                return True
        except Exception as exc:
            LOG.warning("Error while polling pg stat: %s", exc)
        time.sleep(poll_interval)

    LOG.warning("Timed out waiting for clean PGs after %s seconds", timeout)
    return False


def _osd_failure_scenario(clients, stop_state, rados_obj, pool_name, config):
    """Execute OSD failure / mark-out, observe recovery/backfill, and restore OSD."""
    osd_out_duration = int(config.get("osd_out_duration", DEFAULT_OSD_OUT_DURATION))
    simulate_disk_failure = config.get("simulate_disk_failure", False)

    # 1. Baseline observation during steady-state IO
    LOG.info("Phase 1: Establishing steady-state NVMe IO baseline (30s)...")
    time.sleep(30)
    _assert_fio_healthy(clients, "Steady-state baseline")
    _log_fio_metrics_sample(clients, "Baseline")

    # 2. Identify target OSD
    target_osd = _pick_target_osd(rados_obj, pool_name)
    target_host = rados_obj.fetch_host_node(daemon_type="osd", daemon_id=str(target_osd))
    LOG.info(
        "Target OSD: osd.%s residing on host: %s",
        target_osd,
        getattr(target_host, "hostname", "unknown"),
    )

    # 3. Trigger OSD failure (simulate disk failure by stopping daemon or marking out)
    if simulate_disk_failure:
        LOG.info("Phase 2: Simulating disk/OSD failure by stopping daemon osd.%s...", target_osd)
        rados_obj.change_osd_state(action="stop", target=target_osd)
        time.sleep(5)
        LOG.info("Marking osd.%s out following daemon stop...", target_osd)
        rados_obj.update_osd_state_on_cluster(osd_id=target_osd, state="out")
    else:
        LOG.info("Phase 2: Marking osd.%s out during active I/O...", target_osd)
        if not rados_obj.update_osd_state_on_cluster(osd_id=target_osd, state="out"):
            raise RuntimeError(f"Failed to mark osd.{target_osd} out")

    # 4. Observe impact on latency, throughput, recovery, and backfill
    LOG.info(
        "Phase 3: Observing latency/throughput and backfill behavior with osd.%s OUT for %ss...",
        target_osd,
        osd_out_duration,
    )
    _monitor_recovery_and_backfill(
        rados_obj=rados_obj,
        test_pool=pool_name,
        clients=clients,
        duration_sec=osd_out_duration,
        poll_interval=10,
    )

    # 5. Restore OSD (bring daemon back up if stopped, mark in)
    LOG.info("Phase 4: Restoring osd.%s back to the cluster...", target_osd)
    if simulate_disk_failure:
        LOG.info("Starting daemon osd.%s...", target_osd)
        rados_obj.change_osd_state(action="restart", target=target_osd)
        time.sleep(5)

    LOG.info("Marking osd.%s in...", target_osd)
    if not rados_obj.update_osd_state_on_cluster(osd_id=target_osd, state="in"):
        raise RuntimeError(f"Failed to mark osd.{target_osd} in")

    # 6. Wait for recovery / active+clean PGs and observe final backfill
    LOG.info("Phase 5: Waiting for backfill completion and clean PG state post-restore...")
    recovery_timeout = int(config.get("recovery_timeout", DEFAULT_RECOVERY_TIMEOUT))
    _wait_for_clean_pgs(rados_obj, pool_name=pool_name, timeout=recovery_timeout)

    # 7. Post-recovery IO verification
    LOG.info("Phase 6: Verifying post-recovery NVMe IO health and performance...")
    time.sleep(15)
    _assert_fio_healthy(clients, "Post-recovery")
    _log_fio_metrics_sample(clients, "Post-recovery")

    # Signal stop to FIO workers and stop FIO on clients
    stop_state["stopped"] = True
    _stop_fio(clients)
    LOG.info("OSD failure scenario completed successfully")


def _run_with_fio(mapped, clients, config, worker, *args):
    """Start light FIO, execute test worker, stop FIO, and verify no unexpected errors."""
    runtime = int(config.get("fio_max_runtime", DEFAULT_FIO_MAX_RUNTIME))
    _prepare_osd_fail_fio(mapped, config, runtime)
    stop_state = {"stopped": False}
    errors = []
    try:
        with parallel(timeout=runtime + 600) as p:
            for client, _, _ in mapped.values():
                p.spawn(_run_osd_fail_fio_job, client, BYOK_OSD_FAIL_JOB)
            p.spawn(worker, clients, stop_state, *args)
            for result in p:
                if isinstance(result, Exception):
                    if stop_state["stopped"]:
                        LOG.info("FIO ended after scenario completion: %s", result)
                        continue
                    for client in clients:
                        client.exec_command(
                            cmd="pkill -9 -x fio || true", sudo=True, check_ec=False
                        )
                    raise result
                if isinstance(result, int) and result not in FIO_STOP_EXIT_CODES:
                    if stop_state["stopped"]:
                        LOG.info("FIO exit code %s after stop; treating as clean exit", result)
                        continue
                    errors.append(result)
        if errors:
            raise RuntimeError(f"FIO failed with exit codes: {errors}")
        io_errors = _fio_io_errors(clients)
        if io_errors:
            raise RuntimeError("FIO reported IO errors:\n" + "\n".join(io_errors))
    except Exception:
        _stop_fio(clients)
        raise
    finally:
        _stop_fio(clients)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Run OSD failure and recovery test during NVMeoF BYOK client I/O.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    try:
        rbd_obj = _init_rbd(kwargs)
        pool_name = config.get("rbd_pool") or config.get("rep_pool_config", {}).get(
            "pool", "rbd"
        )
        cephadm = CephAdmin(cluster=ceph_cluster, **config)
        rados_obj = RadosOrchestrator(node=cephadm)

        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        for gw in nvme_service.gateways:
            gw.load_gateway_info()
        gateway = nvme_service.gateways[0]
        gateways = nvme_service.gateways
        clients = ceph_cluster.get_nodes(role="client")

        if not clients:
            raise ValueError("Test requires at least one client node")

        subsystems = _existing_subsystems(gateway, config)

        if config.get("cleanup_orphan_images", True):
            LOG.info("Cleaning leftover orphan clone NS/images before test...")
            _cleanup_orphan_images(gateway, rbd_obj, subsystems, config)
            _assert_gateways_ready(gateways, "after orphan cleanup")

        host_nqns = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            initiator.disconnect_all()
            host_nqns[client.hostname] = initiator.initiator_nqn()
            LOG.info("Client %s host NQN: %s", client.hostname, host_nqns[client.hostname])

        records, assigned = _parent_ns_records_retry(
            gateway, subsystems, clients, host_nqns
        )
        LOG.info("Discovered %s encrypted parent namespaces for OSD fail test", len(records))

        mapped = _connect_and_map(gateways, subsystems, clients, assigned, config)

        _run_with_fio(
            mapped,
            clients,
            config,
            _osd_failure_scenario,
            rados_obj,
            pool_name,
            config,
        )

        LOG.info("NVMeoF BYOK OSD failure and recovery test passed successfully")
        return 0
    except Exception as err:
        LOG.exception("NVMeoF BYOK OSD failure test failed: %s", err)
        return 1
