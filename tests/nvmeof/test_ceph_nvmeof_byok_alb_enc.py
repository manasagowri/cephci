"""SC-11 CROSS ENC ALB: encrypted namespaces plus auto load balancing.

Reuses group2 ``cnode*`` encrypted parents (512). Enables ALB, runs light
FIO, adds 128 LUKS2 ``albenc_*`` NS with a dedicated KMIP passphrase, then
for each gateway: stops KMIP, restarts that GW, and checks the load-balancing
rule for encrypted namespaces.

``encrypted_lb_policy`` selects the oracle (default ``short_term``):

- short term: do not move an encrypted namespace unless it is degraded on
  the gateway that currently owns it, its ANA group belongs to a gateway
  that is being deleted, or its location does not match its ANA group.
  This scenario has no gateway deletion and no location mismatch, so a
  namespace that was healthy on its current owner must keep that group.
  A namespace whose owner is the restarted gateway is degraded there, so
  a move off that gateway is allowed.
- long term: equalization moves are allowed, but a namespace must not be
  moved onto a gateway where that same namespace is degraded.

Groups 64 and 255 are transient and are not a passing result. Peers must
show ``degraded=false``, and both FIO jobs must stay ``err=0``.
"""

import json
import threading
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.ceph_admin.orch import Orch
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    _assign_kmip_endpoints,
    _existing_subsystems,
    _init_rbd,
    _keys_for_node,
    _ns_add,
    _rbd_image_names,
    _sorted_kmip_nodes,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _copy_kmip_certs_when_containers_ready,
    _gw_unit_identity,
    _refresh_gateway_ssh,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import (
    _apply_ns_mask,
    _norm_uuid,
    _ns_record,
)
from tests.nvmeof.test_ceph_nvmeof_byok_ha_enc import (
    _parent_has_encryption,
    _parent_ns_records_retry,
)
from tests.nvmeof.test_ceph_nvmeof_byok_kmip_gw_restart import (
    DEFAULT_FIO_MAX_RUNTIME,
    DEFAULT_NS_REOPEN_TIMEOUT,
    DEFAULT_PID_CHANGE_DELAY,
    DEFAULT_PID_CHANGE_TIMEOUT,
    FIO_ERR_RE,
    FIO_STOP_EXIT_CODES,
    _connect_and_map,
    _fio_opts,
    _stop_fio,
    _wait_for_paths,
    _wait_pid_changed,
    _write_light_fio_job,
)
from tests.nvmeof.workflows.byok_kmip import (
    DEFAULT_KMIP_CLI_IMAGE,
    _create_one_passphrase,
    _kmip_objects,
    _wait_for_container,
    destroy_passphrase,
    short_hostname,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.load_balancing import validate_auto_loadbalance
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

PARENT_JOB = "/tmp/kmip_alb_enc_parents.fio"
PARENT_FIO_LOG = "/tmp/kmip_alb_enc_parents.fio.log"
ALBENC_JOB = "/tmp/kmip_alb_enc_albenc.fio"
ALBENC_FIO_LOG = "/tmp/kmip_alb_enc_albenc.fio.log"
ALBENC_PREFIX = "albenc_"
ALBENC_PASSPHRASE_NAME = "albenc-cross"
ALBENC_PASSPHRASE_VALUE = "albenc-passwd"
DEFAULT_ALB_NEW_NS = 128
DEFAULT_REBALANCE_PERIOD = 60
DEFAULT_ALB_WAIT_PERIODS = 2
# 64 is the maintenance group used while a move is in progress. 255 is the
# older in-progress id some gateway builds still report.
TRANSIENT_LB_GROUPS = (64, 255)


def _image_name(sub_num, ns_index):
    return f"albenc_c{sub_num:02d}_n{ns_index:02d}"


def _is_albenc_image(name):
    return bool(name) and str(name).startswith(ALBENC_PREFIX)


def _alb_wait_sec(config):
    period = int(config.get("rebalance_period_sec", DEFAULT_REBALANCE_PERIOD))
    windows = int(config.get("alb_wait_periods", DEFAULT_ALB_WAIT_PERIODS))
    return max(period, 1) * max(windows, 1)


def _wait_alb_window(config, label):
    seconds = _alb_wait_sec(config)
    LOG.info("Waiting %ss for ALB cycle (%s)", seconds, label)
    time.sleep(seconds)


def _kmip_nodes_for_subsystems(subsystems):
    nodes = []
    seen = set()
    for sub in subsystems:
        node = sub.get("kmip_node")
        if node is None:
            continue
        host = short_hostname(node)
        if host in seen:
            continue
        seen.add(host)
        nodes.append(node)
    if not nodes:
        raise RuntimeError("No KMIP nodes assigned to cnode* subsystems")
    return nodes


def _subsystem_max_ns(info):
    for key in ("max_namespaces", "max-namespaces"):
        if info.get(key) is None:
            continue
        try:
            return int(info[key])
        except (TypeError, ValueError):
            continue
    return 0


def _ensure_subsystem_max_ns(gateway, subsystems, extra_per_sub):
    """Raise per-subsystem max-namespaces so albenc NS can be added."""
    extra_per_sub = int(extra_per_sub)
    out, _ = gateway.subsystem.list(**{"base_cmd_args": {"format": "json"}})
    listed = {}
    for item in json.loads(out).get("subsystems", []) if out else []:
        nqn = item.get("nqn")
        if nqn:
            listed[nqn] = item

    def _check_and_raise(sub):
        nqn = sub["group_nqn"]
        info = listed.get(nqn) or listed.get(sub.get("nqn")) or {}
        current_max = _subsystem_max_ns(info)
        count = len(_list_namespaces(gateway, nqn))
        needed = count + extra_per_sub
        LOG.info(
            "%s: namespace_count=%s max_namespaces=%s need +%s",
            nqn,
            count,
            current_max or "unknown",
            extra_per_sub,
        )
        if current_max >= needed:
            return
        LOG.info(
            "%s: raising max_namespaces %s -> %s",
            nqn,
            current_max,
            needed,
        )
        gateway.subsystem.change(
            **{"args": {"subsystem": nqn, "max-namespaces": needed}}
        )

    with parallel(max_workers=4) as p:
        for sub in subsystems:
            p.spawn(_check_and_raise, sub)


def _list_namespaces(gateway, nqn):
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    if not out or not str(out).strip():
        return []
    try:
        return json.loads(out).get("namespaces", []) or []
    except (json.JSONDecodeError, TypeError) as exc:
        raise CommandFailed(f"invalid ns list JSON for {nqn}: {exc}") from exc


def _lb_group(ns):
    raw = ns.get("load_balancing_group")
    if raw is None:
        raw = ns.get("anagrpid")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_degraded_flag(ns):
    return ns.get("degraded") in (True, "true", "True", 1, "yes")


def _encrypted_lb_policy(config):
    """Return ``short_term`` or ``long_term``.

    Short term refuses equalization moves of encrypted namespaces. Long term
    allows those moves when the destination is not a gateway on which that
    namespace is degraded.
    """
    policy = str(config.get("encrypted_lb_policy", "short_term")).strip().lower()
    if policy not in ("short_term", "long_term"):
        raise ValueError(
            "encrypted_lb_policy must be short_term or long_term, "
            f"got {policy!r}"
        )
    return policy


def _lb_baseline(gateway, records):
    """Map each record UUID to its settled load-balancing group.

    Raises when a namespace is missing or still on a transient group, so the
    caller does not treat an in-progress move as the owner.
    """
    listed = _listed_by_nqn(gateway, records)
    baseline = {}
    unsettled = []
    for record in records:
        ns = listed.get(record["nqn"], {}).get(record["image"])
        if not ns:
            unsettled.append(f"{record['image']} missing")
            continue
        group = _lb_group(ns)
        if group is None or group in TRANSIENT_LB_GROUPS:
            unsettled.append(f"{record['image']} ana={group}")
            continue
        baseline[_norm_uuid(record.get("uuid"))] = group
    if unsettled:
        raise RuntimeError(
            "LB baseline is not settled:\n" + "\n".join(unsettled[:30])
        )
    return baseline


def _wait_lb_baseline(gateway, records, timeout=120, delay=5):
    """Poll until every namespace has left the transient groups."""
    started = time.time()
    last = None
    while True:
        try:
            return _lb_baseline(gateway, records)
        except RuntimeError as exc:
            last = exc
            if time.time() - started >= timeout:
                raise RuntimeError(
                    f"LB baseline not settled after {int(timeout)}s: {last}"
                ) from last
            time.sleep(delay)


def _assert_groups_unchanged(gateway, records, baseline, label):
    """Fail when an encrypted namespace changed groups with no exception.

    Used where no namespace is degraded on its owner, no gateway is being
    deleted, and locations already match ANA groups. The short-term rule
    then requires every group to stay put.
    """
    listed = _listed_by_nqn(gateway, records)
    errors = []
    for record in records:
        uuid = _norm_uuid(record.get("uuid"))
        ns = listed.get(record["nqn"], {}).get(record["image"])
        if not ns:
            errors.append(f"{record['image']} missing ({label})")
            continue
        group = _lb_group(ns)
        previous = baseline.get(uuid)
        if group in TRANSIENT_LB_GROUPS or group is None:
            errors.append(f"{record['image']} still transient ana={group}")
            continue
        if group != previous:
            errors.append(f"{record['image']} lb {previous} -> {group}")
    if errors:
        raise RuntimeError(
            f"{label}: encrypted namespaces moved:\n" + "\n".join(errors[:30])
        )


def _enc_tuple(ns):
    entries = ns.get("encryption_entries") or []
    return tuple(
        (str(item.get("format") or "").strip(), str(item.get("key_id") or "").strip())
        for item in entries
    )


def _parent_snapshots(gateway, records):
    by_nqn = {}
    for record in records:
        by_nqn.setdefault(record["nqn"], []).append(record)
    nqns = list(by_nqn)
    with parallel(max_workers=4) as p:
        for nqn in nqns:
            p.spawn(_list_namespaces, gateway, nqn)
        fetched = {
            nqn: {ns.get("rbd_image_name"): ns for ns in ns_list}
            for nqn, ns_list in zip(nqns, p)
        }
    snaps = {}
    for nqn, batch in by_nqn.items():
        listed = fetched[nqn]
        for record in batch:
            ns = listed.get(record["image"])
            if not ns:
                raise RuntimeError(f"{nqn} {record['image']}: missing from ns list")
            if not _parent_has_encryption(ns):
                raise RuntimeError(
                    f"{nqn} {record['image']}: missing encryption_entries "
                    f"({ns.get('encryption_entries')})"
                )
            snaps[record["image"]] = _enc_tuple(ns)
    return snaps


def _assert_parent_encryption(gateway, records, snaps, label):
    current = _parent_snapshots(gateway, records)
    mismatches = []
    for image, expected in snaps.items():
        got = current.get(image)
        if got != expected:
            mismatches.append(f"{image}: {expected} -> {got}")
    if mismatches:
        raise RuntimeError(
            f"{label}: parent encryption attrs changed:\n" + "\n".join(mismatches[:20])
        )


def _assert_fio_job_running(clients, job_path, label):
    dead = []
    for client in clients:
        out, _ = client.exec_command(
            cmd="pgrep -ax fio || true", sudo=True, check_ec=False
        )
        text = (out or "").strip()
        if job_path in text:
            LOG.info(
                "%s: FIO %s still running %s: %s",
                client.hostname,
                job_path,
                label,
                text,
            )
            continue
        dead.append(f"{client.hostname} ({text or 'no fio'})")
    if dead:
        raise RuntimeError(f"{label}: FIO {job_path} is not running on {dead}")


def _fio_io_errors(clients, log_path):
    errors = []
    for client in clients:
        out, _ = client.exec_command(
            cmd=f"grep -E 'err=' {log_path} || true",
            sudo=True,
            check_ec=False,
        )
        text = (out or "").strip()
        if not text:
            LOG.warning("%s: no err= lines in %s", client.hostname, log_path)
            continue
        for line in text.splitlines():
            match = FIO_ERR_RE.search(line)
            if match and int(match.group(1)) != 0:
                errors.append(f"{client.hostname}: {line.strip()}")
        LOG.info("%s %s err= lines:\n%s", client.hostname, log_path, text)
    return errors


def _assert_fio_healthy(clients, job_path, log_path, label):
    _assert_fio_job_running(clients, job_path, label)
    errors = _fio_io_errors(clients, log_path)
    if errors:
        raise RuntimeError(f"{label}: FIO errors in {log_path}:\n" + "\n".join(errors))


def _run_fio_job(node, job_path, log_path):
    LOG.info("Starting FIO %s on %s", job_path, node.hostname)
    return node.exec_command(
        cmd=f"fio --output={log_path} --status-interval=30 {job_path}",
        sudo=True,
        long_running=True,
        timeout="notimeout",
    )


def _spawn_fio(clients, job_path, log_path):
    threads = []
    for client in clients:
        thread = threading.Thread(
            target=_run_fio_job,
            args=(client, job_path, log_path),
            daemon=True,
            name=f"fio-{client.hostname}-{job_path}",
        )
        thread.start()
        threads.append(thread)
    return threads


def _cleanup_albenc(gateways, rbd_obj, subsystems, config):
    """Remove leftover albenc namespaces and RBD images from a prior run.

    Scans all gateways in the group and takes the union of albenc NS found,
    because a gateway whose encryption key was destroyed will return an empty
    namespace list for those encrypted entries even though they still exist
    in the gateway-group configuration.

    Degraded albenc namespaces from a previous run have ``rbd_image_name`` set
    to an empty string.  We compute the expected image names from config so
    we can also delete them by nsid when the image name is absent.
    """
    total = int(config.get("alb_new_namespaces", DEFAULT_ALB_NEW_NS))
    per_sub = total // max(len(subsystems), 1)
    pool = config.get("rbd_pool", "rbd")
    removed = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        expected = frozenset(
            _image_name(sub["num"], i) for i in range(1, per_sub + 1)
        )
        # Collect albenc NS across all gateways, deduplicating by nsid.
        # Store the gateway that found each NS so we issue the delete via a
        # gateway that is known to see it (avoids "not found" from a gateway
        # whose encryption key was destroyed in a prior run).
        seen: dict = {}  # nsid -> (ns_dict, gateway)
        # Track nsids of empty-name degraded entries as fallback.
        degraded_seen: dict = {}  # nsid -> (ns_dict, gateway)
        for gw in gateways:
            for ns in _list_namespaces(gw, nqn):
                name = ns.get("rbd_image_name") or ""
                nsid = ns.get("nsid")
                if name in expected:
                    seen.setdefault(nsid, (ns, gw))
                elif not name and ns.get("uuid"):
                    degraded_seen.setdefault(nsid, (ns, gw))
        # If we found fewer named entries than expected, include degraded
        # (empty-name) extras up to the expected count.
        if len(seen) < per_sub:
            for nsid, entry in sorted(
                degraded_seen.items(), key=lambda kv: int(kv[0])
            ):
                if nsid not in seen:
                    seen[nsid] = entry
                if len(seen) >= per_sub:
                    break
        for nsid, (ns, del_gw) in seen.items():
            name = ns.get("rbd_image_name") or ""
            LOG.info("ns del leftover %s nsid=%s on %s", name or "(degraded)", nsid, nqn)
            try:
                del_gw.namespace.delete(
                    **{
                        "args": {
                            "subsystem": nqn,
                            "nsid": nsid,
                            "force": True,
                        }
                    }
                )
                removed += 1
            except CommandFailed as exc:
                LOG.warning("ns del %s on %s: %s", name, nqn, exc)
    images = sorted(
        name
        for name in _rbd_image_names(rbd_obj, pool)
        if name.startswith(ALBENC_PREFIX)
    )
    for image in images:
        LOG.info("rbd rm %s/%s", pool, image)
        rbd_obj.exec_cmd(cmd=f"rbd rm {pool}/{image}", check_ec=False)
    LOG.info("Removed %s leftover albenc NS and %s images", removed, len(images))


def _passphrase_on_node(node, spec, cli_image):
    try:
        objects = _kmip_objects(node, cli_image=cli_image)
    except Exception as exc:
        LOG.warning(
            "[%s] kmip list for albenc passphrase failed: %s",
            short_hostname(node),
            exc,
        )
        objects = []
    for obj in objects:
        if obj.get("name") == spec["name"]:
            return {**spec, "uuid": str(obj["uuid"])}
    uuid = _create_one_passphrase(node, spec, cli_image=cli_image)
    return {**spec, "uuid": str(uuid)}


def _create_albenc_keys(kmip_nodes, config):
    spec = {
        "name": config.get("albenc_passphrase_name", ALBENC_PASSPHRASE_NAME),
        "value": config.get("albenc_passphrase_value", ALBENC_PASSPHRASE_VALUE),
    }
    cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)

    def _create_one(node):
        key = _passphrase_on_node(node, spec, cli_image)
        LOG.info(
            "[%s] albenc passphrase name=%s uuid=%s",
            short_hostname(node),
            spec["name"],
            key["uuid"],
        )
        return node, key

    keys = {}
    errors = []
    with parallel() as p:
        for node in kmip_nodes:
            p.spawn(_create_one, node)
        for result in p:
            if isinstance(result, Exception):
                errors.append(str(result))
            else:
                node, key = result
                keys[node] = key
    if errors:
        raise RuntimeError(
            f"Failed to create albenc passphrase on {len(errors)} KMIP node(s):\n"
            + "\n".join(errors)
        )
    return spec, keys


