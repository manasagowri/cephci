"""NVMeoF BYOK (KMIP) scale test.

Configures dummy KMIP servers, 32 subsystems on a gateway group with
auto-listeners, KMIP endpoints (2 subsystems per KMIP node), encrypted
namespaces (LUKS1/LUKS2), and encrypted clones.

IO, namespace masking, DHCHAP, and HA live in
``test_ceph_nvmeof_byok_features.py``.
"""

import json

from ceph.ceph import Ceph, CommandFailed
from ceph.ceph_admin.orch import Orch
from ceph.parallel import parallel
from ceph.utils import get_nodes_by_ids
from tests.nvmeof.workflows.byok_kmip import (
    CLONE_PASSPHRASE_VALUE,
    DEFAULT_KMIP_CLI_IMAGE,
    DEFAULT_KMIP_IMAGE,
    KMIP_PORT,
    ensure_clone_passphrases_all,
    kmip_server_name,
    load_passphrases_all,
    secret_log_id,
    setup_kmip_infrastructure,
    short_hostname,
)
from tests.nvmeof.workflows.gateway_entities import (
    teardown,
    validate_listeners,
    validate_namespaces,
)
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import (
    check_and_set_nvme_cli_image,
    get_network_mask,
)
from tests.rbd.rbd_utils import Rbd
from utility.log import Log
from utility.retry import retry

LOG = Log(__name__)

CLONE_PASSPHRASE_FILE = "/tmp/byok_clone.passphrase"
DEFAULT_NQN_PREFIX = "nqn.2016-06.io.spdk:cnode"
DEFAULT_LISTENER_PORT = 4420


def _group_nqn(nqn, group):
    """Append gateway group to NQN if it is not already present."""
    if not group:
        return nqn
    suffix = f".{group}"
    if nqn.endswith(suffix):
        return nqn
    return f"{nqn}{suffix}"


def _log_orch_hosts(ceph_cluster):
    """Log ``ceph orch host ls`` for debug (operator mapping)."""
    orch = Orch(ceph_cluster, **{})
    out, err = orch.shell(args=["ceph", "orch", "host", "ls"])
    LOG.info(f"ceph orch host ls:\n{out}\n{err or ''}")


def _sorted_kmip_nodes(ceph_cluster, config=None):
    """Return KMIP nodes from suite ``kmip_nodes`` or the ``kmip`` role."""
    config = config or {}
    node_ids = config.get("kmip_nodes")
    if node_ids:
        if not isinstance(node_ids, list):
            node_ids = [node_ids]
        nodes = get_nodes_by_ids(ceph_cluster, node_ids)
        if not nodes:
            raise ValueError(f"No KMIP nodes matched {node_ids}")
        return sorted(nodes, key=lambda n: short_hostname(n))
    nodes = ceph_cluster.get_nodes(role="kmip")
    if not nodes:
        raise ValueError(
            "No KMIP nodes: set config kmip_nodes or add role kmip to the conf"
        )
    return sorted(nodes, key=lambda n: short_hostname(n))


def _existing_subsystems(gateway, config):
    """Build subsystem metadata from NQNs already present on the gateway."""
    count = int(config.get("subsystems", 32))
    group = config.get("gw_group")
    nqn_prefix = config.get("nqn_prefix", DEFAULT_NQN_PREFIX)
    created = []
    for num in range(1, count + 1):
        nqn = f"{nqn_prefix}{num}"
        created.append(
            {
                "num": num,
                "nqn": nqn,
                "group_nqn": _group_nqn(nqn, group),
            }
        )
    _bind_actual_nqns(gateway, created)
    LOG.info("Reusing %s existing subsystems", len(created))
    return created


