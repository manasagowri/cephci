"""Add BYOK clones, FIO them, and assert gateways stay up.

Reuses an already-configured gateway group (dummy KMIP + subsystems).
Default: clone existing parent *namespaces*, then FIO those clone devices.
After each ``ns add`` and during IO, every gateway unit must stay active
with the same MainPID.

Set ``clone_only_ns: true`` to create stacked-LUKS clones whose parents are
RBD-encrypted only (never NVMe namespaces) and ``ns add`` only the clones.
That path prepares RBD images in parallel per subsystem, then ``ns add``
serially. After each ``ns add`` it waits ``ns_add_settle_sec`` (default 10s)
and checks gateway PIDs so peers can finish OMAP apply before the next clone.
Set ``check_gw_per_clone: false`` to restore the old once-per-NQN check.
Dual-client light FIO uses
one job per client; set ``fio_size`` to cap fill (default 1G). After IO
the gateways are restarted and every clone NS must re-open with the
stacked (clone, parent) ``encryption_entries`` chain in ``ns list``.
Use ``resume_from: io`` to skip create/ns add/mask and only reconnect +
FIO. Set ``skip_gw_restart: true`` to skip the post-IO restart check.
Set ``cleanup_orphan_images: true`` on the clone-only-NS path to delete
leftover ``orphan_`` parent images, snaps, and ``*_clone`` images (and
any clone NVMe namespaces) from a previous run before creating a new set.

Default is one clone per subsystem; set ``clones_per_subsystem`` to take
several from the same NQN (VM repro: 1 subsystem, 10 clones).
Set ``skip_io: true`` to stop after clone add (gateway-abort repro).
"""

import json
import logging
import os
import shlex
import time

from ceph.ceph import Ceph
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    CLONE_PASSPHRASE_FILE,
    DEFAULT_LISTENER_PORT,
    _assign_kmip_endpoints,
    _clone_context,
    _encrypt_clone,
    _encrypt_rbd_image,
    _existing_subsystems,
    _init_rbd,
    _listed_ns_map,
    _ns_add_clone,
    _parent_ns_meta,
    _rbd_cmd,
    _rbd_image_missing,
    _rbd_image_names,
    _rbd_resize,
    _recreate_protected_snap,
    _remove_existing_clone,
    _remove_snap_children,
    _sorted_kmip_nodes,
    _write_passphrase_file,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import (
    _add_ns_host,
    _allow_host,
    _apply_ns_mask,
    _change_ns_visibility,
    _configure_subsystem_hosts,
    _connect_client,
    _norm_uuid,
    _ns_record,
    _paths_for_uuids,
    _run_fio_job,
)
from tests.nvmeof.workflows.byok_kmip import (
    CLONE_PASSPHRASE_VALUE,
    DEFAULT_KMIP_CLI_IMAGE,
    copy_certs_into_gateway_containers,
    ensure_clone_passphrases_all,
    load_passphrases_all,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log
from utility.utils import run_fio

LOG = Log(__name__)

ABORT_MARKERS = (
    "abort_on_update_error",
    "Got error [0-9]+ while updating gateway state",
    "SystemExit:",
)
GW_SETTLE_SEC = 10
DEVICE_WAIT_TRIES = 6
DEVICE_WAIT_DELAY = 10
LIGHT_DEVICE_WAIT_TRIES = 12
ORPHAN_FIO_JOB = "/tmp/orphan_clone_io.fio"
BUG_SUMMARY_NAME = "clone_io_bug_summary.txt"
ORPHAN_BUG_SUMMARY_NAME = "orphan_clone_io_bug_summary.txt"
PARENT_PASSPHRASE_FILE = "/tmp/byok_parent_{fmt}.passphrase"
JOURNAL_HIGHLIGHTS = (
    "Wrong passphrase",
    "abort_on_update",
    "abort_server_on_update_error",
    "SystemExit",
    "Got error",
    "encryption_load2",
    "PermissionError",
    "Retrieved ",
    "context: None",
    "context=None",
    "Started ceph-",
    "Stopped",
    "Main PID",
)


def _run_log_dir():
    """Return the current cephci run directory (e.g. /tmp/cephci-run-XXXX)."""
    if LOG.log_dir:
        return LOG.log_dir
    try:
        for handler in logging.getLogger("cephci").handlers:
            path = getattr(handler, "baseFilename", None)
            if path:
                return os.path.dirname(os.path.abspath(path))
    except Exception:
        pass
    return None


def _short_host(gateway):
    return (gateway.node.hostname or "").split(".")[0]


def _pid_map(snapshot):
    return {item["hostname"]: item["main_pid"] for item in snapshot.values()}


def _collect_unit_journal(gateway, since_seconds):
    """Gateway unit journal for this clone window, minus monitor beacons."""
    unit = gateway.system_unit_id
    since = max(int(since_seconds), 1)
    cmd = (
        f"journalctl -u {unit} --since '{since} seconds ago' --no-pager "
        "| grep -v send_beacon || true"
    )
    out, _ = gateway.node.exec_command(sudo=True, cmd=cmd, check_ec=False)
    return out or ""


def _save_clone_gateway_logs(gateways, clone_index, ctx, since_seconds, prefix="clone"):
    """Write per-gateway journals into the cephci run dir.

    Files: ``<run_dir>/gateway_logs/<host>_<prefix><N>.log``
    """
    run_dir = _run_log_dir()
    if not run_dir:
        LOG.warning("No cephci log directory; skipping gateway journal save")
        return []
    out_dir = os.path.join(run_dir, "gateway_logs")
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    image = (ctx or {}).get("clone_image", "")
    nqn = (ctx or {}).get("nqn", "")
    for gw in gateways:
        host = _short_host(gw)
        path = os.path.join(out_dir, f"{host}_{prefix}{clone_index}.log")
        text = _collect_unit_journal(gw, since_seconds)
        header = (
            f"# host={host} clone={clone_index} image={image} nqn={nqn}\n"
            f"# journal last {int(since_seconds)}s (send_beacon lines omitted)\n"
        )
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(header)
            fh.write(text)
            if text and not text.endswith("\n"):
                fh.write("\n")
        saved.append(path)
        LOG.info("Saved gateway journal %s (%s bytes)", path, os.path.getsize(path))
    return saved


def _highlight_lines(text):
    lines = []
    for line in (text or "").splitlines():
        lower = line.lower()
        if any(token.lower() in lower for token in JOURNAL_HIGHLIGHTS):
            lines.append(line)
    return lines[-100:]


def _write_bug_summary(
    results,
    before,
    err,
    config,
    started,
    extra_after=None,
):
    """Write a Ceph-dev oriented summary of what ran and what failed."""
    run_dir = _run_log_dir()
    if not run_dir:
        LOG.warning("No cephci log directory; skipping bug summary")
        return None
    path = os.path.join(
        run_dir,
        ORPHAN_BUG_SUMMARY_NAME if config.get("clone_only_ns") else BUG_SUMMARY_NAME,
    )
    elapsed = int(time.time() - started) if started else 0
    pids_before = _pid_map(before) if before else {}
    pids_after = _pid_map(extra_after) if extra_after else {}
    failed = [item for item in results if item.get("status") not in ("ok",)]
    lines = [
        "NVMeoF BYOK clone IO stability — failure summary",
        "================================================",
        "",
        "Use this file plus gateway_logs/ when opening a ceph-nvmeof bug.",
        "",
        "What was tested",
        "---------------",
        (
            "Create RBD parents, encrypt them with rbd encryption format (never "
            "as NVMe namespaces), snap/clone, encrypt the clone, then ns-add only "
            "the stacked-LUKS clone."
            if config.get("clone_only_ns")
            else
            "Add encrypted RBD clones (stacked LUKS, opposite format of the parent) "
            "as NVMe-oF namespaces on an existing HA gateway group, then run FIO on "
            "those clone devices. Dummy KMIP supplies the passphrases. The test "
            "fails if any gateway in the group restarts or aborts while applying "
            "OMAP (peer path), even when origin CLI ns add succeeded."
        ),
        "",
        f"clone_count: {config.get('clone_count', 10)}",
        f"clone_only_ns: {bool(config.get('clone_only_ns'))}",
        f"gw_group: {config.get('gw_group', 'group2')}",
        f"rbd_pool: {config.get('rbd_pool', 'rbd')}",
        f"image_size: {config.get('image_size', '50G')}",
        f"io_runtime: {config.get('io_runtime', 600)}s {config.get('io_type', 'randrw')}",
        f"elapsed: {elapsed}s",
        "",
        "Gateway PIDs before clones",
        "--------------------------",
        str(pids_before) if pids_before else "(not captured)",
        "",
        "Gateway PIDs at failure",
        "-----------------------",
        str(pids_after) if pids_after else "(not captured)",
        "",
        "Clone attempts",
        "--------------",
    ]
    if not results:
        lines.append("(no clone attempts recorded)")
    for item in results:
        lines.append(
            f"clone {item['index']}: {item.get('status')} "
            f"image={item.get('image')} nqn={item.get('nqn')} "
            f"formats={item.get('clone_fmt')},{item.get('parent_fmt')} "
            f"key-ids={item.get('key_ids')} nsid={item.get('nsid')}"
        )
        if item.get("error"):
            lines.append(f"  error: {item['error']}")
        for log_path in item.get("logs") or []:
            lines.append(f"  log: {log_path}")
    lines.extend(
        [
            "",
            "Failure",
            "-------",
            str(err),
            "",
            "Expected",
            "--------",
            "Origin `ceph nvmeof ns add --encryption_format <clone>,<parent> "
            "--key_id <clone>,<parent>` succeeds, every peer applies the OMAP",
            "update without aborting, and all gateway systemd MainPIDs stay",
            "unchanged through clone add and FIO.",
            "",
            "Actual (from recent runs)",
            "-------------------------",
            "Origin CLI often succeeds (sometimes after retrying a transient",
            "librbd PermissionError that the gateway maps to 'Wrong passphrase').",
            "A peer then fails encryption_load2 on the same clone during OMAP",
            "apply (context: None), logs Wrong passphrase, and with",
            "abort_on_update_error=True exits: 'Got error 1 while updating",
            "gateway state, aborting gateway'. systemd restarts the unit.",
            "The origin gateway stays up.",
            "",
            "Journal highlights from saved clone logs",
            "----------------------------------------",
        ]
    )
    highlight_any = False
    for item in failed or results[-1:]:
        for log_path in item.get("logs") or []:
            try:
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    hits = _highlight_lines(fh.read())
            except OSError:
                continue
            if not hits:
                continue
            highlight_any = True
            lines.append(f"[{os.path.basename(log_path)}]")
            lines.extend(hits)
            lines.append("")
    if not highlight_any:
        lines.append("(no abort/passphrase highlights in saved journals)")
        lines.append("")
    lines.extend(
        [
            "Suggested bug title",
            "-------------------",
            "nvmeof: peer gateway aborts on OMAP apply of stacked-LUKS clone "
            "(encryption_load2 PermissionError mapped to Wrong passphrase)",
            "",
        ]
    )
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")
    LOG.info("Wrote clone IO bug summary %s", path)
    return path


def _ssh_stale_error(exc):
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "timed out",
            "timeout",
            "connection reset",
            "broken pipe",
            "not active",
            "no existing session",
            "socket is closed",
        )
    )


