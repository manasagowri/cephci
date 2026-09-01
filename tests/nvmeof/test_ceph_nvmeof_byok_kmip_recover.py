"""Recover degraded BYOK namespaces by restarting stopped KMIP servers and gateways.

When one or more KMIP servers are down at gateway startup time the gateway
stores namespace OMAP records but cannot open the encrypted bdev.  Those
namespaces appear in ``ns list`` as ``degraded=true`` with an empty
``rbd_image_name``.

This test:

1. **Starts any stopped KMIP containers** on every KMIP node (skips nodes
   where the container is already running).
2. **Restarts every group2 gateway one at a time** via
   ``ceph orch daemon restart``, copying KMIP certs into the fresh
   container after each bounce so the gateway can reach the key server.
3. **Monitors ``ns list`` until every namespace is non-degraded**
   (``degraded=false`` *and* non-empty ``rbd_image_name``) across all
   subsystems, timing the recovery window per gateway and in total.

No FIO, no initiators, no RBD operations.  The test fails if the total
expected namespace count is not fully non-degraded within
``ns_reopen_timeout`` seconds (default 5400).
"""

import json
import time

from ceph.ceph import Ceph
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    _existing_subsystems,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _assert_gateways_ready,
    _copy_kmip_certs_when_containers_ready,
    _gw_unit_identity,
    _refresh_gateway_ssh,
)
from tests.nvmeof.test_ceph_nvmeof_byok_kmip_gw_restart import (
    DEFAULT_NS_REOPEN_TIMEOUT,
    DEFAULT_PID_CHANGE_DELAY,
    DEFAULT_PID_CHANGE_TIMEOUT,
    _wait_pid_changed,
)
from tests.nvmeof.workflows.byok_kmip import (
    DEFAULT_KMIP_IMAGE,
    KMIP_CONTAINER_NAME,
    deploy_kmip_server,
    short_hostname,
)
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

# Poll interval when waiting for namespaces to recover.
NS_POLL_DELAY = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kmip_container_running(node, name=KMIP_CONTAINER_NAME):
    """Return True if a container named *name* is running on *node*."""
    out, _ = node.exec_command(
        cmd=f"podman ps --filter name={name} --format '{{{{.ID}}}}'",
        sudo=True,
        check_ec=False,
    )
    return bool((out or "").strip())


def _ensure_kmip_servers_running(kmip_nodes, kmip_image):
    """Start KMIP containers on any node where they are not running.

    Nodes that already have a running container are skipped so that
    in-flight key fetches are not disrupted.

    Returns a list of nodes that were (re)started.
    """
    restarted = []
    for node in kmip_nodes:
        host = short_hostname(node)
        if _kmip_container_running(node):
            LOG.info("KMIP container already running on %s; skipping", host)
            continue
        LOG.info("KMIP container not running on %s; starting it", host)
        deploy_kmip_server(node, image=kmip_image)
        restarted.append(node)
        LOG.info("KMIP container started on %s", host)
    return restarted


def _list_namespaces(gateway, nqn):
    """Return the raw namespace list for *nqn*."""
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    return json.loads(out).get("namespaces", []) if out else []


def _count_degraded(gateway, subsystems):
    """Return ``(total, degraded_count, missing_image_count)`` across all subsystems."""
    total = degraded = missing_img = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        try:
            ns_list = _list_namespaces(gateway, nqn)
        except Exception as exc:
            LOG.warning("ns list failed on %s: %s", nqn, exc)
            continue
        for ns in ns_list:
            total += 1
            if ns.get("degraded") in (True, "true", "True", 1, "yes"):
                degraded += 1
            if not (ns.get("rbd_image_name") or "").strip():
                missing_img += 1
    return total, degraded, missing_img


def _wait_all_non_degraded(gateway, subsystems, timeout, poll_delay):
    """Poll until every namespace on every subsystem is non-degraded.

    Returns elapsed seconds on success.  Raises RuntimeError on timeout.
    """
    host = gateway.node.hostname
    started = time.time()
    deadline = started + timeout
    attempt = 0
    while True:
        attempt += 1
        elapsed = int(time.time() - started)
        total, degraded, missing_img = _count_degraded(gateway, subsystems)
        LOG.info(
            "%s recovery poll %s at %ss: total=%s degraded=%s missing_image=%s",
            host,
            attempt,
            elapsed,
            total,
            degraded,
            missing_img,
        )
        if total > 0 and degraded == 0 and missing_img == 0:
            LOG.info(
                "%s: all %s namespaces non-degraded after %ss",
                host,
                total,
                elapsed,
            )
            return elapsed
        if time.time() >= deadline:
            raise RuntimeError(
                f"{host}: {degraded}/{total} namespaces still degraded "
                f"(missing_image={missing_img}) after {elapsed}s "
                f"(timeout={timeout}s)"
            )
        time.sleep(poll_delay)