def _add_subsystems(gateway, config):
    """Create subsystems with network-mask auto-listeners. Return metadata list."""
    count = int(config.get("subsystems", 32))
    group = config.get("gw_group")
    nqn_prefix = config.get("nqn_prefix", DEFAULT_NQN_PREFIX)
    network_mask = config.get("network_mask")
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    max_ns = int(config.get("max_namespaces", ns_per_sub * 2))
    created = []

    def _add(num):
        nqn = f"{nqn_prefix}{num}"
        group_nqn = _group_nqn(nqn, group)
        args = {
            "nqn": nqn,
            "serial_number": str(num),
            "max_namespaces": max_ns,
            "network-mask": network_mask,
        }
        LOG.info(f"Adding subsystem {nqn} network-mask={network_mask}")
        try:
            gateway.subsystem.add(**{"args": args})
        except CommandFailed as exc:
            if "already" not in str(exc).lower():
                raise
            LOG.info("Subsystem %s already present; reusing it", nqn)
        return {
            "num": num,
            "nqn": nqn,
            "group_nqn": group_nqn,
        }

    with parallel() as p:
        for num in range(1, count + 1):
            p.spawn(_add, num)
        for item in p:
            created.append(item)

    created.sort(key=lambda item: item["num"])
    _bind_actual_nqns(gateway, created)
    return created


def _bind_actual_nqns(gateway, subsystems):
    """Use NQNs returned by subsystem list (with or without group suffix)."""
    out, _ = gateway.subsystem.list(**{"base_cmd_args": {"format": "json"}})
    listed = json.loads(out).get("subsystems", []) if out else []
    actual = {item.get("nqn") for item in listed if item.get("nqn")}
    for sub in subsystems:
        if sub["group_nqn"] in actual:
            continue
        if sub["nqn"] in actual:
            LOG.info(
                f"Subsystem listed without group suffix; using {sub['nqn']}"
            )
            sub["group_nqn"] = sub["nqn"]
            continue
        raise RuntimeError(
            f"Subsystem {sub['nqn']} (also tried {sub['group_nqn']}) not in "
            f"subsystem list: {sorted(actual)}"
        )


@retry(Exception, tries=12, delay=5)
def _verify_listeners(gateway, subsystems, gw_nodes, listener_port):
    """Assert auto-listeners exist on every GW for every subsystem."""
    expected = [
        {
            "traddr": node.ip_address,
            "trsvcid": str(listener_port),
            "host-name": node.hostname,
        }
        for node in gw_nodes
    ]
    for sub in subsystems:
        nqn = sub["group_nqn"]
        LOG.info(f"Verifying listeners on {nqn}")
        validate_listeners(gateway, expected, nqn)


def _assign_kmip_endpoints(subsystems, kmip_nodes, subsystems_per_kmip):
    """Map subsystems in order: first KMIP node owns the first N subsystems."""
    needed = len(subsystems)
    capacity = len(kmip_nodes) * subsystems_per_kmip
    if capacity < needed:
        raise ValueError(
            f"Need {needed} subsystem KMIP slots but have {len(kmip_nodes)} "
            f"KMIP nodes * {subsystems_per_kmip} = {capacity}"
        )
    for index, sub in enumerate(subsystems):
        kmip_index = index // subsystems_per_kmip
        sub["kmip_node"] = kmip_nodes[kmip_index]
    return subsystems


def _add_kmip_endpoints(gateway, subsystems, kmip_port):
    """Register one KMIP endpoint per subsystem (shared 2-per-node)."""
    for sub in subsystems:
        kmip_node = sub["kmip_node"]
        name = kmip_server_name(kmip_node)
        nqn = sub["group_nqn"]
        listed_out, _ = gateway.subsystem.list_kmip_server_endpoints(
            **{
                "base_cmd_args": {"format": "json"},
                "args": {"nqn": nqn},
            }
        )
        listed = listed_out or ""
        if name in listed or kmip_node.ip_address in listed:
            LOG.info(f"KMIP endpoint {name} already listed for {nqn}; skipping add")
            sub["kmip_name"] = name
            continue
        LOG.info(
            f"add_kmip_server_endpoint {nqn} {name} {kmip_node.ip_address} {kmip_port}"
        )
        gateway.subsystem.add_kmip_server_endpoint(
            **{
                "positional_args": [
                    nqn,
                    name,
                    kmip_node.ip_address,
                    kmip_port,
                ]
            }
        )
        out, _ = gateway.subsystem.list_kmip_server_endpoints(
            **{
                "base_cmd_args": {"format": "json"},
                "args": {"nqn": nqn},
            }
        )
        listed = out or ""
        if name not in listed and kmip_node.ip_address not in listed:
            raise RuntimeError(
                f"KMIP endpoint {name} not listed for {nqn}: {listed}"
            )
        sub["kmip_name"] = name