def _refresh_gateway_ssh(gateway):
    """Drop a stale Paramiko session and open a new one to this gateway."""
    node = gateway.node
    LOG.info(
        "Refreshing SSH to %s [%s]",
        node.hostname,
        getattr(node, "ip_address", ""),
    )
    node.reconnect()


def _gw_unit_identity(gateway, require_running=True):
    """Return MainPID / ActiveEnterTimestampMonotonic / ActiveState for the unit."""
    last = None
    for attempt in range(1, 4):
        try:
            return _gw_unit_identity_once(gateway, require_running)
        except Exception as exc:
            last = exc
            if attempt == 3 or not _ssh_stale_error(exc):
                raise
            LOG.warning(
                "SSH to %s failed (attempt %s/3); reconnecting: %s",
                gateway.node.hostname,
                attempt,
                exc,
            )
            try:
                _refresh_gateway_ssh(gateway)
            except Exception as rec_exc:
                LOG.warning(
                    "SSH reconnect to %s failed: %s",
                    gateway.node.hostname,
                    rec_exc,
                )
            time.sleep(5)
    raise last


def _gw_unit_identity_once(gateway, require_running=True):
    """Single SSH pass for gateway unit identity."""
    unit = gateway.system_unit_id
    if not unit:
        raise RuntimeError(f"Empty system_unit_id on {gateway.node.hostname}")
    out, _ = gateway.node.exec_command(
        sudo=True,
        cmd=(
            f"systemctl show {unit} "
            "-p MainPID -p ActiveEnterTimestampMonotonic -p ActiveState"
        ),
    )
    props = {}
    for line in (out or "").strip().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    identity = {
        "unit": unit,
        "main_pid": props.get("MainPID", ""),
        "active_ts": props.get("ActiveEnterTimestampMonotonic", ""),
        "active_state": props.get("ActiveState", ""),
        "hostname": gateway.node.hostname,
    }
    if not require_running:
        return identity
    if not identity["main_pid"] or identity["main_pid"] == "0":
        raise RuntimeError(
            f"Invalid MainPID={identity['main_pid']!r} on {identity['hostname']}"
        )
    if not identity["active_ts"] or identity["active_ts"] == "0":
        raise RuntimeError(
            f"Invalid ActiveEnterTimestamp on {identity['hostname']}"
        )
    if identity["active_state"] != "active":
        raise RuntimeError(
            f"Gateway {identity['hostname']} ActiveState="
            f"{identity['active_state']!r} (expected active)"
        )
    return identity


def _snapshot_gateways(gateways):
    return {gw.node.id: _gw_unit_identity(gw) for gw in gateways}


def _assert_gateways_unchanged(before, after, label):
    """Fail if any gateway restarted or left the active state."""
    failures = []
    for node_id, pre in before.items():
        post = after.get(node_id)
        if not post:
            failures.append(f"{label}: missing gateway {node_id}")
            continue
        host = post["hostname"]
        if post["active_state"] != "active":
            failures.append(
                f"{label}: {host} ActiveState={post['active_state']!r}"
            )
        if pre["main_pid"] != post["main_pid"]:
            failures.append(
                f"{label}: {host} MainPID {pre['main_pid']} -> {post['main_pid']}"
            )
        if pre["active_ts"] != post["active_ts"]:
            failures.append(
                f"{label}: {host} restarted (ActiveEnterTimestamp changed)"
            )
        if pre["unit"] != post["unit"]:
            failures.append(
                f"{label}: {host} unit {pre['unit']!r} -> {post['unit']!r}"
            )
    if failures:
        raise RuntimeError(
            "Gateway stability check failed:\n- " + "\n- ".join(failures)
        )


def _assert_gateways_ready(gateways, label, tries=12, delay=10):
    """Retry gateway info until each daemon is ready or the budget is spent."""
    for gw in gateways:
        try:
            gw.load_gateway_info(tries=tries, delay=delay)
        except Exception as exc:
            raise RuntimeError(
                f"{label}: gateway {gw.node.hostname} is not ready: {exc}"
            ) from exc
        LOG.info("%s: %s ready", label, gw.node.hostname)