def _restart_gateway_and_wait(nvme_service, gateway, subsystems, config):
    """Restart one gateway daemon, copy KMIP certs, wait for all NS to recover.

    Returns a timing dict with keys: host, pid_ready_s, ns_usable_s, total_s.
    """
    host = gateway.node.hostname
    pid_timeout = int(config.get("gw_pid_timeout", DEFAULT_PID_CHANGE_TIMEOUT))
    pid_delay = int(config.get("gw_pid_delay", DEFAULT_PID_CHANGE_DELAY))
    ns_timeout = int(config.get("ns_reopen_timeout", DEFAULT_NS_REOPEN_TIMEOUT))
    gw_ready_tries = int(config.get("gw_ready_tries", 24))
    gw_ready_delay = int(config.get("gw_ready_delay", 10))

    try:
        _refresh_gateway_ssh(gateway)
    except Exception as exc:
        LOG.warning("SSH refresh to %s before restart: %s", host, exc)

    before = _gw_unit_identity(gateway)
    started = time.time()
    LOG.info("Restarting gateway %s (pid=%s)", host, before["main_pid"])
    nvme_service.restart_daemon(gateway, wait_sec=0)

    after = _wait_pid_changed(gateway, before, timeout=pid_timeout, delay=pid_delay)
    pid_ready_s = int(time.time() - started)
    LOG.info(
        "%s: daemon restarted pid %s -> %s in %ss",
        host,
        before["main_pid"],
        after["main_pid"],
        pid_ready_s,
    )

    # Copy KMIP certs into the fresh container so the gateway can reach the server.
    _copy_kmip_certs_when_containers_ready([gateway.node])

    # Wait for the gateway to declare itself ready.
    gateway.load_gateway_info(tries=gw_ready_tries, delay=gw_ready_delay)
    LOG.info("%s: gateway ready after restart", host)

    # Now wait for all namespaces to recover on this gateway.
    ns_started = time.time()
    ns_usable_s = _wait_all_non_degraded(
        gateway, subsystems, timeout=ns_timeout, poll_delay=NS_POLL_DELAY
    )
    total_s = int(time.time() - started)
    LOG.info(
        "%s: all namespaces non-degraded %ss after process ready (%ss total)",
        host,
        ns_usable_s,
        total_s,
    )
    return {
        "host": host,
        "pid_ready_s": pid_ready_s,
        "ns_usable_s": ns_usable_s,
        "total_s": total_s,
    }


def _log_recovery_summary(timings):
    """Log a per-gateway and total summary of recovery times."""
    if not timings:
        return
    LOG.info("=" * 60)
    LOG.info("KMIP recovery timing summary")
    LOG.info("=" * 60)
    for t in timings:
        LOG.info(
            "  %-20s  pid_ready=%4ss  ns_usable=%4ss  total=%4ss",
            t["host"],
            t["pid_ready_s"],
            t["ns_usable_s"],
            t["total_s"],
        )
    total_ns = sum(t["ns_usable_s"] for t in timings)
    LOG.info("  Total ns recovery time across all gateways: %ss", total_ns)
    LOG.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Start stopped KMIP servers, restart gateways, wait for all NS to recover.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    try:
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        for gw in nvme_service.gateways:
            gw.load_gateway_info()
        gateway = nvme_service.gateways[0]
        gateways = nvme_service.gateways

        subsystems = _existing_subsystems(gateway, config)

        # --- Phase 1: ensure every KMIP server is running ---
        kmip_nodes = ceph_cluster.get_nodes(role="kmip")
        if not kmip_nodes:
            # Fall back: any node tagged as a KMIP node in config
            kmip_node_ids = config.get("kmip_nodes") or []
            from ceph.utils import get_nodes_by_ids
            kmip_nodes = get_nodes_by_ids(ceph_cluster, kmip_node_ids) if kmip_node_ids else []
        if not kmip_nodes:
            raise ValueError(
                "No KMIP nodes found (role=kmip or config.kmip_nodes). "
                "Cannot start KMIP servers."
            )

        kmip_image = config.get("kmip_image", DEFAULT_KMIP_IMAGE)
        LOG.info(
            "Phase 1: ensuring KMIP containers are running on %s nodes",
            len(kmip_nodes),
        )
        restarted_nodes = _ensure_kmip_servers_running(kmip_nodes, kmip_image)
        if restarted_nodes:
            LOG.info(
                "Phase 1 complete: started KMIP on %s node(s): %s",
                len(restarted_nodes),
                [short_hostname(n) for n in restarted_nodes],
            )
            # Brief settle so the KMIP server finishes loading its key store.
            settle = int(config.get("kmip_start_settle_sec", 10))
            if settle > 0:
                LOG.info("Waiting %ss for KMIP servers to settle", settle)
                time.sleep(settle)
        else:
            LOG.info("Phase 1 complete: all KMIP servers were already running")

        # --- Phase 2: restart each gateway and wait for NS recovery ---
        LOG.info(
            "Phase 2: restarting %s gateway(s) one at a time and monitoring "
            "namespace recovery",
            len(gateways),
        )
        timings = []
        for index, gw in enumerate(gateways, start=1):
            LOG.info(
                "Gateway restart %s/%s: %s", index, len(gateways), gw.node.hostname
            )
            timing = _restart_gateway_and_wait(nvme_service, gw, subsystems, config)
            timings.append(timing)

            # Sanity-check peers are still active after each bounce.
            peers = [g for g in gateways if g is not gw]
            if peers:
                _assert_gateways_ready(
                    peers,
                    f"peers after {gw.node.hostname} restart",
                    tries=int(config.get("gw_ready_tries", 24)),
                    delay=int(config.get("gw_ready_delay", 10)),
                )

        _log_recovery_summary(timings)

        # --- Final verification on the first gateway ---
        total, degraded, missing_img = _count_degraded(gateway, subsystems)
        if degraded or missing_img:
            raise RuntimeError(
                f"Final check: {degraded}/{total} namespaces still degraded "
                f"(missing_image={missing_img}) after all gateways restarted"
            )
        LOG.info(
            "KMIP recovery test passed: all %s namespaces non-degraded "
            "after restarting %s gateway(s)",
            total,
            len(gateways),
        )
        return 0
    except Exception as err:
        LOG.exception("BYOK KMIP recovery test failed: %s", err)
        return 1