def _destroy_albenc_keys(keys, config):
    cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)
    with parallel() as p:
        for node, item in keys.items():
            p.spawn(destroy_passphrase, node, item["uuid"], cli_image)


def _restore_albenc_keys(kmip_nodes, spec, old_keys, config):
    cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)

    def _restore_one(node):
        created = _passphrase_on_node(node, spec, cli_image)
        return node, created

    new_keys = {}
    with parallel() as p:
        for node in kmip_nodes:
            p.spawn(_restore_one, node)
        for node, created in p:
            new_keys[node] = created

    changed = False
    for node, created in new_keys.items():
        old = old_keys.get(node) or {}
        if str(old.get("uuid")) != str(created["uuid"]):
            changed = True
            LOG.info(
                "[%s] albenc uuid changed %s -> %s",
                short_hostname(node),
                old.get("uuid"),
                created["uuid"],
            )
    return new_keys, changed


def _stop_kmip_servers(kmip_nodes):
    """Stop kmip-server and kmip-ha-standby containers on all KMIP nodes in parallel."""

    def _stop_one(node):
        for name in ("kmip-server", "kmip-ha-standby"):
            try:
                node.exec_command(
                    cmd=f"podman stop --time 5 {name}",
                    sudo=True,
                    check_ec=False,
                )
            except Exception as exc:
                LOG.warning("[%s] stop %s: %s", short_hostname(node), name, exc)
        LOG.info("[%s] KMIP containers stopped", short_hostname(node))

    with parallel() as p:
        for node in kmip_nodes:
            p.spawn(_stop_one, node)
        for _ in p:
            pass