def _keys_for_node(passphrases_by_node, node):
    """Look up KMIP passphrases for a node (object identity or hostname)."""
    if node in passphrases_by_node:
        return passphrases_by_node[node]
    host = short_hostname(node)
    for kmip_node, keys in passphrases_by_node.items():
        if short_hostname(kmip_node) == host:
            return keys
    raise KeyError(f"No KMIP passphrases recorded for {host}")


def _parent_format_and_key(ns_index, ns_per_sub, passphrases):
    """First half of namespaces: luks1/key1; second half: luks2/key2.

    A single-namespace subsystem uses LUKS1 so the clone can stack
    ``luks2,luks1`` the same way as the manual VM repro.
    """
    if ns_per_sub <= 1:
        return "luks1", passphrases["parent_luks1"]
    half = ns_per_sub // 2
    if ns_index <= half:
        return "luks1", passphrases["parent_luks1"]
    return "luks2", passphrases["parent_luks2"]


def _clone_format_and_key(parent_fmt, passphrases):
    """Clone layer uses the opposite LUKS format of the parent.

    Every clone uses the same KMIP secret ``passwd`` (passphrases['clone']).
    """
    if parent_fmt == "luks1":
        return "luks2", passphrases["clone"]
    return "luks1", passphrases["clone"]


def _image_name(sub_num, ns_index, fmt):
    return f"byok_c{sub_num:02d}_n{ns_index:02d}_{fmt}"


def _listed_ns_map(gateway, nqn):
    """Return ``{rbd_image_name: nsid}`` for namespaces on a subsystem."""
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    listed = json.loads(out).get("namespaces", []) if out else []
    mapping = {}
    for ns in listed:
        name = ns.get("rbd_image_name")
        if name:
            mapping[name] = ns.get("nsid")
    return mapping


def _rbd_image_names(rbd_obj, pool):
    """Return the set of image names in an RBD pool."""
    out = rbd_obj.exec_cmd(cmd=f"rbd ls {pool} --format json", output=True, check_ec=False)
    if not isinstance(out, str) or not out.strip():
        return set()
    try:
        names = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(names, list):
        return set()
    return {name for name in names if name}


def _ns_already_present(exc):
    text = str(exc).lower()
    return "namespace" in text and any(
        token in text for token in ("already", "duplicate", "exists")
    )


def _rbd_image_already_exists(exc):
    text = str(exc).lower()
    return "image" in text and "already" in text


@retry(CommandFailed, tries=6, delay=15, backoff=1)
def _ns_add(gateway, args):
    """Add a namespace, retrying transient gateway connect failures."""
    add_args = dict(args)
    try:
        return gateway.namespace.add(**{"args": add_args})
    except CommandFailed as exc:
        if _ns_already_present(exc):
            LOG.info(
                "ns %s already present on %s; skipping add",
                add_args.get("rbd_image_name"),
                add_args.get("nqn"),
            )
            return
        if _rbd_image_already_exists(exc) and add_args.get("rbd-create-image"):
            LOG.info(
                "RBD image %s already exists; ns add without create-image",
                add_args.get("rbd_image_name"),
            )
            add_args["rbd-create-image"] = False
            return gateway.namespace.add(**{"args": add_args})
        if "wrong passphrase" in str(exc).lower():
            raise RuntimeError(str(exc)) from exc
        raise


def _parent_ns_meta(sub, ns_index, ns_per_sub, passphrases_by_node):
    keys = _keys_for_node(passphrases_by_node, sub["kmip_node"])
    fmt, key = _parent_format_and_key(ns_index, ns_per_sub, keys)
    return {
        "image": _image_name(sub["num"], ns_index, fmt),
        "format": fmt,
        "key": key,
        "nqn": sub["group_nqn"],
        "ns_index": ns_index,
    }