def _scan_gateway_journals(gateways, since_seconds):
    """Fail if any gateway journal shows OMAP-apply abort markers."""
    hits = []
    grep_expr = "|".join(ABORT_MARKERS)
    since = max(int(since_seconds), 1)
    for gw in gateways:
        unit = gw.system_unit_id
        cmd = (
            f"journalctl -u {unit} --since '{since} seconds ago' --no-pager "
            f"| grep -E '{grep_expr}' || true"
        )
        out, _ = gw.node.exec_command(sudo=True, cmd=cmd, check_ec=False)
        text = (out or "").strip()
        if text:
            hits.append(f"[{gw.node.hostname}] journal hits:\n{text}")
    if hits:
        raise RuntimeError(
            "Gateway abort markers found in journals:\n" + "\n".join(hits)
        )


def _check_gateways(gateways, before, label, since_seconds=None):
    after = _snapshot_gateways(gateways)
    _assert_gateways_unchanged(before, after, label)
    _assert_gateways_ready(gateways, label)
    if since_seconds is not None:
        _scan_gateway_journals(gateways, since_seconds)
    LOG.info("%s: %s gateways still active with original PIDs", label, len(gateways))


def _watch_gateways(gateways, before, duration, interval, label="during clone IO"):
    deadline = time.time() + duration
    while True:
        remaining = deadline - time.time()
        _check_gateways(gateways, before, label)
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))


def _ns_by_image(gateway, nqn, image):
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    listed = json.loads(out).get("namespaces", []) if out else []
    for ns in listed:
        if ns.get("rbd_image_name") == image:
            return ns
    raise RuntimeError(f"Namespace for image {image} not listed on {nqn}")


def _ns_usable(ns):
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


def _orphan_image_name(sub_num, ns_index, fmt):
    """RBD parent name that is never added as an NVMe namespace."""
    return f"orphan_c{sub_num:02d}_n{ns_index:02d}_{fmt}"


def _is_orphan_image(name):
    return bool(name) and str(name).startswith("orphan_")


def _is_orphan_clone_image(name):
    return _is_orphan_image(name) and str(name).endswith("_clone")


def _is_orphan_parent_image(name):
    return _is_orphan_image(name) and not str(name).endswith("_clone")


def _delete_orphan_namespaces(gateway, subsystems, images):
    """Force-delete NVMe namespaces whose RBD image is a leftover orphan_."""
    wanted = set(images)
    removed = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        ns_map = _listed_ns_map(gateway, nqn)
        for image, nsid in list(ns_map.items()):
            if image not in wanted:
                continue
            LOG.info(
                "Removing leftover orphan namespace %s nsid=%s from %s",
                image,
                nsid,
                nqn,
            )
            try:
                gateway.namespace.delete(
                    **{"args": {"nqn": nqn, "nsid": nsid, "force": True}}
                )
                removed += 1
            except Exception as exc:
                LOG.warning(
                    "Failed to delete leftover namespace %s nsid=%s from %s: %s",
                    image,
                    nsid,
                    nqn,
                    exc,
                )
    return removed


def _delete_orphan_parent_image(rbd_obj, pool, image):
    """Unprotect/remove snaps and children, then delete an orphan_ parent."""
    try:
        snaps = rbd_obj.snap_ls(pool, image) or []
    except Exception as exc:
        LOG.warning("Could not list snaps on %s/%s: %s", pool, image, exc)
        snaps = []
    if isinstance(snaps, dict):
        snaps = [snaps]
    for snap in snaps:
        name = snap.get("name") if isinstance(snap, dict) else None
        if not name:
            continue
        snap_spec = f"{pool}/{image}@{name}"
        LOG.info("Removing leftover snapshot %s", snap_spec)
        _remove_snap_children(rbd_obj, snap_spec)
        _rbd_cmd(
            rbd_obj,
            f"rbd snap unprotect {snap_spec}",
            ok_substrings=("not protected", "is unprotected", "no such snapshot"),
        )
        _rbd_cmd(
            rbd_obj,
            f"rbd snap rm {snap_spec}",
            ok_substrings=("no such snapshot", "does not exist"),
        )
    LOG.info("Removing leftover parent image %s/%s", pool, image)
    rbd_obj.exec_cmd(cmd=f"rbd rm {pool}/{image}", check_ec=False)


def _cleanup_orphan_images(gateway, rbd_obj, subsystems, config):
    """Delete leftover clone-only-NS parents, snaps, and clones from a prior run.

    Only images whose names start with ``orphan_`` are touched: encrypted
    parents that are not NVMe namespaces, and their ``*_clone`` children.
    Clone namespaces are removed first so ``rbd rm`` can succeed.
    """
    pool = config.get("rbd_pool", "rbd")
    existing = _rbd_image_names(rbd_obj, pool)
    orphans = sorted(name for name in existing if _is_orphan_image(name))
    if not orphans:
        LOG.info("cleanup_orphan_images: no leftover orphan_ images in %s", pool)
        return
    clones = [name for name in orphans if _is_orphan_clone_image(name)]
    parents = [name for name in orphans if _is_orphan_parent_image(name)]
    LOG.info(
        "cleanup_orphan_images: deleting %s leftover clone(s) and %s parent(s) in %s",
        len(clones),
        len(parents),
        pool,
    )
    ns_removed = _delete_orphan_namespaces(gateway, subsystems, orphans)
    LOG.info("cleanup_orphan_images: removed %s leftover orphan namespace(s)", ns_removed)
    workers = max(1, int(config.get("rbd_parallel_workers", 8)))
    if clones:
        with parallel(max_workers=workers) as p:
            for clone in clones:
                LOG.info("Removing leftover clone image %s/%s", pool, clone)
                p.spawn(
                    rbd_obj.exec_cmd,
                    cmd=f"rbd rm {pool}/{clone}",
                    check_ec=False,
                )
            for _ in p:
                pass
    for parent in parents:
        _delete_orphan_parent_image(rbd_obj, pool, parent)
    remaining = sorted(
        name for name in _rbd_image_names(rbd_obj, pool) if _is_orphan_image(name)
    )
    if remaining:
        preview = ", ".join(remaining[:20])
        extra = f" ... and {len(remaining) - 20} more" if len(remaining) > 20 else ""
        LOG.warning(
            "cleanup_orphan_images: %s orphan_ image(s) still present: %s%s",
            len(remaining),
            preview,
            extra,
        )
        return
    LOG.info("cleanup_orphan_images: all leftover orphan_ images removed")


def _orphan_parent_ns_meta(sub, ns_index, ns_per_sub, passphrases_by_node):
    meta = _parent_ns_meta(sub, ns_index, ns_per_sub, passphrases_by_node)
    meta["image"] = _orphan_image_name(sub["num"], ns_index, meta["format"])
    return meta


def _remove_ns_if_present(gateway, nqn, image):
    """Delete an NVMe namespace for ``image`` if one is listed."""
    ns_map = _listed_ns_map(gateway, nqn)
    nsid = ns_map.get(image)
    if nsid is None:
        return
    LOG.warning(
        "Removing namespace %s nsid=%s from %s (must not remain as NVMe NS)",
        image,
        nsid,
        nqn,
    )
    try:
        gateway.namespace.delete(
            **{"args": {"nqn": nqn, "nsid": nsid, "force": True}}
        )
    except Exception as exc:
        LOG.warning(
            "Failed to delete namespace %s nsid=%s from %s: %s",
            image,
            nsid,
            nqn,
            exc,
        )


def _ensure_rbd_image(rbd_obj, pool, image, size, existing_rbd):
    """Create the RBD image if it is missing."""
    if image in existing_rbd or not _rbd_image_missing(rbd_obj, pool, image):
        LOG.info("Reusing RBD image %s/%s", pool, image)
        existing_rbd.add(image)
        return
    LOG.info("Creating RBD image %s/%s size=%s", pool, image, size)
    if rbd_obj.create_image(pool, image, size):
        raise RuntimeError(f"Failed to create RBD image {pool}/{image}")
    existing_rbd.add(image)