def _start_kmip_servers(kmip_nodes, wait_tries=12, wait_delay=5):
    """Start kmip-server and kmip-ha-standby on all KMIP nodes and wait until up.

    A node that is unreachable or whose container fails to start is logged as a
    warning and skipped — the gateway can still recover via the HA standby on
    another node, so a single dead KMIP node should not abort the degrade cycle.
    """

    def _start_one(node):
        for name in ("kmip-server", "kmip-ha-standby"):
            try:
                node.exec_command(
                    cmd=f"podman start {name}",
                    sudo=True,
                    check_ec=False,
                )
            except Exception as exc:
                LOG.warning("[%s] start %s: %s", short_hostname(node), name, exc)
        # Wait until kmip-server is responding before returning so that
        # the gateway restart that follows can actually fetch keys.
        try:
            _wait_for_container(node, "kmip-server", tries=wait_tries, delay=wait_delay)
            LOG.info("[%s] KMIP containers started and ready", short_hostname(node))
        except Exception as exc:
            LOG.warning(
                "[%s] kmip-server did not come up after start: %s — continuing",
                short_hostname(node),
                exc,
            )

    with parallel() as p:
        for node in kmip_nodes:
            p.spawn(_start_one, node)
        for _ in p:
            pass


def _albenc_ns_count(gateways, subsystems, config):
    """Return how many expected albenc namespaces already exist on the cluster.

    Scans the union of all gateways (same logic as _cleanup_albenc) to count
    namespaces that match the expected ``albenc_*`` image names *or* are
    degraded empty-name entries that look like surviving albenc NS from a prior
    run.  Returns the total found so the caller can decide whether to skip
    creation.
    """
    total = int(config.get("alb_new_namespaces", DEFAULT_ALB_NEW_NS))
    per_sub = total // max(len(subsystems), 1)
    found = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        expected = frozenset(
            _image_name(sub["num"], i) for i in range(1, per_sub + 1)
        )
        seen: set = set()
        degraded_seen: set = set()
        for gw in gateways:
            for ns in _list_namespaces(gw, nqn):
                name = ns.get("rbd_image_name") or ""
                nsid = ns.get("nsid")
                if name in expected:
                    seen.add(nsid)
                elif not name and ns.get("uuid"):
                    degraded_seen.add(nsid)
        if len(seen) < per_sub:
            for nsid in sorted(degraded_seen, key=lambda x: int(x)):
                if nsid not in seen:
                    seen.add(nsid)
                if len(seen) >= per_sub:
                    break
        found += len(seen)
    return found