def _add_parent_namespaces(gateway, subsystems, config, passphrases_by_node, rbd_obj=None):
    """Add encrypted parent namespaces (create_image + single format/key-id).

    Images already present on the subsystem are skipped so a resume can
    finish a partially added subsystem (e.g. cnode10) and continue later
    ones. If the gateway still lists a namespace whose RBD image is gone
    (pool was recreated), delete that leftover NS and add it again.
    """
    pool = config.get("rbd_pool", "rbd")
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    size = config.get("image_size", "50G")
    start_sub = int(config.get("resume_namespace_subsystem", 1))
    existing_rbd = _rbd_image_names(rbd_obj, pool) if rbd_obj else set()

    def _add_one(sub, ns_index, ns_map):
        meta = _parent_ns_meta(sub, ns_index, ns_per_sub, passphrases_by_node)
        image = meta["image"]
        nqn = meta["nqn"]
        if image in ns_map:
            if image in existing_rbd:
                LOG.info(f"ns {image} already on {nqn}; skipping add")
                return meta
            nsid = ns_map.get(image)
            LOG.info(
                f"Stale ns {image} nsid={nsid} on {nqn} (RBD image missing); deleting"
            )
            try:
                gateway.namespace.delete(
                    **{"args": {"nqn": nqn, "nsid": nsid, "force": True}}
                )
            except CommandFailed as exc:
                LOG.warning(f"Failed to delete stale ns {image} nsid={nsid}: {exc}")
        LOG.info(
            f"ns add {nqn} image={image} format={meta['format']} "
            f"key-id={meta['key']['uuid']}"
        )
        _ns_add(
            gateway,
            {
                "nqn": nqn,
                "rbd_pool": pool,
                "rbd_image_name": image,
                "size": size,
                "rbd-create-image": True,
                "encryption-format": meta["format"],
                "key-id": meta["key"]["uuid"],
            },
        )
        return meta

    for sub in subsystems:
        ns_map = _listed_ns_map(gateway, sub["group_nqn"])
        expected_meta = [
            _parent_ns_meta(sub, idx, ns_per_sub, passphrases_by_node)
            for idx in range(1, ns_per_sub + 1)
        ]
        if sub["num"] < start_sub:
            missing = [m["image"] for m in expected_meta if m["image"] not in ns_map]
            if not missing:
                LOG.info(
                    f"cnode{sub['num']}: {len(ns_map)} parent namespaces "
                    "already present; reusing"
                )
                sub["namespaces"] = expected_meta
                continue
            LOG.info(
                f"cnode{sub['num']}: backfilling missing parent namespaces {missing}"
            )
        ns_meta = []
        with parallel() as p:
            for ns_index in range(1, ns_per_sub + 1):
                p.spawn(_add_one, sub, ns_index, ns_map)
            for result in p:
                ns_meta.append(result)
        ns_meta.sort(key=lambda item: item["ns_index"])
        sub["namespaces"] = ns_meta


def _write_passphrase_file(rbd_obj, path, value, uuid=None):
    """Write a LUKS passphrase file with no trailing newline (matches KMIP value)."""
    LOG.info(
        f"Writing {path} uuid={uuid} secret_id={secret_log_id(value)} "
        f"secret_len={len(value)}"
    )
    rbd_obj.exec_cmd(cmd=f"echo -n '{value}' > {path}")
    return path


def _remove_existing_clone(
    gateway, rbd_obj, pool, nqn, clone_image, ns_map, existing_rbd
):
    """Drop clone namespace and RBD image so they can be recreated cleanly."""
    nsid = ns_map.get(clone_image)
    if nsid is not None:
        LOG.info(
            f"Removing existing clone namespace {clone_image} nsid={nsid} from {nqn}"
        )
        try:
            gateway.namespace.delete(
                **{"args": {"nqn": nqn, "nsid": nsid}}
            )
        except CommandFailed as exc:
            LOG.warning(
                f"Failed to delete clone namespace {clone_image} nsid={nsid}: {exc}"
            )
        ns_map.pop(clone_image, None)
    LOG.info(f"Removing existing clone image {pool}/{clone_image} if present")
    rbd_obj.exec_cmd(cmd=f"rbd rm {pool}/{clone_image}", check_ec=False)
    existing_rbd.discard(clone_image)