def _resize_after_clone_encryption(rbd_obj, pool, ctx, size):
    """Compensate LUKS header size after clone ``rbd encryption format``.

    Same rules as the parent-NS clone path and RBD stacked-LUKS docs:
    resize the parent again when stacking luks2 on luks1, and resize the
    clone whenever parent and clone formats differ.
    """
    if ctx["parent_fmt"] == "luks1" and ctx["clone_fmt"] == "luks2":
        LOG.info(
            "Resizing parent %s after clone encryption (luks1 -> luks2)",
            ctx["image"],
        )
        _rbd_resize(rbd_obj, pool, ctx["image"], size)
    if ctx["parent_fmt"] != ctx["clone_fmt"]:
        LOG.info(
            "Resizing clone %s after encryption (%s on %s)",
            ctx["clone_image"],
            ctx["clone_fmt"],
            ctx["parent_fmt"],
        )
        _rbd_resize(rbd_obj, pool, ctx["clone_image"], size)


def _ensure_orphan_parent(gateway, rbd_obj, ctx, config, existing_rbd):
    """Create and encrypt a parent RBD image that is not an NVMe namespace.

    Sequence: rbd create -> rbd encryption format -> rbd resize (LUKS
    header) -> later snap/clone. The parent must never appear in ``ns list``.
    """
    pool = config.get("rbd_pool", "rbd")
    size = config.get("image_size", "50G")
    nqn = ctx["nqn"]
    image = ctx["image"]
    _remove_ns_if_present(gateway, nqn, image)
    _ensure_rbd_image(rbd_obj, pool, image, size, existing_rbd)
    parent_file = PARENT_PASSPHRASE_FILE.format(fmt=ctx["parent_fmt"])
    _write_passphrase_file(rbd_obj, parent_file, ctx["parent_key"]["value"])
    LOG.info(
        "Encrypting parent %s/%s format=%s (not adding as NVMe namespace)",
        pool,
        image,
        ctx["parent_fmt"],
    )
    _encrypt_rbd_image(rbd_obj, pool, image, ctx["parent_fmt"], parent_file)
    LOG.info(
        "Resizing parent %s after encryption to compensate LUKS header",
        image,
    )
    _rbd_resize(rbd_obj, pool, image, size)


def _assert_parent_not_namespace(gateway, nqn, image):
    ns_map = _listed_ns_map(gateway, nqn)
    if image in ns_map:
        raise RuntimeError(
            f"Parent image {image} is listed as nsid={ns_map[image]} on {nqn}; "
            "clone_only_ns requires the parent not to be an NVMe namespace"
        )


def _pick_orphan_clone_targets(subsystems, passphrases_by_node, config):
    """Build clone targets from new RBD parents (not existing NVMe namespaces)."""
    count = int(config.get("clone_count", 10))
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    per_sub = int(config.get("clones_per_subsystem", count))
    targets = []
    for sub in subsystems:
        if len(targets) >= count:
            break
        taken = 0
        for ns_index in range(1, ns_per_sub + 1):
            if len(targets) >= count or taken >= per_sub:
                break
            ns_meta = _orphan_parent_ns_meta(
                sub, ns_index, ns_per_sub, passphrases_by_node
            )
            ctx = _clone_context(sub, ns_meta, passphrases_by_node)
            ctx["ns_meta"] = ns_meta
            targets.append(ctx)
            taken += 1
            LOG.info(
                "Orphan clone target %s: %s parent=%s -> %s "
                "formats parent=%s clone=%s",
                len(targets),
                ctx["nqn"],
                ctx["image"],
                ctx["clone_image"],
                ctx["parent_fmt"],
                ctx["clone_fmt"],
            )
        if taken == 0:
            LOG.info("%s: no orphan clone slots available", sub["group_nqn"])
    if len(targets) < count:
        raise RuntimeError(
            f"Need {count} orphan clone targets, built {len(targets)}"
        )
    return targets