def _add_albenc_namespaces(gateway, subsystems, config, keys):
    pool = config.get("rbd_pool", "rbd")
    size = config.get("image_size", "5G")
    total = int(config.get("alb_new_namespaces", DEFAULT_ALB_NEW_NS))
    per_sub = total // max(len(subsystems), 1)
    if per_sub < 1:
        raise RuntimeError(
            "alb_new_namespaces must cover at least one NS per subsystem"
        )
    created = []

    def _add_one(sub, ns_index):
        image = _image_name(sub["num"], ns_index)
        nqn = sub["group_nqn"]
        key = _keys_for_node(keys, sub["kmip_node"])
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
        return {"image": image, "nqn": nqn, "ns_index": ns_index, "sub": sub}

    with parallel(max_workers=4) as p:
        for sub in subsystems:
            for ns_index in range(1, per_sub + 1):
                p.spawn(_add_one, sub, ns_index)
        for item in p:
            created.append(item)
    LOG.info("Added %s albenc namespaces", len(created))
    return created


def _albenc_records(gateways, subsystems, nqn_owners, owner_nqns, config):
    """Return per-namespace records for every albenc NS across all subsystems.

    Uses the union of namespace lists from all gateways so that the result is
    correct even when one gateway (e.g. after its encryption key was destroyed
    in a prior run) omits the encrypted entries from its ns list.

    On a re-run the albenc namespaces may be in degraded state and their
    ``rbd_image_name`` will be an empty string.  We therefore match by the
    known expected image names first, and fall back to accepting empty-name
    entries (by UUID) when a subsystem still has fewer matches than expected.
    """
    total = int(config.get("alb_new_namespaces", DEFAULT_ALB_NEW_NS))
    per_sub = total // max(len(subsystems), 1)

    def _expected_images(sub):
        return frozenset(_image_name(sub["num"], i) for i in range(1, per_sub + 1))

    def _fetch_sub(sub):
        nqn = sub["group_nqn"]
        owners = nqn_owners.get(nqn) or []
        if not owners:
            raise RuntimeError(f"No client owner for {nqn}")
        expected = _expected_images(sub)
        # Pass 1: collect by image name (works when the NS is healthy).
        seen: dict = {}  # nsid -> ns
        # Collect empty-name entries per gateway as fallback candidates.
        degraded_candidates: dict = {}  # nsid -> ns (empty rbd_image_name + uuid)
        for gw in gateways:
            for ns in _list_namespaces(gw, nqn):
                img = ns.get("rbd_image_name") or ""
                if img in expected:
                    seen[ns["nsid"]] = ns
                elif not img and ns.get("uuid"):
                    # May be an albenc NS that lost its image name after key
                    # destruction.  Keep as fallback; prefer the entry already
                    # present (any gateway is equally representative here).
                    degraded_candidates.setdefault(ns["nsid"], ns)
        # Pass 2: if we are still short (re-run with degraded albenc NS), fill
        # in from the degraded candidates up to the expected count.
        if len(seen) < per_sub:
            for nsid, ns in sorted(
                degraded_candidates.items(), key=lambda kv: int(kv[0])
            ):
                if nsid not in seen:
                    seen[nsid] = ns
                if len(seen) >= per_sub:
                    break
        namespaces = sorted(seen.values(), key=lambda ns: int(ns["nsid"]))
        sub_records = []
        for index, ns in enumerate(namespaces):
            owner = owners[index % len(owners)]
            record = _ns_record(nqn, ns, owner=owner, owner_nqn=owner_nqns[owner])
            record["kmip_node"] = sub.get("kmip_node")
            sub_records.append(record)
        return sub_records

    records = []
    with parallel(max_workers=4) as p:
        for sub in subsystems:
            p.spawn(_fetch_sub, sub)
        for sub_records in p:
            records.extend(sub_records)
    return records


def _map_albenc(mapped, records):
    paths_by_host = {}
    by_owner = {}
    for record in records:
        by_owner.setdefault(record["owner"], []).append(record)
    for hostname, (client, initiator, _) in mapped.items():
        owned = by_owner.get(hostname, [])
        if not owned:
            raise RuntimeError(f"No albenc namespaces assigned to {hostname}")
        uuids = [_norm_uuid(ns["uuid"]) for ns in owned]
        paths = _wait_for_paths(initiator, uuids, hostname)
        if len(paths) < len(uuids):
            raise RuntimeError(
                f"{hostname}: expected {len(uuids)} albenc devices, found {len(paths)}"
            )
        paths_by_host[hostname] = paths
        LOG.info("%s can see %s albenc namespaces", hostname, len(paths))
    return paths_by_host


