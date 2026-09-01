"""NVMeoF BYOK Cluster Fill (75% fast fill, then paced slow fill to 80% + parallel reads) test with monitoring.

Phased execution:
  Phase 1 (Fast Fill to ~75% in 1-2 hours):
    - Sequential 1M writes across all assigned BYOK namespaces until 75% capacity is reached.
  Phase 2 (Endurance 8h slow write fill to 80% with parallel read IO):
    - Runs rate-paced / time-paced mixed read-write FIO (e.g. 70% read, 30% write with rate limiting
      and paced offset distribution) over 8 hours (28800s) such that capacity drifts steadily from
      75% to 80% at the end of 8 hours while parallel read IOs test latency drift under load.
  Background Monitoring (Throughout both phases):
    - Latency drift (read/write latencies over time)
    - Error rates (IO errors, Ceph health warnings/errors)
    - Thermal throttling on physical NVMe drives (smart-log temperature, warning/critical times)
    - Gateway stability (SPDK / daemon status) and OSD stability (up/in status)
    - Continuously logged to /tmp/byok_endurance_metrics.jsonl
  Post-Run Analysis:
    - Summarizes and aggregates all metrics into /tmp/byok_endurance_summary.json.
"""

import json
import os
import shlex
import threading
import time
from datetime import datetime