def _preferred_ns_indexes(sub_num, ns_per_sub):
    """Alternate LUKS1 / LUKS2 parent indexes, then the rest."""
    half = max(ns_per_sub // 2, 1)
    preferred = 1 if sub_num % 2 else min(half + 1, ns_per_sub)
    rest = [idx for idx in range(1, ns_per_sub + 1) if idx != preferred]
    return [preferred] + rest


def _pick_clone_targets(gateway, subsystems, passphrases_by_node, config, rbd_obj):
    """Pick unused parents until ``clone_count`` is reached.

    Default is one clone per subsystem (baremetal scale). Set
    ``clones_per_subsystem`` to take several parents from the same NQN
    (VM repro: 1 subsystem, 10 parents, 10 clones).
    """
    count = int(config.get("clone_count", 10))
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    per_sub = int(config.get("clones_per_subsystem", 1))
    pool = config.get("rbd_pool", "rbd")
    targets = []
    for sub in subsystems:
        if len(targets) >= count:
            break
        nqn = sub["group_nqn"]
        ns_map = _listed_ns_map(gateway, nqn)
        listed = {}
        out, _ = gateway.namespace.list(
            **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
        )
        for ns in json.loads(out).get("namespaces", []) if out else []:
            name = ns.get("rbd_image_name")
            if name:
                listed[name] = ns
        taken = 0
        for ns_index in _preferred_ns_indexes(sub["num"], ns_per_sub):
            if len(targets) >= count or taken >= per_sub:
                break
            ns_meta = _parent_ns_meta(sub, ns_index, ns_per_sub, passphrases_by_node)
            image = ns_meta["image"]
            clone_image = f"{image}_clone"
            parent_ns = listed.get(image)
            if parent_ns is None or not _ns_usable(parent_ns):
                continue
            if clone_image in ns_map:
                continue
            if _rbd_image_missing(rbd_obj, pool, image):
                continue
            ctx = _clone_context(sub, ns_meta, passphrases_by_node)
            ctx["ns_meta"] = ns_meta
            targets.append(ctx)
            taken += 1
            LOG.info(
                "Clone target %s: %s/%s -> %s formats parent=%s clone=%s",
                len(targets),
                nqn,
                ctx["image"],
                ctx["clone_image"],
                ctx["parent_fmt"],
                ctx["clone_fmt"],
            )
        if taken == 0:
            LOG.info("%s: no unused parent available for a new clone", nqn)
    if len(targets) < count:
        raise RuntimeError(
            f"Need {count} unused parent namespaces to clone, found {len(targets)}"
        )
    return targets


def _prepare_clone_image(gateway, rbd_obj, ctx, config, existing_rbd):
    """Create/encrypt parent (clone_only_ns), snap, clone, and encrypt the clone."""
    pool = config.get("rbd_pool", "rbd")
    size = config.get("image_size", "50G")
    nqn = ctx["nqn"]
    if config.get("clone_only_ns"):
        _ensure_orphan_parent(gateway, rbd_obj, ctx, config, existing_rbd)
    ns_map = _listed_ns_map(gateway, nqn)
    _remove_existing_clone(
        gateway,
        rbd_obj,
        pool,
        nqn,
        ctx["clone_image"],
        ns_map,
        existing_rbd,
    )
    _rbd_resize(rbd_obj, pool, ctx["image"], size)
    _recreate_protected_snap(rbd_obj, pool, ctx["image"], ctx["snap"])
    LOG.info(
        "Cloning %s/%s -> %s format=%s",
        pool,
        ctx["image"],
        ctx["clone_image"],
        ctx["clone_fmt"],
    )
    if rbd_obj.create_clone(
        f"{pool}/{ctx['image']}@{ctx['snap']}", pool, ctx["clone_image"]
    ):
        raise RuntimeError(
            f"Failed to clone {pool}/{ctx['image']}@{ctx['snap']}"
        )
    existing_rbd.add(ctx["clone_image"])
    _encrypt_clone(
        rbd_obj,
        pool,
        ctx["clone_image"],
        ctx["clone_fmt"],
        ctx["clone_key"],
        passphrase_file=CLONE_PASSPHRASE_FILE,
    )
    _resize_after_clone_encryption(rbd_obj, pool, ctx, size)
    return ctx


def _ns_add_prepared_clone(gateway, ctx, config):
    """ns-add a prepared stacked-LUKS clone and record nsid/uuid."""
    pool = config.get("rbd_pool", "rbd")
    nqn = ctx["nqn"]
    LOG.info(
        "ns add clone %s image=%s formats=%s,%s key-ids=%s,%s",
        nqn,
        ctx["clone_image"],
        ctx["clone_fmt"],
        ctx["parent_fmt"],
        ctx["clone_key"]["uuid"],
        ctx["parent_key"]["uuid"],
    )
    _ns_add_clone(
        gateway,
        {
            "nqn": nqn,
            "rbd_pool": pool,
            "rbd_image_name": ctx["clone_image"],
            "encryption-format": f"{ctx['clone_fmt']},{ctx['parent_fmt']}",
            "key-id": f"{ctx['clone_key']['uuid']},{ctx['parent_key']['uuid']}",
        },
    )
    listed = _ns_by_image(gateway, nqn, ctx["clone_image"])
    ctx["nsid"] = listed.get("nsid")
    ctx["uuid"] = listed.get("uuid")
    if not ctx["nsid"] or not ctx["uuid"]:
        raise RuntimeError(
            f"Clone {ctx['clone_image']} on {nqn} missing nsid/uuid: {listed}"
        )
    if config.get("clone_only_ns"):
        _assert_parent_not_namespace(gateway, nqn, ctx["image"])
    return ctx


def _add_one_clone(gateway, rbd_obj, ctx, config, existing_rbd):
    """Snapshot, clone, encrypt, and ns-add one clone. Fail the test on error.

    When ``clone_only_ns`` is set, create and encrypt the parent RBD image
    first (never as an NVMe namespace), including ``rbd resize`` after
    parent encryption. After clone encryption the same LUKS-header resize
    rules as the parent-NS path apply.
    """
    _prepare_clone_image(gateway, rbd_obj, ctx, config, existing_rbd)
    return _ns_add_prepared_clone(gateway, ctx, config)


def _clone_result_entry(index, ctx):
    return {
        "index": index,
        "image": ctx["clone_image"],
        "nqn": ctx["nqn"],
        "parent_fmt": ctx["parent_fmt"],
        "clone_fmt": ctx["clone_fmt"],
        "key_ids": f"{ctx['clone_key']['uuid']},{ctx['parent_key']['uuid']}",
        "nsid": ctx.get("nsid"),
        "status": "in_progress",
        "error": None,
        "logs": [],
    }


def _targets_by_nqn(targets):
    """Group clone targets in first-seen subsystem order."""
    groups = []
    index = {}
    for ctx in targets:
        nqn = ctx["nqn"]
        if nqn not in index:
            index[nqn] = []
            groups.append(index[nqn])
        index[nqn].append(ctx)
    return groups


def _discover_orphan_clone_targets(
    gateway, subsystems, passphrases_by_node, config
):
    """Rebuild clone_only_ns targets from clone namespaces already on the GWs."""
    count = int(config.get("clone_count", 10))
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    per_sub = int(config.get("clones_per_subsystem", count))
    targets = []
    for sub in subsystems:
        if len(targets) >= count:
            break
        listed = {}
        out, _ = gateway.namespace.list(
            **{
                "base_cmd_args": {"format": "json"},
                "args": {"subsystem": sub["group_nqn"]},
            }
        )
        for ns in json.loads(out).get("namespaces", []) if out else []:
            name = ns.get("rbd_image_name")
            if name:
                listed[name] = ns
        taken = 0
        for ns_index in range(1, ns_per_sub + 1):
            if len(targets) >= count or taken >= per_sub:
                break
            ns_meta = _orphan_parent_ns_meta(
                sub, ns_index, ns_per_sub, passphrases_by_node
            )
            ctx = _clone_context(sub, ns_meta, passphrases_by_node)
            ctx["ns_meta"] = ns_meta
            ns = listed.get(ctx["clone_image"])
            if not ns or not ns.get("nsid") or not ns.get("uuid"):
                continue
            ctx["nsid"] = ns["nsid"]
            ctx["uuid"] = ns["uuid"]
            targets.append(ctx)
            taken += 1
            LOG.info(
                "resume_from=io: %s nsid=%s uuid=%s",
                ctx["clone_image"],
                ctx["nsid"],
                ctx["uuid"],
            )
    if len(targets) < count:
        raise RuntimeError(
            f"resume_from=io: need {count} orphan clone namespaces, found {len(targets)}"
        )
    return targets


def _add_orphan_clones_batched(
    gateway,
    rbd_obj,
    targets,
    config,
    existing_rbd,
    gateways,
    before,
    started,
    save_gw_logs,
    log_prefix,
    results,
):
    """Prepare clones in parallel per subsystem, then ns-add one at a time.

    Default waits ``ns_add_settle_sec`` after each ``ns add`` and checks
    gateway PIDs before the next clone, so peer OMAP apply is not stacked
    on top of the following origin ``encryption_load2``.
    """
    groups = _targets_by_nqn(targets)
    workers = int(config.get("rbd_parallel_workers", 8))
    settle = int(config.get("ns_add_settle_sec", GW_SETTLE_SEC))
    check_each = config.get("check_gw_per_clone", True)
    clone_index = 0
    for sub_i, batch in enumerate(groups, start=1):
        nqn = batch[0]["nqn"]
        LOG.info(
            "========== subsystem %s/%s %s (%s clones) ==========",
            sub_i,
            len(groups),
            nqn,
            len(batch),
        )
        sub_t0 = time.time()
        fmts = {}
        for ctx in batch:
            fmt = ctx["parent_fmt"]
            if fmt not in fmts:
                fmts[fmt] = ctx["parent_key"]["value"]
        for fmt, value in fmts.items():
            _write_passphrase_file(
                rbd_obj, PARENT_PASSPHRASE_FILE.format(fmt=fmt), value
            )
        try:
            with parallel(max_workers=workers) as p:
                for ctx in batch:
                    p.spawn(
                        _prepare_clone_image,
                        gateway,
                        rbd_obj,
                        ctx,
                        config,
                        existing_rbd,
                    )
                for result in p:
                    if isinstance(result, Exception):
                        raise result
        except Exception as exc:
            for ctx in batch:
                clone_index += 1
                result = _clone_result_entry(clone_index, ctx)
                result["status"] = "prepare_failed"
                result["error"] = str(exc)
                results.append(result)
            if save_gw_logs:
                logs = _save_clone_gateway_logs(
                    gateways,
                    sub_i,
                    batch[-1],
                    time.time() - sub_t0 + 5,
                    prefix=log_prefix,
                )
                for item in results[-len(batch) :]:
                    item["logs"] = logs
            raise
        for ctx in batch:
            clone_index += 1
            result = _clone_result_entry(clone_index, ctx)
            results.append(result)
            clone_t0 = time.time()
            try:
                _ns_add_prepared_clone(gateway, ctx, config)
                result["status"] = "ns_add_ok"
                result["nsid"] = ctx.get("nsid")
            except Exception as exc:
                result["status"] = "ns_add_failed"
                result["error"] = str(exc)
                if save_gw_logs:
                    result["logs"] = _save_clone_gateway_logs(
                        gateways,
                        clone_index,
                        ctx,
                        time.time() - clone_t0 + 5,
                        prefix=log_prefix,
                    )
                raise
            if settle:
                time.sleep(settle)
            if check_each:
                try:
                    _check_gateways(
                        gateways,
                        before,
                        f"after ns add {ctx['clone_image']}",
                        since_seconds=time.time() - clone_t0 + 5,
                    )
                    result["status"] = "ok"
                except Exception as exc:
                    result["status"] = "gateway_check_failed"
                    result["error"] = str(exc)
                    if save_gw_logs:
                        result["logs"] = _save_clone_gateway_logs(
                            gateways,
                            clone_index,
                            ctx,
                            time.time() - clone_t0 + 5,
                            prefix=log_prefix,
                        )
                    raise
        if save_gw_logs and not check_each:
            logs = _save_clone_gateway_logs(
                gateways,
                sub_i,
                batch[-1],
                time.time() - sub_t0 + 5,
                prefix=log_prefix,
            )
            for item in results[-len(batch) :]:
                item["logs"] = logs
        if not check_each:
            try:
                _check_gateways(
                    gateways,
                    before,
                    f"after ns add {nqn}",
                    since_seconds=time.time() - sub_t0 + 5,
                )
            except Exception as exc:
                for item in results[-len(batch) :]:
                    if item["status"] == "ns_add_ok":
                        item["status"] = "gateway_check_failed"
                        item["error"] = str(exc)
                if save_gw_logs:
                    logs = _save_clone_gateway_logs(
                        gateways,
                        sub_i,
                        batch[-1],
                        time.time() - sub_t0 + 5,
                        prefix=log_prefix,
                    )
                    for item in results[-len(batch) :]:
                        item["logs"] = logs
                raise
            for item in results[-len(batch) :]:
                if item["status"] == "ns_add_ok":
                    item["status"] = "ok"
        LOG.info(
            "%s: %s orphan clones added in %ss (test elapsed %ss)",
            nqn,
            len(batch),
            int(time.time() - sub_t0),
            int(time.time() - started),
        )


def _orphan_ns_records(targets, clients, host_nqns):
    """Masking records for clone_only_ns clones, round-robin across clients."""
    records = []
    for ctx in targets:
        record = _ns_record(
            ctx["nqn"],
            {
                "nsid": ctx["nsid"],
                "uuid": ctx["uuid"],
                "rbd_image_name": ctx["clone_image"],
            },
        )
        records.append(record)
    for index, record in enumerate(records):
        client = clients[index % len(clients)]
        record["owner"] = client.hostname
        record["owner_nqn"] = host_nqns[client.hostname]
    assigned = {client.hostname: [] for client in clients}
    for record in records:
        assigned[record["owner"]].append(record)
    return records, assigned


def _write_light_fio_job(node, job_path, devices, global_opts):
    """One FIO section for all devices so iodepth is per client, not per NS."""
    script = (
        "from pathlib import Path\n"
        f"devices = {json.dumps(list(devices))}\n"
        f"opts = {json.dumps(global_opts)}\n"
        "lines = ['[global]']\n"
        "for key, value in opts.items():\n"
        "    lines.append(f'{key}={value}')\n"
        "lines.append('[orphan_clone]')\n"
        "lines.append('filename=' + ':'.join(devices))\n"
        f"Path({json.dumps(job_path)}).write_text('\\n'.join(lines) + '\\n')\n"
    )
    node.exec_command(cmd=f"python3 -c {shlex.quote(script)}", sudo=True)


def _wait_for_clone_paths_client(initiator, uuids, hostname):
    last_paths = []
    tries = LIGHT_DEVICE_WAIT_TRIES
    for attempt in range(1, tries + 1):
        last_paths = _paths_for_uuids(initiator, uuids)
        LOG.info(
            "%s clone devices visible=%s expected=%s (attempt %s/%s)",
            hostname,
            len(last_paths),
            len(uuids),
            attempt,
            tries,
        )
        if len(last_paths) >= len(uuids):
            return last_paths
        time.sleep(DEVICE_WAIT_DELAY)
    raise RuntimeError(
        f"{hostname}: expected {len(uuids)} clone devices, found {len(last_paths)}"
    )


def _run_orphan_clone_io(mapped, config, gateways, before):
    """Light FIO: one job per client, optional size cap to limit cluster fill."""
    io_runtime = int(config.get("io_runtime", 600))
    io_type = config.get("io_type", "randrw")
    bs = config.get("bs", "64k")
    iodepth = str(config.get("iodepth", 4))
    interval = int(config.get("gw_poll_interval", 15))
    fio_size = config.get("fio_size", "1G")
    total = sum(len(paths) for _, _, paths in mapped.values())
    LOG.info(
        "FIO %s on %s orphan clone devices for %ss (bs=%s iodepth=%s "
        "size=%s, one job/client)",
        io_type,
        total,
        io_runtime,
        bs,
        iodepth,
        fio_size,
    )
    for client, _, paths in mapped.values():
        if not paths:
            raise RuntimeError(f"No clone devices on {client.hostname}")
        opts = {
            "ioengine": "libaio",
            "direct": "1",
            "bs": bs,
            "rw": io_type,
            "iodepth": iodepth,
            "group_reporting": "1",
            "time_based": "1",
            "runtime": str(io_runtime),
            "numjobs": "1",
        }
        if fio_size:
            opts["size"] = str(fio_size)
        _write_light_fio_job(client, ORPHAN_FIO_JOB, paths, opts)
    errors = []
    with parallel() as p:
        for client, _, _ in mapped.values():
            p.spawn(_run_fio_job, client, ORPHAN_FIO_JOB)
        p.spawn(
            _watch_gateways,
            gateways,
            before,
            io_runtime + 30,
            interval,
            "during orphan clone IO",
        )
        for result in p:
            if isinstance(result, int) and result != 0:
                errors.append(result)
    if errors:
        raise RuntimeError(f"FIO {ORPHAN_FIO_JOB} failed with {errors}")


def _mask_clones(gateway, targets, host_nqn):
    nqns = []
    seen = set()
    for ctx in targets:
        nqn = ctx["nqn"]
        if nqn not in seen:
            _allow_host(gateway, nqn, host_nqn)
            seen.add(nqn)
            nqns.append(nqn)
        _change_ns_visibility(gateway, nqn, ctx["nsid"])
        _add_ns_host(gateway, nqn, ctx["nsid"], host_nqn)
        LOG.info(
            "Masked clone %s nsid=%s uuid=%s to host on %s",
            ctx["clone_image"],
            ctx["nsid"],
            ctx["uuid"],
            nqn,
        )
    return nqns


def _connect_clone_subsystems(initiator, gateways, nqns, port):
    for gateway in gateways:
        LOG.info(
            "Connecting %s subsystems via %s (%s)",
            len(nqns),
            gateway.node.hostname,
            gateway.node.ip_address,
        )
        initiator.connect_targets(
            gateway,
            {
                "nqn": "discover-all",
                "subsystems": nqns,
                "listener_port": port,
            },
        )


def _wait_for_clone_paths(initiator, uuids):
    last_paths = []
    for attempt in range(1, DEVICE_WAIT_TRIES + 1):
        last_paths = _paths_for_uuids(initiator, uuids)
        LOG.info(
            "Clone devices visible=%s expected=%s (attempt %s/%s)",
            len(last_paths),
            len(uuids),
            attempt,
            DEVICE_WAIT_TRIES,
        )
        if len(last_paths) >= len(uuids):
            return last_paths
        time.sleep(DEVICE_WAIT_DELAY)
    raise RuntimeError(
        f"Expected {len(uuids)} clone devices, found {len(last_paths)}"
    )


def _run_clone_io(client, paths, config, gateways, before):
    io_runtime = int(config.get("io_runtime", 600))
    io_type = config.get("io_type", "randrw")
    bs = config.get("bs", "64k")
    iodepth = str(config.get("iodepth", 16))
    interval = int(config.get("gw_poll_interval", 15))
    LOG.info(
        "FIO %s on %s clone devices for %ss (bs=%s iodepth=%s)",
        io_type,
        len(paths),
        io_runtime,
        bs,
        iodepth,
    )
    errors = []
    with parallel() as p:
        for path in paths:
            p.spawn(
                run_fio,
                device_name=path,
                client_node=client,
                io_type=io_type,
                run_time=io_runtime,
                bs=bs,
                iodepth=iodepth,
                long_running=True,
                cmd_timeout="notimeout",
            )
        p.spawn(_watch_gateways, gateways, before, io_runtime + 30, interval)
        for result in p:
            if isinstance(result, int) and result != 0:
                errors.append(result)
    if errors:
        raise RuntimeError(f"FIO failed with exit codes {errors}")


def _run_clone_only_client_io(
    gateway,
    gateways,
    subsystems,
    targets,
    clients,
    config,
    before,
    started,
    resume_io,
):
    """Mask clone NS across clients, connect, and run light FIO on clones only."""
    if not clients:
        raise ValueError("clone-only-NS IO requires a client node")
    host_nqns = {}
    initiators = {}
    for client in clients:
        initiator = NVMeInitiator(client)
        initiator.disconnect_all()
        initiators[client.hostname] = initiator
        host_nqns[client.hostname] = initiator.initiator_nqn()
        LOG.info("Client %s host NQN %s", client.hostname, host_nqns[client.hostname])
    _configure_subsystem_hosts(gateway, subsystems, host_nqns)
    records, assigned = _orphan_ns_records(targets, clients, host_nqns)
    if resume_io:
        LOG.info(
            "resume_from=io: skipping mask of %s orphan clone namespaces",
            len(records),
        )
    else:
        LOG.info(
            "Masking %s orphan clone namespaces across %s clients",
            len(records),
            len(clients),
        )
        with parallel(max_workers=2) as p:
            for ns in records:
                p.spawn(_apply_ns_mask, gateway, ns)
            for result in p:
                if isinstance(result, Exception):
                    raise result
    port = config.get("listener_port", DEFAULT_LISTENER_PORT)
    mapped = {}
    for client in clients:
        initiator = initiators[client.hostname]
        _connect_client(initiator, gateways, subsystems, port)
        time.sleep(5)
        uuids = [_norm_uuid(ns["uuid"]) for ns in assigned[client.hostname]]
        paths = _wait_for_clone_paths_client(initiator, uuids, client.hostname)
        mapped[client.hostname] = (client, initiator, paths)
        LOG.info(
            "%s connected with %s orphan clone namespaces",
            client.hostname,
            len(paths),
        )
    _run_orphan_clone_io(mapped, config, gateways, before)
    _check_gateways(
        gateways,
        before,
        "after orphan clone IO",
        since_seconds=time.time() - started,
    )


def _expected_key_chain(ctx):
    """Stacked LUKS chain as stored in ``ns list``: clone layer, then parent."""
    return [
        {
            "format": str(ctx["clone_fmt"]).lower(),
            "key_id": str(ctx["clone_key"]["uuid"]),
        },
        {
            "format": str(ctx["parent_fmt"]).lower(),
            "key_id": str(ctx["parent_key"]["uuid"]),
        },
    ]


def _listed_key_chain(ns):
    entries = ns.get("encryption_entries") or []
    return [
        {
            "format": str(item.get("format") or "").lower(),
            "key_id": str(item.get("key_id") or ""),
        }
        for item in entries
    ]


def _clone_reopen_error(ns, ctx):
    """Return a reason string if the clone NS did not re-open correctly."""
    if not ns:
        return "missing from ns list"
    if not _ns_usable(ns):
        return (
            f"not usable (degraded={ns.get('degraded')} "
            f"size={ns.get('rbd_image_size')} bdev={ns.get('bdev_name')})"
        )
    actual = _listed_key_chain(ns)
    expected = _expected_key_chain(ctx)
    if actual != expected:
        return f"key chain {actual} != expected {expected}"
    return None


def _copy_kmip_certs_when_containers_ready(gw_nodes, tries=18, delay=10):
    """Wait for nvmeof containers after restart, then copy host KMIP certs in."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            copy_certs_into_gateway_containers(gw_nodes)
            return
        except Exception as exc:
            last = exc
            LOG.warning(
                "KMIP cert copy into GW containers attempt %s/%s: %s",
                attempt,
                tries,
                exc,
            )
            if attempt < tries:
                time.sleep(delay)
    raise RuntimeError(f"Could not copy KMIP certs into GW containers: {last}")


def _assert_clones_reopened(gateways, targets):
    """Every GW must list every clone NS as usable with the stacked key chain."""
    errors = []
    expected_by_nqn = {}
    for ctx in targets:
        expected_by_nqn.setdefault(ctx["nqn"], []).append(ctx)
    for gw in gateways:
        host = gw.node.hostname
        for nqn, batch in expected_by_nqn.items():
            try:
                out, _ = gw.namespace.list(
                    **{
                        "base_cmd_args": {"format": "json"},
                        "args": {"subsystem": nqn},
                    }
                )
                listed = {}
                for ns in json.loads(out).get("namespaces", []) if out else []:
                    name = ns.get("rbd_image_name")
                    if name:
                        listed[name] = ns
            except Exception as exc:
                errors.append(f"{host} {nqn}: ns list failed: {exc}")
                continue
            for ctx in batch:
                err = _clone_reopen_error(listed.get(ctx["clone_image"]), ctx)
                if err:
                    errors.append(f"{host} {ctx['clone_image']}: {err}")
    if errors:
        preview = "\n".join(errors[:40])
        extra = f"\n... and {len(errors) - 40} more" if len(errors) > 40 else ""
        raise RuntimeError(
            f"{len(errors)} clone NS reopen check(s) failed:\n{preview}{extra}"
        )
    LOG.info(
        "All %s clone namespaces reopened on %s gateways with 2-entry key chains",
        len(targets),
        len(gateways),
    )


def _assert_clones_reopened_with_retries(gateways, targets, tries=18, delay=15):
    last = None
    for attempt in range(1, tries + 1):
        try:
            _assert_clones_reopened(gateways, targets)
            return
        except Exception as exc:
            last = exc
            LOG.warning(
                "Clone NS reopen check attempt %s/%s: %s",
                attempt,
                tries,
                exc,
            )
            if attempt < tries:
                time.sleep(delay)
    raise RuntimeError(f"Clone namespaces did not re-open after GW restart: {last}")


def _restart_gateways_and_verify_clones(nvme_service, targets, config):
    """Restart the gateway group and confirm stacked-LUKS clones re-open."""
    LOG.info(
        "Restarting all NVMeoF gateways, then verifying %s clone namespaces "
        "re-open with (clone, parent) encryption_entries",
        len(targets),
    )
    nvme_service.restart(wait_sec=int(config.get("gw_restart_wait", 15)))
    gw_nodes = [gw.node for gw in nvme_service.gateways]
    _copy_kmip_certs_when_containers_ready(gw_nodes)
    nvme_service.wait_for_gateways(
        tries=int(config.get("gw_ready_tries", 24)),
        delay=int(config.get("gw_ready_delay", 10)),
    )
    _assert_clones_reopened_with_retries(
        nvme_service.gateways,
        targets,
        tries=int(config.get("ns_reopen_tries", 18)),
        delay=int(config.get("ns_reopen_delay", 15)),
    )


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Create BYOK clones, FIO them, and keep group2 gateways alive.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)
    started = time.time()
    save_gw_logs = config.get("save_gateway_logs", True)
    clone_only_ns = bool(config.get("clone_only_ns"))
    resume_io = clone_only_ns and config.get("resume_from") == "io"
    log_prefix = "orphan_sub" if clone_only_ns else "clone"
    results = []
    before = {}
    gateways = []
    existing_rbd = set()

    try:
        rbd_obj = _init_rbd(kwargs)
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        if clone_only_ns and (
            config.get("gw_max_namespaces")
            or config.get("max_namespaces_per_subsystem")
        ):
            spec_changed = nvme_service.ensure_namespace_limits(
                max_namespaces=int(config.get("gw_max_namespaces", 4096)),
                max_namespaces_per_subsystem=int(
                    config.get("max_namespaces_per_subsystem", 512)
                ),
                max_namespaces_with_netmask=int(
                    config.get("max_namespaces_with_netmask", 4096)
                ),
            )
            if spec_changed:
                LOG.info(
                    "NVMeoF spec updated; waiting for gateways and recopying KMIP certs"
                )
                time.sleep(30)
                nvme_service.wait_for_gateways()
                copy_certs_into_gateway_containers(
                    [gw.node for gw in nvme_service.gateways]
                )
                nvme_service.wait_for_gateways()
        gateway = nvme_service.gateways[0]
        gateways = nvme_service.gateways
        clients = ceph_cluster.get_nodes(role="client")
        if not clients:
            raise ValueError("clone IO requires a client node")
        client = clients[0]

        kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
        kmip_cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)
        ensure_clone_passphrases_all(kmip_nodes, cli_image=kmip_cli_image)
        passphrases_by_node = load_passphrases_all(
            kmip_nodes, cli_image=kmip_cli_image
        )
        subsystems = _existing_subsystems(gateway, config)
        _assign_kmip_endpoints(
            subsystems,
            kmip_nodes,
            int(config.get("subsystems_per_kmip", 2)),
        )
        ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
        for sub in subsystems:
            sub["namespaces"] = [
                _parent_ns_meta(sub, idx, ns_per_sub, passphrases_by_node)
                for idx in range(1, ns_per_sub + 1)
            ]

        if resume_io:
            if config.get("cleanup_orphan_images"):
                LOG.warning(
                    "cleanup_orphan_images ignored with resume_from=io "
                    "(existing orphan clones must stay)"
                )
            LOG.info(
                "resume_from=io: reuse existing orphan clone namespaces; "
                "skip RBD create, ns add, and masking"
            )
            targets = _discover_orphan_clone_targets(
                gateway, subsystems, passphrases_by_node, config
            )
        elif clone_only_ns:
            if config.get("cleanup_orphan_images"):
                _cleanup_orphan_images(gateway, rbd_obj, subsystems, config)
            targets = _pick_orphan_clone_targets(
                subsystems, passphrases_by_node, config
            )
        else:
            targets = _pick_clone_targets(
                gateway, subsystems, passphrases_by_node, config, rbd_obj
            )

        if not resume_io:
            existing_rbd = _rbd_image_names(
                rbd_obj, config.get("rbd_pool", "rbd")
            )
            _write_passphrase_file(
                rbd_obj, CLONE_PASSPHRASE_FILE, CLONE_PASSPHRASE_VALUE
            )

        before = _snapshot_gateways(gateways)
        _assert_gateways_ready(gateways, "before clones")
        LOG.info(
            "Gateway PIDs before clones: %s",
            {item["hostname"]: item["main_pid"] for item in before.values()},
        )

        if resume_io:
            LOG.info("resume_from=io: %s existing orphan clones", len(targets))
        elif clone_only_ns:
            _add_orphan_clones_batched(
                gateway,
                rbd_obj,
                targets,
                config,
                existing_rbd,
                gateways,
                before,
                started,
                save_gw_logs,
                log_prefix,
                results,
            )
        else:
            for index, ctx in enumerate(targets, start=1):
                LOG.info(
                    "========== clone %s/%s %s ==========",
                    index,
                    len(targets),
                    ctx["clone_image"],
                )
                clone_t0 = time.time()
                result = _clone_result_entry(index, ctx)
                results.append(result)
                try:
                    _add_one_clone(gateway, rbd_obj, ctx, config, existing_rbd)
                    result["status"] = "ns_add_ok"
                    result["nsid"] = ctx.get("nsid")
                except Exception as exc:
                    result["status"] = "ns_add_failed"
                    result["error"] = str(exc)
                    if save_gw_logs:
                        result["logs"] = _save_clone_gateway_logs(
                            gateways,
                            index,
                            ctx,
                            time.time() - clone_t0 + 5,
                            prefix=log_prefix,
                        )
                    raise
                time.sleep(GW_SETTLE_SEC)
                if save_gw_logs:
                    result["logs"] = _save_clone_gateway_logs(
                        gateways,
                        index,
                        ctx,
                        time.time() - clone_t0 + 5,
                        prefix=log_prefix,
                    )
                try:
                    _check_gateways(
                        gateways,
                        before,
                        f"after ns add {ctx['clone_image']}",
                        since_seconds=time.time() - started,
                    )
                    result["status"] = "ok"
                except Exception as exc:
                    result["status"] = "gateway_check_failed"
                    result["error"] = str(exc)
                    if save_gw_logs:
                        result["logs"] = _save_clone_gateway_logs(
                            gateways,
                            index,
                            ctx,
                            time.time() - clone_t0 + 5,
                            prefix=log_prefix,
                        )
                    raise

        if config.get("skip_io"):
            LOG.info(
                "skip_io: %s clones added; skipping FIO (clone-add repro only)",
                len(targets),
            )
            LOG.info(
                "BYOK clone IO passed: %s clones, gateways stayed up",
                len(targets),
            )
            return 0

        if clone_only_ns:
            _run_clone_only_client_io(
                gateway,
                gateways,
                subsystems,
                targets,
                clients,
                config,
                before,
                started,
                resume_io,
            )
            if not config.get("skip_gw_restart"):
                _restart_gateways_and_verify_clones(nvme_service, targets, config)
                _assert_gateways_ready(nvme_service.gateways, "after GW restart")
        else:
            initiator = NVMeInitiator(client)
            initiator.disconnect_all()
            host_nqn = initiator.initiator_nqn()
            nqns = _mask_clones(gateway, targets, host_nqn)
            port = config.get("listener_port", DEFAULT_LISTENER_PORT)
            _connect_clone_subsystems(initiator, gateways, nqns, port)
            time.sleep(5)
            uuids = [_norm_uuid(ctx["uuid"]) for ctx in targets]
            paths = _wait_for_clone_paths(initiator, uuids)
            _run_clone_io(client, paths, config, gateways, before)
            _check_gateways(
                gateways,
                before,
                "after clone IO",
                since_seconds=time.time() - started,
            )
        LOG.info(
            "BYOK clone IO passed: %s clones, gateways stayed up",
            len(targets),
        )
        return 0
    except Exception as err:
        LOG.exception("NVMeoF BYOK clone IO test failed: %s", err)
        after = None
        if gateways:
            try:
                after = {
                    gw.node.id: _gw_unit_identity(gw, require_running=False)
                    for gw in gateways
                }
            except Exception as snap_exc:
                LOG.warning("Could not snapshot gateways for bug summary: %s", snap_exc)
        try:
            summary = _write_bug_summary(
                results, before, err, config, started, extra_after=after
            )
            if summary:
                LOG.error("Bug summary written to %s", summary)
        except Exception as summary_exc:
            LOG.warning("Failed to write clone IO bug summary: %s", summary_exc)
        return 1
    finally:
        try:
            for node in ceph_cluster.get_nodes(role="client"):
                NVMeInitiator(node).disconnect_all()
        except Exception as exc:
            LOG.warning("Initiator disconnect during cleanup: %s", exc)