def _unbalance_parents(gateways, records, ana_group, count=32):
    ana_group = int(ana_group)
    listed = _listed_by_nqn(gateways[0], records)
    gateways_by_group = {int(gw.ana_group_id): gw for gw in gateways}
    candidates = []
    for record in records:
        ns = listed.get(record["nqn"], {}).get(record["image"]) or {}
        owner_group = _lb_group(ns)
        if owner_group != ana_group and owner_group in gateways_by_group:
            candidates.append((record, gateways_by_group[owner_group]))
        if len(candidates) == count:
            break

    def _move_one(record, owner_gateway):
        try:
            owner_gateway.namespace.change_load_balancing_group(
                **{
                    "args": {
                        "subsystem": record["nqn"],
                        "nsid": record["nsid"],
                        "load-balancing-group": ana_group,
                    }
                }
            )
            return 1
        except CommandFailed as exc:
            LOG.warning(
                "change_load_balancing_group %s nsid=%s via %s: %s",
                record["nqn"],
                record["nsid"],
                owner_gateway.node.hostname,
                exc,
            )
            return 0

    moved = 0
    with parallel(max_workers=4) as p:
        for record, owner_gateway in candidates:
            p.spawn(_move_one, record, owner_gateway)
        for result in p:
            moved += result
    LOG.info("Moved %s parent NS onto ANA group %s", moved, ana_group)
    return moved


def _ns_by_image(gateway, nqn):
    return {
        ns.get("rbd_image_name"): ns
        for ns in _list_namespaces(gateway, nqn)
        if ns.get("rbd_image_name")
    }


def _ns_by_uuid(gateway, nqn):
    """Return {norm_uuid: ns} for every namespace in *nqn* on *gateway*.

    This is the correct lookup key for a degraded namespace whose
    ``rbd_image_name`` and ``rbd_pool_name`` are returned as empty strings by
    the gateway that restarted with a missing encryption key.
    """
    return {
        _norm_uuid(ns.get("uuid")): ns
        for ns in _list_namespaces(gateway, nqn)
        if ns.get("uuid")
    }


def _listed_by_nqn(gateway, records):
    nqns = sorted({record["nqn"] for record in records})
    results = {}
    total = len(nqns)
    for index, nqn in enumerate(nqns, 1):
        results[nqn] = _ns_by_image(gateway, nqn)
        LOG.info(
            "%s: namespace scan progress %s/%s",
            gateway.node.hostname,
            index,
            total,
        )
    return results


def _listed_by_uuid(gateway, records):
    """Like _listed_by_nqn but keyed by normalised UUID instead of image name.

    Use this for the restarted gateway after key destruction: degraded entries
    have empty rbd_image_name / rbd_pool_name but still expose their UUID.
    """
    nqns = sorted({record["nqn"] for record in records})
    results = {}
    total = len(nqns)
    for index, nqn in enumerate(nqns, 1):
        results[nqn] = _ns_by_uuid(gateway, nqn)
        LOG.info(
            "%s: UUID-keyed namespace scan progress %s/%s",
            gateway.node.hostname,
            index,
            total,
        )
    return results


def _assert_albenc_healthy(gateways, records, label):
    errors = []
    listed = {gw: _listed_by_nqn(gw, records) for gw in gateways}
    for record in records:
        for gw in gateways:
            ns = listed[gw].get(record["nqn"], {}).get(record["image"])
            if not ns:
                errors.append(
                    f"{gw.node.hostname}: {record['image']} missing ({label})"
                )
                continue
            group = _lb_group(ns)
            if _is_degraded_flag(ns):
                errors.append(
                    f"{gw.node.hostname}: {record['image']} degraded="
                    f"{ns.get('degraded')} ana={group} ({label})"
                )
            if not _parent_has_encryption(ns):
                errors.append(
                    f"{gw.node.hostname}: {record['image']} missing encryption "
                    f"({label})"
                )
    if errors:
        raise RuntimeError(
            f"{label}: albenc NS not healthy:\n" + "\n".join(errors[:30])
        )


def _assert_degraded_on_restarted(
    restarted, peers, records, baseline, policy="short_term"
):
    """Assert the per-GW degraded view and the encrypted load-balancing rule.

    On the restarted gateway every affected namespace is ``degraded=true``.
    Peers still have the bdev open and must show ``degraded=false``. Groups
    64 and 255 are still in progress, so they fail this check and the caller
    keeps polling.

    ``baseline`` maps namespace UUID to the load-balancing group from before
    the KMIP stop. ``policy`` is ``short_term`` or ``long_term``. This scenario
    does not delete a gateway and does not change namespace location, so the
    only short-term exception is a namespace that is degraded on the gateway
    that owned it (the restarted gateway).
    """
    errors = []
    host = restarted.node.hostname
    restarted_group = int(restarted.ana_group_id)
    # Use UUID-keyed map for the restarted gateway — image names are empty there.
    restarted_uuid_map = _listed_by_uuid(restarted, records)
    # Peers are healthy; image names are present — keep the normal lookup.
    peer_maps = {peer: _listed_by_nqn(peer, records) for peer in peers}
    for record in records:
        uuid = _norm_uuid(record.get("uuid"))
        ns = restarted_uuid_map.get(record["nqn"], {}).get(uuid)
        if not ns:
            errors.append(
                f"{host}: {record['image']} (uuid={uuid}) missing after restart"
            )
            continue
        group = _lb_group(ns)
        if not _is_degraded_flag(ns):
            errors.append(
                f"{host}: {record['image']} expected degraded=true "
                f"got degraded={ns.get('degraded')} ana={group}"
            )
        if group is None or group in TRANSIENT_LB_GROUPS:
            errors.append(
                f"{host}: {record['image']} ana={group} still transient; "
                "waiting for a settled load-balancing group"
            )
        else:
            previous = baseline.get(uuid)
            if policy == "short_term":
                # Healthy on its previous owner: that owner is not the restarted
                # gateway, so the group must not change. A move onto another
                # healthy peer is also a failure.
                if previous != restarted_group and group != previous:
                    errors.append(
                        f"{host}: {record['image']} was healthy on group "
                        f"{previous} and moved to {group}"
                    )
            elif previous != restarted_group and group == restarted_group:
                # Long term may rebalance, but not onto the gateway where this
                # namespace is degraded.
                errors.append(
                    f"{host}: {record['image']} moved from group {previous} "
                    f"onto restarted group {restarted_group}, where it is degraded"
                )
        for peer in peers:
            pns = peer_maps[peer].get(record["nqn"], {}).get(record["image"])
            if not pns:
                errors.append(
                    f"{peer.node.hostname}: {record['image']} missing on peer"
                )
                continue
            if _is_degraded_flag(pns):
                errors.append(
                    f"{peer.node.hostname}: {record['image']} should not be "
                    f"degraded (degraded={pns.get('degraded')} ana={_lb_group(pns)})"
                )
    if errors:
        raise RuntimeError("Per-GW degrade view failed:\n" + "\n".join(errors[:40]))
    LOG.info(
        "%s: %s policy held for %s albenc NS; restarted group %s; peers show degraded=false",
        host,
        policy,
        len(records),
        restarted_group,
    )