def _rbd_cmd(rbd_obj, cmd, ok_substrings=()):
    """Run an RBD command and raise with stderr unless the error is expected."""
    out, err = rbd_obj.exec_cmd(cmd=cmd, all=True)
    combined = f"{out} {err}".lower()
    if out == 1:
        if any(token in combined for token in ok_substrings):
            LOG.info(f"{cmd} already satisfied ({err})")
            return out, err
        raise RuntimeError(f"{cmd} failed: {err}")
    return out, err


def _remove_snap_children(rbd_obj, snap_spec):
    """Remove leftover clones of a snapshot so it can be unprotected."""
    out, err = rbd_obj.exec_cmd(cmd=f"rbd children {snap_spec}", all=True)
    text = "" if out == 1 else str(out or "")
    children = [line.strip() for line in text.splitlines() if line.strip()]
    for child in children:
        LOG.info(f"Removing leftover clone child {child} of {snap_spec}")
        rbd_obj.exec_cmd(cmd=f"rbd rm {child}", check_ec=False)


def _recreate_protected_snap(rbd_obj, pool, image, snap):
    """Always create a new protected snapshot for this run.

    Existing snapshots are unprotected and removed first so the clone is
    taken after the current parent resize, not from a leftover snap.
    """
    snap_spec = f"{pool}/{image}@{snap}"
    snaps = rbd_obj.snap_ls(pool, image, snap)
    if snaps:
        LOG.info(f"Removing existing snapshot {snap_spec} to recreate it")
        _remove_snap_children(rbd_obj, snap_spec)
        _rbd_cmd(
            rbd_obj,
            f"rbd snap unprotect {snap_spec}",
            ok_substrings=("not protected", "is unprotected"),
        )
        _rbd_cmd(
            rbd_obj,
            f"rbd snap rm {snap_spec}",
            ok_substrings=("no such snapshot", "does not exist"),
        )
    LOG.info(f"Creating snapshot {snap_spec}")
    _rbd_cmd(
        rbd_obj,
        f"rbd snap create {snap_spec}",
        ok_substrings=("already exists",),
    )
    _rbd_cmd(
        rbd_obj,
        f"rbd snap protect {snap_spec}",
        ok_substrings=("already protected",),
    )


def _encrypt_clone(rbd_obj, pool, clone_image, clone_fmt, clone_key, passphrase_file=None):
    """Apply LUKS format to a clone image using the shared clone secret."""
    passphrase_file = passphrase_file or CLONE_PASSPHRASE_FILE
    result = rbd_obj.exec_cmd(
        cmd=f"rbd encryption format {pool}/{clone_image} {clone_fmt} {passphrase_file}",
        all=True,
    )
    out, err = result if isinstance(result, tuple) else (result, "")
    if out == 1:
        text = str(err).lower()
        if any(token in text for token in ("already encrypted", "already formatted")):
            LOG.info(
                f"{pool}/{clone_image} already encrypted; continuing to ns add"
            )
            return passphrase_file
        raise RuntimeError(
            f"rbd encryption format failed for {pool}/{clone_image}: {err}"
        )
    LOG.info(f"Applied {clone_fmt} encryption to {pool}/{clone_image}")
    return passphrase_file


def _rbd_image_missing(rbd_obj, pool, image):
    """Return True if the RBD image is not present in the pool."""
    out, err = rbd_obj.exec_cmd(cmd=f"rbd info {pool}/{image}", all=True)
    combined = f"{out} {err}".lower()
    return out == 1 and any(
        token in combined
        for token in ("no such file", "no such image", "does not exist", "not found")
    )


def _rbd_resize(rbd_obj, pool, image, size):
    """Resize the RBD image only; LUKS passphrase file is not required."""
    cmd = f"rbd resize --size {size} {pool}/{image} --allow-shrink"
    LOG.info(cmd)
    out, err = rbd_obj.exec_cmd(cmd=cmd, all=True)
    combined = f"{out} {err}".lower()
    if out == 1 and "new size is equal to original size" not in combined:
        if any(
            token in combined
            for token in ("no such file", "no such image", "does not exist", "not found")
        ):
            LOG.warning("Skipping resize; image %s/%s is missing", pool, image)
            return False
        raise RuntimeError(f"rbd resize failed for {pool}/{image}: {err}")
    return True