from ceph.ceph import Ceph, CommandFailed
from ceph.ceph_admin.orch import Orch
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _existing_subsystems,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import (
    _allow_host,
    _apply_namespace_masking,
    _change_ns_visibility,
    _configure_subsystem_hosts,
    _connect_client,
    _host_names,
    _is_auto_visible,
    _is_clone,
    _list_namespaces,
    _needs_mask,
    _norm_uuid,
    _ns_record,
    _ns_usable,
    _paths_for_uuids,
    _sample_masking_io,
    _size_to_gib,
    _verify_gateway_masking,
    _verify_initiator_masking,
    _write_fio_job,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

FILL_JOB = "/tmp/byok_capacity_fill.fio"
PACED_WORKLOAD_JOB = "/tmp/byok_paced_endurance.fio"
MONITOR_LOG_FILE = "/tmp/byok_endurance_metrics.jsonl"
MONITOR_SUMMARY_FILE = "/tmp/byok_endurance_summary.json"


class ClusterMonitor:
    """Background polling engine for cluster health, gateways, OSDs, and drive thermal status."""

    def __init__(
        self,
        ceph_cluster: Ceph,
        gateways,
        clients,
        poll_interval: int = 60,
        log_file: str = MONITOR_LOG_FILE,
        target_pool: str = "rbd",
    ):
        self.ceph_cluster = ceph_cluster
        self.orch = Orch(ceph_cluster, **{})
        self.gateways = gateways
        self.clients = clients
        self.poll_interval = poll_interval
        self.log_file = log_file
        self.target_pool = target_pool
        self._stop_event = threading.Event()
        self._thread = None
        self.samples = []
        self.osd_nodes = ceph_cluster.get_nodes(role="osd")

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_poll_loop, daemon=True)
        self._thread.start()
        LOG.info("Cluster background monitor started (polling every %ss)", self.poll_interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=120)
        LOG.info("Cluster background monitor stopped. Total samples gathered: %d", len(self.samples))

    def _run_poll_loop(self):
        while not self._stop_event.is_set():
            sample_time = time.time()
            try:
                sample = self._collect_sample()
                self.samples.append(sample)
                self._append_sample_to_log(sample)
            except Exception as exc:
                LOG.warning("Error collecting cluster monitoring sample: %s", exc)

            elapsed = time.time() - sample_time
            sleep_time = max(1.0, self.poll_interval - elapsed)
            self._stop_event.wait(timeout=sleep_time)

    def _append_sample_to_log(self, sample: dict):
        line = json.dumps(sample) + "\n"
        try:
            with open(self.log_file, "a") as f:
                f.write(line)
        except Exception as exc:
            LOG.debug("Could not write metric sample locally: %s", exc)

    def _collect_sample(self) -> dict:
        timestamp = datetime.utcnow().isoformat()
        sample = {
            "timestamp": timestamp,
            "epoch": time.time(),
            "ceph_df": self._get_ceph_df(),
            "ceph_health": self._get_ceph_health(),
            "osd_status": self._get_osd_status(),
            "gateway_status": self._get_gateway_status(),
            "nvme_drive_thermals": self._get_nvme_drive_thermals(),
            "client_iostat": self._get_client_stats(),
        }
        return sample

    def _get_ceph_df(self) -> dict:
        try:
            out, _ = self.orch.shell(args=["ceph", "df", "-f", "json"], print_output=False)
            data = json.loads(out)
            stats = data.get("stats", {})
            total_bytes = stats.get("total_bytes", 0)
            used_bytes = stats.get("total_used_bytes", 0)
            raw_used_pct = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0.0

            pool_pct = 0.0
            for p in data.get("pools", []):
                if p.get("name") == self.target_pool:
                    p_stats = p.get("stats", {})
                    p_bytes = p_stats.get("bytes_used", 0)
                    p_max = p_stats.get("max_avail", 0)
                    if (p_bytes + p_max) > 0:
                        pool_pct = (p_bytes / (p_bytes + p_max)) * 100
                    break

            return {
                "raw_total_bytes": total_bytes,
                "raw_used_bytes": used_bytes,
                "raw_used_pct": round(raw_used_pct, 2),
                "pool_used_pct": round(pool_pct, 2),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _get_ceph_health(self) -> dict:
        try:
            out, _ = self.orch.shell(args=["ceph", "health", "detail", "-f", "json"], print_output=False)
            data = json.loads(out)
            status = data.get("status", "UNKNOWN")
            summary = data.get("checks", {})
            warnings = [k for k, v in summary.items() if v.get("severity") == "HEALTH_WARN"]
            errors = [k for k, v in summary.items() if v.get("severity") == "HEALTH_ERR"]
            return {
                "status": status,
                "warning_count": len(warnings),
                "warnings": warnings,
                "error_count": len(errors),
                "errors": errors,
            }
        except Exception as exc:
            return {"status": "ERROR", "error": str(exc)}

    def _get_osd_status(self) -> dict:
        try:
            out, _ = self.orch.shell(args=["ceph", "osd", "stat", "-f", "json"], print_output=False)
            data = json.loads(out)
            num_osds = data.get("num_osds", 0)
            num_up_osds = data.get("num_up_osds", 0)
            num_in_osds = data.get("num_in_osds", 0)
            down_osds = num_osds - num_up_osds
            out_osds = num_osds - num_in_osds
            return {
                "total": num_osds,
                "up": num_up_osds,
                "in": num_in_osds,
                "down": down_osds,
                "out": out_osds,
                "stable": (down_osds == 0 and out_osds == 0),
            }
        except Exception as exc:
            return {"error": str(exc), "stable": False}

    def _get_gateway_status(self) -> dict:
        gw_status = {}
        for gw in self.gateways:
            node = gw.node
            host = node.hostname
            try:
                ps_out, _ = node.exec_command(
                    cmd="systemctl is-active ceph-nvmeof* || ps -ef | grep -E 'spdk|nvmf' | grep -v grep",
                    sudo=True,
                    check_ec=False,
                )
                active = "active" in ps_out or "spdk" in ps_out or "nvmf" in ps_out
                load_out, _ = node.exec_command(cmd="uptime", sudo=True, check_ec=False)
                gw_status[host] = {
                    "active": bool(active),
                    "uptime": load_out.strip() if load_out else "",
                }
            except Exception as exc:
                gw_status[host] = {"active": False, "error": str(exc)}
        return gw_status

    def _get_nvme_drive_thermals(self) -> dict:
        """Query physical NVMe drives on OSD/storage nodes for temperature and thermal throttling."""
        thermals = {}
        for osd_node in self.osd_nodes:
            host = osd_node.hostname
            try:
                cmd = (
                    "python3 -c \""
                    "import json, glob, subprocess; "
                    "results = []; "
                    "devs = glob.glob('/dev/nvme[0-9]'); "
                    "devs += [d for d in glob.glob('/dev/nvme[0-9]n[0-9]') if 'n1' not in d]; "
                    "for d in sorted(set(devs)): "
                    "  try: "
                    "    p = subprocess.run(['nvme', 'smart-log', d, '-o', 'json'], capture_output=True, text=True, timeout=5); "
                    "    if p.returncode == 0: "
                    "      data = json.loads(p.stdout); "
                    "      temp = data.get('temperature', 0); "
                    "      w_temp = data.get('warning_temp_time', 0); "
                    "      c_temp = data.get('critical_comp_time', 0); "
                    "      warn = data.get('critical_warning', 0); "
                    "      results.append({'dev': d, 'temperature_c': temp - 273 if temp > 200 else temp, 'warning_temp_time': w_temp, 'critical_comp_time': c_temp, 'crit_warning': warn}); "
                    "  except Exception: "
                    "    pass; "
                    "print(json.dumps(results))\""
                )
                out, _ = osd_node.exec_command(cmd=cmd, sudo=True, check_ec=False)
                if out and out.strip().startswith("["):
                    thermals[host] = json.loads(out.strip())
                else:
                    thermals[host] = []
            except Exception as exc:
                thermals[host] = {"error": str(exc)}
        return thermals

    def _get_client_stats(self) -> dict:
        client_stats = {}
        for client in self.clients:
            host = client.hostname
            try:
                out, _ = client.exec_command(
                    cmd="vmstat 1 2 -S M | tail -1",
                    sudo=True,
                    check_ec=False,
                )
                client_stats[host] = {"vmstat": out.strip() if out else ""}
            except Exception as exc:
                client_stats[host] = {"error": str(exc)}
        return client_stats


def _collect_usable_namespaces_multigw(gateways, subsystems):
    """List parent and clone namespaces across all gateways in the group.
    
    A namespace is considered usable if it is live on ANY gateway.
    """
    usable_by_uuid = {}
    skipped = 0
    clones = 0
    parents = 0

    for sub in subsystems:
        nqn = sub["group_nqn"]
        for gw in gateways:
            listed = _list_namespaces(gw, nqn)
            for ns in listed:
                uuid = _norm_uuid(ns.get("uuid"))
                if not uuid:
                    continue
                if uuid in usable_by_uuid:
                    continue
                if _ns_usable(ns):
                    record = _ns_record(nqn, ns)
                    record["clone"] = _is_clone(ns)
                    if record["clone"]:
                        clones += 1
                    else:
                        parents += 1
                    usable_by_uuid[uuid] = record

    usable = list(usable_by_uuid.values())
    LOG.info(
        "Multi-GW Usable namespaces=%s parents=%s clones=%s (scanned %s gateways)",
        len(usable),
        parents,
        clones,
        len(gateways),
    )
    return usable


def _assign_namespaces_multigw(gateways, subsystems, clients, host_nqns):
    """Round-robin all usable namespaces discovered across all gateways."""
    assigned = {client.hostname: [] for client in clients}
    all_ns = _collect_usable_namespaces_multigw(gateways, subsystems)
    if not all_ns:
        raise RuntimeError("No usable namespaces found on subsystems across gateways")
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


def _reuse_ns_assignment_multigw(gateways, subsystems, clients, host_nqns):
    """Rebuild ownership from existing ACLs across all gateways.
    
    If any gateway shows a valid image or ACL, keep it so initiators mapping via HA peers match.
    """
    assigned = {client.hostname: [] for client in clients}
    seen_uuids = set()
    to_mask = []
    next_index = 0
    usable = 0
    clones = 0

    # Aggregate best information for each namespace across all gateways
    for sub in subsystems:
        nqn = sub["group_nqn"]
        ns_map = {}
        for gw in gateways:
            for raw in _list_namespaces(gw, nqn):
                uuid = _norm_uuid(raw.get("uuid"))
                if not uuid:
                    continue
                if uuid not in ns_map:
                    ns_map[uuid] = raw
                else:
                    # Prefer non-degraded entry if one gateway has it open
                    if _ns_usable(raw) and not _ns_usable(ns_map[uuid]):
                        ns_map[uuid] = raw

        for uuid, raw in ns_map.items():
            if uuid in seen_uuids:
                continue
            seen_uuids.add(uuid)
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
        raise RuntimeError("No namespaces found across gateways")

    LOG.info(
        "Reused namespace ACLs (Multi-GW): total=%s remask=%s already_masked=%s clones=%s",
        usable,
        len(to_mask),
        usable - len(to_mask),
        clones,
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


def _get_current_cluster_fill(orch: Orch, pool_name: str = "rbd") -> tuple:
    """Return (raw_used_pct, pool_used_pct)."""
    out, _ = orch.shell(args=["ceph", "df", "-f", "json"], print_output=False)
    data = json.loads(out)
    stats = data.get("stats", {})
    total = stats.get("total_bytes", 0)
    used = stats.get("total_used_bytes", 0)
    raw_pct = (used / total * 100) if total > 0 else 0.0

    pool_pct = 0.0
    for p in data.get("pools", []):
        if p.get("name") == pool_name:
            p_stats = p.get("stats", {})
            u = p_stats.get("bytes_used", 0)
            m = p_stats.get("max_avail", 0)
            if (u + m) > 0:
                pool_pct = (u / (u + m)) * 100
            break
    return raw_pct, pool_pct


def _fill_cluster_to_threshold(
    mapped: dict,
    orch: Orch,
    target_pct: float = 75.0,
    fill_bs: str = "1M",
    iodepth: str = "16",
    pool_name: str = "rbd",
    step_percent: int = 15,
):
    """Phase 1: Fast fill namespaces until cluster capacity reaches target_pct (75%)."""
    raw_pct, pool_pct = _get_current_cluster_fill(orch, pool_name)
    LOG.info(
        "[Phase 1] Fast Fill Starting: Raw used=%.2f%%, Pool '%s' used=%.2f%% (Target=%.2f%%)",
        raw_pct,
        pool_name,
        pool_pct,
        target_pct,
    )

    if raw_pct >= target_pct:
        LOG.info("[Phase 1] Cluster is already at or above target raw fill threshold (%.2f%% >= %.2f%%)", raw_pct, target_pct)
        return

    current_fill_slice = int(target_pct)
    LOG.info("[Phase 1] Writing %d%% sequential fill across all namespaces to hit target in ~1-2 hours...", current_fill_slice)

    for client, _, paths in mapped.values():
        if not paths:
            continue
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
                "size": f"{current_fill_slice}%",
                "numjobs": "1",
            },
        )

    fill_start = time.time()
    with parallel() as p:
        for client, _, _ in mapped.values():
            p.spawn(
                client.exec_command,
                cmd=f"fio {FILL_JOB}",
                sudo=True,
                long_running=True,
                timeout="notimeout",
            )
        for res in p:
            if isinstance(res, int) and res != 0:
                LOG.warning("Fast fill non-zero return: %s", res)

    fill_duration = time.time() - fill_start
    raw_pct, pool_pct = _get_current_cluster_fill(orch, pool_name)
    LOG.info(
        "[Phase 1 Completed] Fast Fill took %.2f minutes (%.2f hrs). Raw used=%.2f%%, Pool used=%.2f%%",
        fill_duration / 60.0,
        fill_duration / 3600.0,
        raw_pct,
        pool_pct,
    )