def _count_albenc_visible(gateway, records):
    """Return how many albenc records are currently visible (by UUID) on *gateway*.

    A namespace is "visible" if it appears in the ns list at all — whether
    degraded or healthy.  We count by UUID because degraded entries have an
    empty rbd_image_name.
    """
    nqns = sorted({r["nqn"] for r in records})
    seen = 0
    for nqn in nqns:
        uuid_map = _ns_by_uuid(gateway, nqn)
        for record in records:
            if record["nqn"] == nqn and _norm_uuid(record.get("uuid")) in uuid_map:
                seen += 1
    return seen


def _wait_all_ns_loaded(gateway, records, delay=30):
    """Block until every albenc NS UUID is visible on *gateway*.

    This replaces a fixed timeout: when the GW restarts with KMIP down it may
    take 15-30 minutes to load all ~1400 NS.  The albenc entries (last in OMAP)
    only appear once the GW has finished that initial load.  We simply keep
    polling — no upper time limit — logging progress every iteration.
    """
    host = gateway.node.hostname
    total = len(records)
    started = time.time()
    while True:
        visible = _count_albenc_visible(gateway, records)
        elapsed = int(time.time() - started)
        if visible == total:
            LOG.info(
                "%s: all %s albenc NS now visible after %ss",
                host,
                total,
                elapsed,
            )
            return
        LOG.info(
            "%s: waiting for NS load — %s/%s albenc NS visible at %ss",
            host,
            visible,
            total,
            elapsed,
        )
        time.sleep(delay)


def _wait_degraded_view(
    restarted, peers, records, timeout, baseline, policy="short_term", delay=10
):
    """Wait until the restarted GW shows all albenc NS as degraded.

    Phase 1 — wait for all NS to finish loading (no timeout: the GW may take
    15-30 min with KMIP down at this scale).  We treat "missing" entries as
    "still loading", not as a failure.

    Phase 2 — once all NS are visible, assert they are all degraded (and peers
    are healthy).  If this assertion fails after a full load we raise immediately
    since it won't self-correct.

    The *timeout* parameter is kept for API compatibility but only acts as a
    safety cap for Phase 2 (the assertion loop after all NS are loaded).
    """
    host = restarted.node.hostname
    # Phase 1: wait — without a timeout — until every albenc NS UUID is visible.
    _wait_all_ns_loaded(restarted, records, delay=delay)
    # Phase 2: now that everything is loaded, assert the degraded view.
    started = time.time()
    last = None
    while True:
        try:
            _assert_degraded_on_restarted(
                restarted, peers, records, baseline, policy
            )
            return
        except Exception as exc:
            last = exc
            elapsed = int(time.time() - started)
            if elapsed >= timeout:
                raise RuntimeError(
                    f"{host}: degrade view not ready after {elapsed}s "
                    f"(all NS loaded but wrong state): {last}"
                ) from last
            LOG.warning(
                "%s: waiting for degrade view at %ss: %s",
                host,
                elapsed,
                exc,
            )
            time.sleep(delay)


def _reload_one(gateway, record, keys, uuid_changed):
    args = {"subsystem": record["nqn"], "nsid": record["nsid"]}
    node = record.get("kmip_node")
    if uuid_changed:
        if node is None:
            LOG.warning(
                "ns reload %s nsid=%s: no kmip_node for key-id update",
                record["nqn"],
                record["nsid"],
            )
        else:
            args["key-id"] = _keys_for_node(keys, node)["uuid"]
    try:
        gateway.namespace.reload(**{"args": args})
    except CommandFailed as exc:
        LOG.warning(
            "ns reload %s nsid=%s: %s",
            record["nqn"],
            record["nsid"],
            exc,
        )


def _reload_albenc(gateway, records, keys, uuid_changed):
    with parallel(max_workers=4) as p:
        for record in records:
            p.spawn(_reload_one, gateway, record, keys, uuid_changed)


def _both_maps(gateway, records):
    """Return (image_map, uuid_map) for *gateway* in a single ns-list pass.

    Builds ``{nqn: {rbd_image_name: ns}}`` and ``{nqn: {norm_uuid: ns}}``
    simultaneously so the caller never needs two separate scans.
    """
    nqns = sorted({record["nqn"] for record in records})
    img_map: dict = {}
    uuid_map: dict = {}
    total = len(nqns)
    for index, nqn in enumerate(nqns, 1):
        by_img: dict = {}
        by_uuid: dict = {}
        for ns in _list_namespaces(gateway, nqn):
            name = ns.get("rbd_image_name")
            if name:
                by_img[name] = ns
            uid = _norm_uuid(ns.get("uuid"))
            if uid:
                by_uuid[uid] = ns
        img_map[nqn] = by_img
        uuid_map[nqn] = by_uuid
        LOG.info(
            "%s: namespace scan progress %s/%s",
            gateway.node.hostname,
            index,
            total,
        )
    return img_map, uuid_map


def _wait_albenc_recovered(restarted, records, timeout, delay):
    """Poll until all albenc NS on the restarted gateway are healthy again.

    Phase 1 — wait (no timeout) for all albenc NS to finish loading after the
    recovery restart.  With KMIP available the GW still takes several minutes
    to load ~1400 NS; the albenc entries appear last.

    Phase 2 — once all NS are visible, poll until every one is healthy
    (degraded=false, non-empty rbd_image_name, encryption present).  The
    *timeout* parameter acts as a cap only for this phase.

    While the gateway is still loading the entries appear with
    ``degraded=true``, empty ``rbd_image_name``, and empty ``rbd_pool_name``.
    We build both maps in a single ns-list pass each iteration: look up by
    image name first (succeeds once recovered), fall back to UUID (works while
    still degraded with empty image name).
    """
    host = restarted.node.hostname
    started = time.time()

    # Phase 1: wait — without a timeout — until every albenc NS UUID is visible.
    _wait_all_ns_loaded(restarted, records, delay=delay)

    # Phase 2: all NS are visible — now wait for them to become healthy.
    deadline = time.time() + timeout
    last = []
    while True:
        last = []
        listed, uuid_listed = _both_maps(restarted, records)
        for record in records:
            # Try image-name lookup first (succeeds once fully recovered).
            ns = listed.get(record["nqn"], {}).get(record["image"])
            if not ns:
                # Fall back to UUID: entry is present but still degraded.
                uuid = _norm_uuid(record.get("uuid"))
                ns = uuid_listed.get(record["nqn"], {}).get(uuid)
            if not ns:
                last.append(f"{record['image']}: missing")
                continue
            if _is_degraded_flag(ns):
                last.append(
                    f"{record['image']}: degraded={ns.get('degraded')} "
                    f"ana={_lb_group(ns)}"
                )
                continue
            if not _parent_has_encryption(ns):
                last.append(f"{record['image']}: missing encryption")
        elapsed = int(time.time() - started)
        if not last:
            LOG.info(
                "%s: all %s albenc NS recovered in %ss",
                host,
                len(records),
                elapsed,
            )
            return elapsed
        if time.time() >= deadline:
            raise RuntimeError(
                f"{host}: albenc NS still degraded after {elapsed}s:\n"
                + "\n".join(last[:20])
            )
        LOG.info(
            "%s: waiting for albenc recovery %s/%s still degraded at %ss",
            host,
            len(last),
            len(records),
            elapsed,
        )
        time.sleep(delay)