def _clone_context(sub, ns_meta, passphrases_by_node):
    """Build clone names, formats, and keys for one parent namespace."""
    keys = _keys_for_node(passphrases_by_node, sub["kmip_node"])
    parent_fmt = ns_meta["format"]
    clone_fmt, clone_key = _clone_format_and_key(parent_fmt, keys)
    image = ns_meta["image"]
    return {
        "nqn": sub["group_nqn"],
        "image": image,
        "snap": f"{image}_snap",
        "clone_image": f"{image}_clone",
        "parent_fmt": parent_fmt,
        "parent_key": ns_meta["key"],
        "clone_fmt": clone_fmt,
        "clone_key": clone_key,
    }


def _clone_and_add_namespaces(gateway, rbd_obj, subsystems, config, passphrases_by_node):
    """Clone parents, apply LUKS, compensate header size, then ns-add.

    Clones already present on the subsystem are left alone. Missing parent
    images are skipped. Only remaining clones are created. A failed clone
    ns add is skipped so later images and subsystems can continue.
    """
    pool = config.get("rbd_pool", "rbd")
    size = config.get("image_size", "50G")
    existing_rbd = _rbd_image_names(rbd_obj, pool)

    def _prepare_parent(sub, ns_meta, ns_map):
        ctx = _clone_context(sub, ns_meta, passphrases_by_node)
        ctx["ns_meta"] = ns_meta
        if _rbd_image_missing(rbd_obj, pool, ctx["image"]):
            LOG.warning(
                "Parent image %s/%s is missing; skipping clone %s",
                pool,
                ctx["image"],
                ctx["clone_image"],
            )
            return None
        _remove_existing_clone(
            gateway,
            rbd_obj,
            pool,
            ctx["nqn"],
            ctx["clone_image"],
            ns_map,
            existing_rbd,
        )
        _rbd_resize(rbd_obj, pool, ctx["image"], size)
        _recreate_protected_snap(rbd_obj, pool, ctx["image"], ctx["snap"])
        return ctx

    def _create_clone(ctx):
        image = ctx["image"]
        clone_image = ctx["clone_image"]
        snap = ctx["snap"]
        LOG.info(f"Cloning {pool}/{image} -> {clone_image} format={ctx['clone_fmt']}")
        if rbd_obj.create_clone(f"{pool}/{image}@{snap}", pool, clone_image):
            raise RuntimeError(f"Failed to clone {pool}/{image}@{snap}")
        existing_rbd.add(clone_image)
        return clone_image

    def _encrypt_resize_add(ctx):
        image = ctx["image"]
        clone_image = ctx["clone_image"]
        parent_fmt = ctx["parent_fmt"]
        clone_fmt = ctx["clone_fmt"]
        parent_key = ctx["parent_key"]
        clone_key = ctx["clone_key"]
        nqn = ctx["nqn"]

        _encrypt_clone(
            rbd_obj,
            pool,
            clone_image,
            clone_fmt,
            clone_key,
            passphrase_file=CLONE_PASSPHRASE_FILE,
        )
        if parent_fmt == "luks1" and clone_fmt == "luks2":
            _rbd_resize(rbd_obj, pool, image, size)
        if parent_fmt != clone_fmt:
            _rbd_resize(rbd_obj, pool, clone_image, size)

        LOG.info(
            f"ns add clone {nqn} image={clone_image} "
            f"formats={clone_fmt},{parent_fmt} "
            f"key-ids={clone_key['uuid']},{parent_key['uuid']}"
        )
        _ns_add(
            gateway,
            {
                "nqn": nqn,
                "rbd_pool": pool,
                "rbd_image_name": clone_image,
                "encryption-format": f"{clone_fmt},{parent_fmt}",
                "key-id": f"{clone_key['uuid']},{parent_key['uuid']}",
            },
        )
        return clone_image

    for sub in subsystems:
        ns_map = _listed_ns_map(gateway, sub["group_nqn"])
        prepared = []
        skipped = []
        for ns_meta in sub["namespaces"]:
            ctx = _clone_context(sub, ns_meta, passphrases_by_node)
            if ctx["clone_image"] in ns_map:
                LOG.info(
                    "clone %s already on %s; leaving it in place",
                    ctx["clone_image"],
                    sub["group_nqn"],
                )
                skipped.append(ctx["clone_image"])
                continue
            prepared_ctx = _prepare_parent(sub, ns_meta, ns_map)
            if prepared_ctx is None:
                continue
            prepared.append(prepared_ctx)
        if not prepared:
            LOG.info(
                "%s: all %s clones already present; nothing to retry",
                sub["group_nqn"],
                len(skipped),
            )
            sub["clones"] = skipped
            continue
        LOG.info(
            "%s: skipping %s existing clones; retrying %s missing clones",
            sub["group_nqn"],
            len(skipped),
            len(prepared),
        )
        with parallel() as p:
            for ctx in prepared:
                p.spawn(_create_clone, ctx)
            for _ in p:
                pass
        _write_passphrase_file(
            rbd_obj,
            CLONE_PASSPHRASE_FILE,
            CLONE_PASSPHRASE_VALUE,
        )
        clones = list(skipped)
        for ctx in prepared:
            try:
                clones.append(_encrypt_resize_add(ctx))
            except Exception as exc:
                LOG.warning(
                    "Skipping clone %s on %s and continuing: %s",
                    ctx["clone_image"],
                    ctx["nqn"],
                    exc,
                )
        sub["clones"] = clones