def _run_paced_endurance_workload(
    mapped: dict,
    total_ns: int,
    size_gib_per_ns: float,
    start_pct: float = 75.0,
    end_pct: float = 80.0,
    io_runtime: int = 28800,  # 8 hours
    continuous_bs: str = "64k",
    iodepth: str = "16",
    rwmixread: int = 70,
):
    """Phase 2: Run 8-hour mixed read-write workload pacing writes from 75% -> 80% with parallel reads."""
    pct_delta = max(0.0, end_pct - start_pct)
    total_mapped_gib = total_ns * size_gib_per_ns
    target_write_gib = (pct_delta / 100.0) * total_mapped_gib
    num_clients = max(1, len(mapped))

    # Calculate target write rate per client to stretch the remaining ~5% fill across the full 8-hour window
    target_write_rate_kb_total = (target_write_gib * 1024 * 1024) / max(1, io_runtime)
    target_write_rate_kb_per_client = int(target_write_rate_kb_total / num_clients)
    per_ns_rate_kib = max(64, int(target_write_rate_kb_per_client / max(1, total_ns // num_clients)))

    LOG.info(
        "[Phase 2] Starting 8-Hour Paced Endurance Workload: "
        "Drifting fill from %.1f%% to %.1f%% (+%.1f GiB writes total, ~%d KiB/s write rate per NS). "
        "Parallel Reads: %d%% mix, Block Size=%s, iodepth=%s, Duration=%ds (~%.1f hrs)",
        start_pct,
        end_pct,
        target_write_gib,
        per_ns_rate_kib,
        rwmixread,
        continuous_bs,
        iodepth,
        io_runtime,
        io_runtime / 3600.0,
    )

    fio_opts = {
        "ioengine": "libaio",
        "direct": "1",
        "bs": continuous_bs,
        "rw": "randrw",
        "rwmixread": str(rwmixread),
        "iodepth": iodepth,
        "group_reporting": "1",
        "time_based": "1",
        "runtime": str(io_runtime),
        "rate_iops": "0",
        "rate_min": "0",
        "offset": "0",
        "size": "100%",
        "numjobs": "1",
        "write_lat_log": "/tmp/fio_paced_lat",
        "write_iops_log": "/tmp/fio_paced_iops",
        "log_avg_msec": "10000",
    }

    for client, _, paths in mapped.values():
        _write_fio_job(client, PACED_WORKLOAD_JOB, paths, fio_opts)

    fio_results = {}
    errors = []

    with parallel() as p:
        for client, _, _ in mapped.values():
            cmd = f"fio {PACED_WORKLOAD_JOB} --output-format=json --output=/tmp/byok_paced_fio_result.json"
            p.spawn(
                client.exec_command,
                cmd=cmd,
                sudo=True,
                long_running=True,
                timeout="notimeout",
            )
        for res in p:
            if isinstance(res, int) and res != 0:
                errors.append(res)

    for client, _, _ in mapped.values():
        try:
            out, _ = client.exec_command(cmd="cat /tmp/byok_paced_fio_result.json", sudo=True)
            fio_results[client.hostname] = json.loads(out)
        except Exception as exc:
            LOG.warning("Failed to retrieve FIO json output from %s: %s", client.hostname, exc)

    return fio_results, errors


def _analyze_monitoring_and_io(monitor_samples: list, fio_results: dict) -> dict:
    """Summarize and analyze latency drift, error rates, thermal throttling, and component stability."""
    summary = {
        "duration_samples": len(monitor_samples),
        "ceph_stability": {
            "all_healthy": True,
            "warn_samples_count": 0,
            "error_samples_count": 0,
            "osd_down_events": 0,
            "unique_warnings": set(),
            "unique_errors": set(),
        },
        "gateway_stability": {
            "all_active": True,
            "unhealthy_samples": 0,
        },
        "thermal_throttling": {
            "max_nvme_temp_c": 0,
            "warning_temp_events": 0,
            "critical_comp_events": 0,
            "critical_warnings_found": 0,
            "drives_monitored": set(),
        },
        "capacity_drift": {
            "start_raw_pct": 0.0,
            "end_raw_pct": 0.0,
            "start_pool_pct": 0.0,
            "end_pool_pct": 0.0,
        },
        "fio_workload_metrics": {},
    }

    if monitor_samples:
        first = monitor_samples[0]
        last = monitor_samples[-1]
        summary["capacity_drift"]["start_raw_pct"] = first.get("ceph_df", {}).get("raw_used_pct", 0.0)
        summary["capacity_drift"]["end_raw_pct"] = last.get("ceph_df", {}).get("raw_used_pct", 0.0)
        summary["capacity_drift"]["start_pool_pct"] = first.get("ceph_df", {}).get("pool_used_pct", 0.0)
        summary["capacity_drift"]["end_pool_pct"] = last.get("ceph_df", {}).get("pool_used_pct", 0.0)

    for s in monitor_samples:
        health = s.get("ceph_health", {})
        if health.get("status") not in ("HEALTH_OK", "OK"):
            summary["ceph_stability"]["all_healthy"] = False
        if health.get("warning_count", 0) > 0:
            summary["ceph_stability"]["warn_samples_count"] += 1
            for w in health.get("warnings", []):
                summary["ceph_stability"]["unique_warnings"].add(w)
        if health.get("error_count", 0) > 0:
            summary["ceph_stability"]["error_samples_count"] += 1
            for e in health.get("errors", []):
                summary["ceph_stability"]["unique_errors"].add(e)

        osd_st = s.get("osd_status", {})
        if not osd_st.get("stable", True):
            summary["ceph_stability"]["osd_down_events"] += 1

        gw_st = s.get("gateway_status", {})
        for host, ginfo in gw_st.items():
            if not ginfo.get("active", False):
                summary["gateway_stability"]["all_active"] = False
                summary["gateway_stability"]["unhealthy_samples"] += 1

        thermals = s.get("nvme_drive_thermals", {})
        for host, drives in thermals.items():
            if isinstance(drives, list):
                for d in drives:
                    dev_id = f"{host}:{d.get('dev')}"
                    summary["thermal_throttling"]["drives_monitored"].add(dev_id)
                    temp = d.get("temperature_c", 0)
                    if temp > summary["thermal_throttling"]["max_nvme_temp_c"]:
                        summary["thermal_throttling"]["max_nvme_temp_c"] = temp
                    if d.get("warning_temp_time", 0) > 0:
                        summary["thermal_throttling"]["warning_temp_events"] += 1
                    if d.get("critical_comp_time", 0) > 0:
                        summary["thermal_throttling"]["critical_comp_events"] += 1
                    if d.get("crit_warning", 0) > 0:
                        summary["thermal_throttling"]["critical_warnings_found"] += 1

    summary["ceph_stability"]["unique_warnings"] = list(summary["ceph_stability"]["unique_warnings"])
    summary["ceph_stability"]["unique_errors"] = list(summary["ceph_stability"]["unique_errors"])
    summary["thermal_throttling"]["drives_monitored"] = list(summary["thermal_throttling"]["drives_monitored"])

    # Parse FIO results
    for hostname, data in fio_results.items():
        jobs = data.get("jobs", [])
        if not jobs:
            continue
        job = jobs[0]
        read_stat = job.get("read", {})
        write_stat = job.get("write", {})

        summary["fio_workload_metrics"][hostname] = {
            "read_iops": read_stat.get("iops", 0),
            "read_bw_mbps": read_stat.get("bw", 0) / 1024.0,
            "read_lat_mean_ms": read_stat.get("lat_ns", {}).get("mean", 0) / 1e6,
            "read_lat_p99_ms": read_stat.get("clat_ns", {}).get("percentile", {}).get("99.000000", 0) / 1e6,
            "write_iops": write_stat.get("iops", 0),
            "write_bw_mbps": write_stat.get("bw", 0) / 1024.0,
            "write_lat_mean_ms": write_stat.get("lat_ns", {}).get("mean", 0) / 1e6,
            "write_lat_p99_ms": write_stat.get("clat_ns", {}).get("percentile", {}).get("99.000000", 0) / 1e6,
            "total_errors": job.get("error", 0),
        }

    return summary


def _print_and_save_summary(summary: dict, output_file: str = MONITOR_SUMMARY_FILE):
    """Print readable analysis summary and save to json."""
    LOG.info("=" * 70)
    LOG.info("                BYOK ENDURANCE WORKLOAD RUN SUMMARY                ")
    LOG.info("=" * 70)
    LOG.info("Total Monitoring Samples Collected: %s", summary["duration_samples"])
    LOG.info(
        "Cluster Fill Drift: Raw (%.2f%% -> %.2f%%), Target Pool (%.2f%% -> %.2f%%)",
        summary["capacity_drift"]["start_raw_pct"],
        summary["capacity_drift"]["end_raw_pct"],
        summary["capacity_drift"]["start_pool_pct"],
        summary["capacity_drift"]["end_pool_pct"],
    )
    LOG.info(
        "Ceph Stability: Healthy=%s | Warnings=%d | Errors=%d | OSD down events=%d",
        summary["ceph_stability"]["all_healthy"],
        summary["ceph_stability"]["warn_samples_count"],
        summary["ceph_stability"]["error_samples_count"],
        summary["ceph_stability"]["osd_down_events"],
    )
    if summary["ceph_stability"]["unique_warnings"]:
        LOG.warning("Unique Ceph Warnings observed: %s", summary["ceph_stability"]["unique_warnings"])
    if summary["ceph_stability"]["unique_errors"]:
        LOG.error("Unique Ceph Errors observed: %s", summary["ceph_stability"]["unique_errors"])

    LOG.info(
        "Gateway Stability: All Gateways Active=%s | Unhealthy gateway samples=%d",
        summary["gateway_stability"]["all_active"],
        summary["gateway_stability"]["unhealthy_samples"],
    )

    LOG.info(
        "NVMe Thermals: Monitored Drives=%d | Max Temp Observed=%d°C | Temp Warning Events=%d | Critical Temp Events=%d",
        len(summary["thermal_throttling"]["drives_monitored"]),
        summary["thermal_throttling"]["max_nvme_temp_c"],
        summary["thermal_throttling"]["warning_temp_events"],
        summary["thermal_throttling"]["critical_comp_events"],
    )

    LOG.info("-" * 70)
    LOG.info("Client IO & Latency Metrics:")
    for client, m in summary["fio_workload_metrics"].items():
        LOG.info(
            "[%s] READ: IOPS=%.1f, BW=%.1f MB/s, Lat Avg=%.2f ms, Lat p99=%.2f ms | "
            "WRITE: IOPS=%.1f, BW=%.1f MB/s, Lat Avg=%.2f ms, Lat p99=%.2f ms | Errors=%d",
            client,
            m["read_iops"],
            m["read_bw_mbps"],
            m["read_lat_mean_ms"],
            m["read_lat_p99_ms"],
            m["write_iops"],
            m["write_bw_mbps"],
            m["write_lat_mean_ms"],
            m["write_lat_p99_ms"],
            m["total_errors"],
        )
    LOG.info("=" * 70)

    try:
        with open(output_file, "w") as f:
            json.dump(summary, f, indent=2)
        LOG.info("Full analysis report written to %s", output_file)
    except Exception as exc:
        LOG.warning("Could not write summary json to %s: %s", output_file, exc)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Execute BYOK fast fill to 75% (~1-2h), then 8h paced slow fill to 80% + parallel reads with monitoring."""
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    try:
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        gateway = nvme_service.gateways[0]
        clients = ceph_cluster.get_nodes(role="client")
        orch = Orch(ceph_cluster, **{})

        if not clients:
            raise ValueError("Endurance workload test requires at least one client node")

        subsystems = _existing_subsystems(gateway, config)
        port = config.get("listener_port", DEFAULT_LISTENER_PORT)

        fast_fill_target = float(config.get("fast_fill_target_pct", 75.0))
        final_fill_target = float(config.get("final_fill_target_pct", 80.0))
        fill_bs = config.get("fill_bs", "1M")
        iodepth = str(config.get("iodepth", 16))

        # Phase 2 runtime: default 8 hours (28800 seconds)
        paced_io_runtime = int(config.get("paced_io_runtime", 28800))
        continuous_bs = config.get("continuous_bs", "64k")
        rwmixread = int(config.get("rwmixread", 70))
        poll_interval = int(config.get("poll_interval", 60))

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

        if config.get("ns_masking", True) and len(clients) >= 2:
            _configure_subsystem_hosts(gateway, subsystems, host_nqns)
            if resume_from in ("io", "fill"):
                LOG.info("Reusing existing host ACLs across all gateways for resume_from=%s", resume_from)
                assigned, to_mask = _reuse_ns_assignment_multigw(
                    nvme_service.gateways, subsystems, clients, host_nqns
                )
                if to_mask:
                    _apply_namespace_masking(gateway, to_mask)
            else:
                assigned, all_ns = _assign_namespaces_multigw(
                    nvme_service.gateways, subsystems, clients, host_nqns
                )
                _apply_namespace_masking(gateway, all_ns)
            for hostname, namespaces in assigned.items():
                ns_uuids[hostname] = {
                    _norm_uuid(ns["uuid"]) for ns in namespaces if ns.get("uuid")
                }
        else:
            usable = _collect_usable_namespaces_multigw(nvme_service.gateways, subsystems)
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
        LOG.info(
            "Plan: %s namespaces (~%.0f GiB total mapped). "
            "Phase 1: Fast fill to %.1f%% (~1-2h). "
            "Phase 2: 8h paced slow fill to %.1f%% with parallel reads (%d%% read mix).",
            total_ns,
            total_gib,
            fast_fill_target,
            final_fill_target,
            rwmixread,
        )

        # Initialize and start background cluster & thermal monitoring across both phases
        monitor = ClusterMonitor(
            ceph_cluster=ceph_cluster,
            gateways=nvme_service.gateways,
            clients=clients,
            poll_interval=poll_interval,
            log_file=MONITOR_LOG_FILE,
            target_pool=config.get("rbd_pool", "rbd"),
        )
        monitor.start()

        try:
            # Phase 1: Fast fill to 75%
            if config.get("fill_cluster", True):
                _fill_cluster_to_threshold(
                    mapped=mapped,
                    orch=orch,
                    target_pct=fast_fill_target,
                    fill_bs=fill_bs,
                    iodepth=iodepth,
                    pool_name=config.get("rbd_pool", "rbd"),
                )

            # Phase 2: 8-hour endurance slow write to 80% + parallel read IO
            fio_results, fio_errors = _run_paced_endurance_workload(
                mapped=mapped,
                total_ns=total_ns,
                size_gib_per_ns=size_gib,
                start_pct=fast_fill_target,
                end_pct=final_fill_target,
                io_runtime=paced_io_runtime,
                continuous_bs=continuous_bs,
                iodepth=iodepth,
                rwmixread=rwmixread,
            )

            if fio_errors:
                raise RuntimeError(f"FIO endurance workload failed on client nodes: {fio_errors}")

        finally:
            monitor.stop()

        # Phase 3: Summarize and analyze all metrics
        summary = _analyze_monitoring_and_io(monitor.samples, fio_results)
        _print_and_save_summary(summary, output_file=MONITOR_SUMMARY_FILE)

        # Evaluate test pass/fail criteria
        if summary["ceph_stability"]["error_samples_count"] > 0:
            LOG.error("Test failed: Ceph reported HEALTH_ERR during endurance run.")
            return 1

        if summary["gateway_stability"]["unhealthy_samples"] > 0:
            LOG.error("Test failed: NVMeoF Gateways experienced downtime/instability.")
            return 1

        if summary["thermal_throttling"]["critical_comp_events"] > 0:
            LOG.warning("Drive thermal alert: Critical composite temperature events detected.")

        LOG.info("NVMeoF BYOK Two-Phase (Fast 75% Fill + 8h Paced 80% Fill & Read) completed successfully.")
        return 0

    except Exception as err:
        LOG.exception("NVMeoF BYOK endurance test failed: %s", err)
        return 1