def _restart_one_gateway(nvme_service, gateway, config):
    try:
        _refresh_gateway_ssh(gateway)
    except Exception as exc:
        LOG.warning(
            "SSH refresh to %s before restart failed: %s",
            gateway.node.hostname,
            exc,
        )
    before = _gw_unit_identity(gateway)
    nvme_service.restart_daemon(
        gateway, wait_sec=int(config.get("gw_restart_delay", 5))
    )
    _wait_pid_changed(
        gateway,
        before,
        int(config.get("pid_change_timeout", DEFAULT_PID_CHANGE_TIMEOUT)),
        int(config.get("pid_change_delay", DEFAULT_PID_CHANGE_DELAY)),
    )
    _copy_kmip_certs_when_containers_ready([gateway.node])
    gateway.load_gateway_info(
        tries=int(config.get("gw_ready_tries", 24)),
        delay=int(config.get("gw_ready_delay", 10)),
    )


def _prepare_parent_jobs(mapped, config, runtime):
    opts = _fio_opts(config, runtime)
    for client, _, paths in mapped.values():
        if not paths:
            raise RuntimeError(f"No parent devices on {client.hostname}")
        _write_light_fio_job(client, PARENT_JOB, paths, opts)


def _prepare_albenc_jobs(mapped, paths_by_host, config, runtime):
    opts = _fio_opts(config, runtime)
    clients = []
    for hostname, (client, _, _) in mapped.items():
        paths = paths_by_host.get(hostname) or []
        if not paths:
            raise RuntimeError(f"No albenc devices on {hostname}")
        _write_light_fio_job(client, ALBENC_JOB, paths, opts)
        clients.append(client)
    return clients


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """ALB ENC. Returns 0 on success, 1 on failure."""
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)
    clients = []
    stop_state = {"stopped": False}
    cleanup_sessions = bool(config.get("cleanup_sessions", False))
    rbd_obj = None
    gateway = None
    gateways = []
    subsystems = []

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
            raise ValueError("ALB ENC requires a client node")
        if len(gateways) < 2:
            raise ValueError("ALB ENC requires at least two gateways")

        subsystems = _existing_subsystems(gateway, config)
        kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
        _assign_kmip_endpoints(
            subsystems,
            kmip_nodes,
            int(config.get("subsystems_per_kmip", 2)),
        )
        kmip_nodes = _kmip_nodes_for_subsystems(subsystems)
        if config.get("cleanup_prior", True):
            _cleanup_albenc(gateways, rbd_obj, subsystems, config)

        limits_changed = nvme_service.ensure_namespace_limits(
            max_namespaces=int(config.get("gw_max_namespaces", 4096)),
            max_namespaces_per_subsystem=int(
                config.get("max_namespaces_per_subsystem", 512)
            ),
            max_namespaces_with_netmask=int(
                config.get("max_namespaces_with_netmask", 4096)
            ),
        )
        redeployed = nvme_service.ensure_rebalance_period(
            int(config.get("rebalance_period_sec", DEFAULT_REBALANCE_PERIOD))
        )
        if limits_changed or redeployed:
            delay = int(config.get("gw_restart_delay", 30))
            LOG.info("Waiting for gateways after NVMeoF spec update")
            time.sleep(delay)
            nvme_service.wait_for_gateways(
                tries=int(config.get("gw_ready_tries", 36)),
                delay=10,
            )
            _copy_kmip_certs_when_containers_ready([gw.node for gw in gateways])
            for gw in gateways:
                gw.load_gateway_info()
            gateway = nvme_service.gateways[0]
            gateways = nvme_service.gateways

        extra_per_sub = int(
            config.get("alb_new_namespaces", DEFAULT_ALB_NEW_NS)
        ) // max(len(subsystems), 1)
        _ensure_subsystem_max_ns(gateway, subsystems, extra_per_sub)

        host_nqns = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            host_nqns[client.hostname] = initiator.initiator_nqn()
            LOG.info(
                "Client %s host NQN %s", client.hostname, host_nqns[client.hostname]
            )

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
        nqn_owners = {}
        owner_nqns = {}
        for record in records:
            owners = nqn_owners.setdefault(record["nqn"], [])
            if record["owner"] not in owners:
                owners.append(record["owner"])
            owner_nqns[record["owner"]] = record["owner_nqn"]

        mapped = _connect_and_map(gateways, subsystems, clients, assigned, config)
        ana_id = gateways[0].ana_group_id
        _unbalance_parents(gateways, records, ana_id)
        runtime = int(config.get("fio_max_runtime", DEFAULT_FIO_MAX_RUNTIME))
        _prepare_parent_jobs(mapped, config, runtime)
        orch = Orch(ceph_cluster, **{})

        errors = []
        with parallel(timeout=runtime + 600) as p:
            p.spawn(
                _alb_worker,
                nvme_service,
                gateway,
                gateways,
                subsystems,
                kmip_nodes,
                records,
                mapped,
                clients,
                nqn_owners,
                owner_nqns,
                orch,
                config,
                stop_state,
            )
            for client, _, _ in mapped.values():
                p.spawn(_run_fio_job, client, PARENT_JOB, PARENT_FIO_LOG)
            for result in p:
                if isinstance(result, Exception):
                    if stop_state["stopped"]:
                        LOG.info("FIO ended after stop: %s", result)
                        continue
                    for client in clients:
                        client.exec_command(
                            cmd="pkill -9 -x fio || true",
                            sudo=True,
                            check_ec=False,
                        )
                    raise result
                if isinstance(result, int) and result not in FIO_STOP_EXIT_CODES:
                    if stop_state["stopped"]:
                        continue
                    errors.append(result)
        if errors:
            raise RuntimeError(f"Parent FIO failed with {errors}")
        parent_errors = _fio_io_errors(clients, PARENT_FIO_LOG)
        albenc_errors = _fio_io_errors(clients, ALBENC_FIO_LOG)
        if parent_errors or albenc_errors:
            raise RuntimeError(
                "FIO reported IO errors:\n" + "\n".join(parent_errors + albenc_errors)
            )
        LOG.info(
            "ALB ENC passed: encryption preserved, %s load-balancing rule held, FIO err=0",
            _encrypted_lb_policy(config),
        )
        return 0
    except Exception as err:
        LOG.exception("ALB ENC test failed: %s", err)
        if config.get("cleanup_on_fail") and gateways and rbd_obj and subsystems:
            try:
                _cleanup_albenc(gateways, rbd_obj, subsystems, config)
            except Exception as exc:
                LOG.warning("cleanup_on_fail after ALB ENC: %s", exc)
        return 1
    finally:
        try:
            _stop_fio(clients)
        except Exception as exc:
            LOG.warning("FIO stop after ALB ENC: %s", exc)
        if cleanup_sessions:
            try:
                for node in ceph_cluster.get_nodes(role="client"):
                    NVMeInitiator(node).disconnect_all()
            except Exception as exc:
                LOG.warning("Cleanup after ALB ENC: %s", exc)