def _verify_namespaces(gateway, subsystems, config):
    """Each subsystem should have the parent and clone namespaces that exist."""
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    for sub in subsystems:
        nqn = sub["group_nqn"]
        ns_map = _listed_ns_map(gateway, nqn)
        expected_parents = [
            ns["image"] for ns in sub["namespaces"] if ns["image"] in ns_map
        ]
        expected = expected_parents + list(sub["clones"])
        validate_namespaces(gateway, expected, nqn)
        listed = json.loads(
            gateway.namespace.list(
                **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
            )[0]
            or "{}"
        ).get("namespaces", [])
        LOG.info(
            "%s: verified %s namespaces (parents=%s clones=%s full=%s)",
            nqn,
            len(listed),
            len(expected_parents),
            len(sub["clones"]),
            ns_per_sub * 2,
        )


def _pool_names(rbd_obj):
    """Return pool names from ``ceph osd pool ls`` (empty list on failure)."""
    out = rbd_obj.exec_cmd(cmd="ceph osd pool ls", output=True, check_ec=False)
    if not isinstance(out, str):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _init_rbd(kwargs):
    """Return an Rbd helper, creating the pool only if it is missing.

    Do not use ``initial_rbd_config`` / ``check_pool_exists``: those treat
    "pool already exists" and ``ceph df`` failures as fatal, which breaks
    --reuse on an already-deployed cluster.
    """
    config = kwargs.get("config") or {}
    pool = config.get("rbd_pool") or config.get("rep_pool_config", {}).get(
        "pool", "rbd"
    )
    rbd_obj = Rbd(**kwargs)
    pools = _pool_names(rbd_obj)
    if pool not in pools:
        LOG.info(f"Creating RBD pool {pool}")
        rbd_obj.exec_cmd(cmd=f"ceph osd pool create {pool}", check_ec=False)
        pools = _pool_names(rbd_obj)
        if pool not in pools:
            raise RuntimeError(f"Failed to create or find RBD pool {pool}")
    else:
        LOG.info(f"RBD pool {pool} already exists; reusing it")
    rbd_obj.exec_cmd(cmd=f"rbd pool init {pool}", check_ec=False)
    return rbd_obj


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Execute the NVMeoF BYOK KMIP scale workflow.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    nvme_service = None
    try:
        rbd_obj = _init_rbd(kwargs)
        nvme_service = NVMeService(config, ceph_cluster)
        if config.get("install"):
            nvme_service.deploy()
        nvme_service.init_gateways()
        gateway = nvme_service.gateways[0]

        _log_orch_hosts(ceph_cluster)

        kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
        LOG.info("KMIP nodes: %s", [short_hostname(n) for n in kmip_nodes])
        gw_nodes = [gw.node for gw in nvme_service.gateways]
        if not config.get("network_mask"):
            config["network_mask"] = get_network_mask(nvme_service.gateways)
            LOG.info(f"Derived network_mask={config['network_mask']}")

        resume_from = config.get("resume_from")
        kmip_cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)
        subsystems_per_kmip = int(config.get("subsystems_per_kmip", 2))

        if resume_from in ("namespaces", "clones"):
            if resume_from == "clones":
                LOG.info(
                    "resume_from=clones: keep clones already on the subsystem; "
                    "skip missing parent images; retry only remaining clones"
                )
                ensure_clone_passphrases_all(
                    kmip_nodes, cli_image=kmip_cli_image
                )
                passphrases_by_node = load_passphrases_all(
                    kmip_nodes, cli_image=kmip_cli_image
                )
                subsystems = _existing_subsystems(gateway, config)
                _assign_kmip_endpoints(subsystems, kmip_nodes, subsystems_per_kmip)
                ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
                for sub in subsystems:
                    sub["namespaces"] = [
                        _parent_ns_meta(
                            sub, idx, ns_per_sub, passphrases_by_node
                        )
                        for idx in range(1, ns_per_sub + 1)
                    ]
            else:
                passphrases_by_node = load_passphrases_all(
                    kmip_nodes, cli_image=kmip_cli_image
                )
                subsystems = _existing_subsystems(gateway, config)
                _assign_kmip_endpoints(subsystems, kmip_nodes, subsystems_per_kmip)
                LOG.info(
                    "resume_from=namespaces: reuse KMIP/subsystems/endpoints; "
                    "recreate parent namespaces from cnode%s",
                    config.get("resume_namespace_subsystem", 1),
                )
                _add_parent_namespaces(
                    gateway,
                    subsystems,
                    config,
                    passphrases_by_node,
                    rbd_obj=rbd_obj,
                )
        else:
            reuse_kmip = resume_from == "kmip_endpoints"
            if reuse_kmip:
                LOG.info(
                    "resume_from=kmip_endpoints: reuse KMIP servers and existing "
                    "subsystems; recopy certs, redeploy gateways, then add KMIP endpoints"
                )
            _, passphrases_by_node = setup_kmip_infrastructure(
                kmip_nodes,
                gw_nodes,
                kmip_image=config.get("kmip_image", DEFAULT_KMIP_IMAGE),
                kmip_cli_image=kmip_cli_image,
                kmip_port=int(config.get("kmip_port", KMIP_PORT)),
                nvme_service=nvme_service,
                reuse_existing=reuse_kmip,
            )
            gateway = nvme_service.gateways[0]
            gw_nodes = [gw.node for gw in nvme_service.gateways]
            if reuse_kmip:
                subsystems = _existing_subsystems(gateway, config)
            else:
                subsystems = _add_subsystems(gateway, config)
            listener_port = config.get("listener_port", DEFAULT_LISTENER_PORT)
            _verify_listeners(gateway, subsystems, gw_nodes, listener_port)
            _assign_kmip_endpoints(subsystems, kmip_nodes, subsystems_per_kmip)
            _add_kmip_endpoints(
                gateway, subsystems, int(config.get("kmip_port", KMIP_PORT))
            )
            _add_parent_namespaces(
                gateway,
                subsystems,
                config,
                passphrases_by_node,
                rbd_obj=rbd_obj,
            )
        _clone_and_add_namespaces(
            gateway, rbd_obj, subsystems, config, passphrases_by_node
        )
        _verify_namespaces(gateway, subsystems, config)
        config["subsystems"] = [{"nqn": sub["group_nqn"]} for sub in subsystems]
        return 0
    except Exception as err:
        LOG.exception("NVMeoF BYOK KMIP test failed: %s", err)
        return 1
    finally:
        if config.get("cleanup") and nvme_service:
            teardown(nvme_service, rbd_obj)