def _alb_worker(
    nvme_service,
    gateway,
    gateways,
    subsystems,
    kmip_nodes,
    records,
    mapped,
    clients,
    nqn_owners,
    owner_nqns,
    orch,
    config,
    stop_state,
):
    reopen = int(config.get("ns_reopen_timeout", DEFAULT_NS_REOPEN_TIMEOUT))
    policy = _encrypted_lb_policy(config)
    parent_baseline = _wait_lb_baseline(gateway, records)
    _wait_alb_window(config, "cycle 1 parents")
    if policy == "short_term":
        # Parents are healthy, no gateway is being deleted, and locations
        # match. Equalization must not move them.
        _assert_groups_unchanged(
            gateway, records, parent_baseline, "short-term parent hold"
        )
    else:
        validate_auto_loadbalance(
            orch, nvme_service.nvme_metadata_pool, nvme_service.group
        )
    _assert_fio_healthy(clients, PARENT_JOB, PARENT_FIO_LOG, "after parent ALB cycle")
    snaps = _parent_snapshots(gateway, records)
    LOG.info("Snapshot encryption attrs for %s parent NS", len(snaps))

    spec, keys = _create_albenc_keys(kmip_nodes, config)
    expected_new = int(config.get("alb_new_namespaces", DEFAULT_ALB_NEW_NS))
    existing = _albenc_ns_count(gateways, subsystems, config)
    if existing >= expected_new:
        LOG.info(
            "Found %s existing albenc namespaces (expected %s) — skipping creation",
            existing,
            expected_new,
        )
    else:
        if existing:
            LOG.info(
                "Found %s/%s albenc namespaces — creating remaining %s",
                existing,
                expected_new,
                expected_new - existing,
            )
        _add_albenc_namespaces(gateway, subsystems, config, keys)
    albenc = _albenc_records(gateways, subsystems, nqn_owners, owner_nqns, config)
    if len(albenc) != expected_new:
        raise RuntimeError(f"Expected {expected_new} albenc NS, found {len(albenc)}")
    for record in albenc:
        _apply_ns_mask(gateway, record)
    paths_by_host = _map_albenc(mapped, albenc)
    albenc_baseline = _wait_lb_baseline(gateway, albenc)
    _wait_alb_window(config, "albenc initial placement")
    if policy == "short_term":
        _assert_groups_unchanged(
            gateway, albenc, albenc_baseline, "short-term albenc hold"
        )
    _assert_albenc_healthy(gateways, albenc, "after albenc ALB spread")
    _prepare_albenc_jobs(
        mapped,
        paths_by_host,
        config,
        int(config.get("fio_max_runtime", DEFAULT_FIO_MAX_RUNTIME)),
    )
    _spawn_fio(clients, ALBENC_JOB, ALBENC_FIO_LOG)
    time.sleep(10)
    _assert_fio_healthy(clients, ALBENC_JOB, ALBENC_FIO_LOG, "albenc FIO started")
    _assert_fio_healthy(clients, PARENT_JOB, PARENT_FIO_LOG, "parents still running")

    recovery_timings = []
    for restarted in gateways:
        peers = [gw for gw in gateways if gw is not restarted]
        host = restarted.node.hostname
        LOG.info("ALB ENC degrade iteration on %s", host)

        # Stop all KMIP servers so the restarted gateway cannot fetch the
        # albenc passphrase and loads those namespaces degraded. Peer gateways
        # remain healthy because they already have the bdev open in memory.
        # Snapshot owners first. The short-term rule must leave every namespace
        # that is healthy on its current gateway on that same group.
        degrade_baseline = _wait_lb_baseline(gateway, albenc)
        _stop_kmip_servers(kmip_nodes)
        _restart_one_gateway(nvme_service, restarted, config)
        _wait_degraded_view(
            restarted,
            peers,
            albenc,
            int(config.get("degrade_timeout", 600)),
            degrade_baseline,
            policy,
        )
        _assert_parent_encryption(
            gateway, records, snaps, f"after restart {host}"
        )
        _assert_fio_healthy(
            clients,
            PARENT_JOB,
            PARENT_FIO_LOG,
            f"parents during {host} degrade",
        )
        _assert_fio_healthy(
            clients,
            ALBENC_JOB,
            ALBENC_FIO_LOG,
            f"albenc during {host} degrade",
        )

        # Bring KMIP back up and restart the gateway — it will now fetch the
        # passphrase successfully and bring the albenc NS out of degraded.
        # Key UUID is unchanged so no ns reload is needed.
        _start_kmip_servers(kmip_nodes)
        _restart_one_gateway(nvme_service, restarted, config)
        elapsed = _wait_albenc_recovered(restarted, albenc, reopen, 15)
        recovery_timings.append((host, elapsed))
        LOG.info(
            "NS recovery timing: %s -> %ss for %s albenc NS to recover after "
            "KMIP restore + GW restart",
            host,
            elapsed,
            len(albenc),
        )
        recovered_baseline = _wait_lb_baseline(gateway, albenc)
        _wait_alb_window(config, f"after recovery {host}")
        _assert_albenc_healthy(gateways, albenc, f"after recovery {host}")
        if policy == "long_term":
            validate_auto_loadbalance(
                orch, nvme_service.nvme_metadata_pool, nvme_service.group
            )
        else:
            _assert_groups_unchanged(
                gateway,
                albenc,
                recovered_baseline,
                f"short-term hold after recovery {host}",
            )
        _assert_fio_healthy(
            clients,
            PARENT_JOB,
            PARENT_FIO_LOG,
            f"parents after recovery {host}",
        )
        _assert_fio_healthy(
            clients,
            ALBENC_JOB,
            ALBENC_FIO_LOG,
            f"albenc after recovery {host}",
        )

    LOG.info(
        "ALB ENC NS recovery summary (%s albenc NS per restart):\n%s",
        len(albenc),
        "\n".join(
            f"  {host}: {secs}s ({secs // 60}m {secs % 60}s)"
            for host, secs in recovery_timings
        ),
    )
    stop_state["stopped"] = True
    _stop_fio(clients)
