"""
IBMCEPH-19516: encrypted namespaces must rebalance when NS location
does not match the serving ANA / GW location.

Buggy builds refuse the move (rebalance.py: "not allowed rebalance:
namespace is encrypted") and leave encrypted NS on the wrong ANA group.

Fixed-in: ceph-20.2.2-364.el10cp / ceph-nvmeof#2195.

Config:
    expect_encrypted_ns_rebalance (bool): False = 9.1 repro (must stay stuck
        or journal the encrypted block). True = 9.2z1 verify (must move).
    location_site_a / location_site_b: GW/NS location labels.
    rebalance_timeout_sec / rebalance_poll_sec: wait for ANA placement.
"""

import json
import time

from looseversion import LooseVersion

from ceph.ceph import Ceph
from ceph.ceph_admin.orch import Orch
from ceph.utils import get_node_by_id
from ceph.waiter import WaitUntil
from cli.ceph.nvme_gw import NvmeGw
from tests.nvmeof.workflows.gateway_entities import (
    configure_hosts,
    configure_listeners,
    configure_subsystems,
    teardown,
)
from tests.nvmeof.workflows.ha import HighAvailability
from tests.nvmeof.workflows.kmip_utils import (
    acquire_kmip_for_nvmeof,
    configure_kmip_endpoint_on_subsystem,
    register_kmip_passphrases,
    release_kmip_if_owned,
)
from tests.nvmeof.workflows.nvme_encryption import add_encrypted_namespace
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import (
    check_and_set_nvme_cli_image,
    string_to_dict,
)
from tests.nvmeof.workflows.stretch_ha import build_gw_location_map
from tests.rbd.rbd_utils import initial_rbd_config
from utility.log import Log
from utility.utils import (
    generate_unique_id,
    get_ceph_version_from_cluster,
)

LOG = Log(__name__)

ENC_BLOCK_MARKER = "not allowed rebalance: namespace is encrypted"


def _parse_json_blob(out):
    """Parse JSON from cephadm output that may include leading log lines."""
    if isinstance(out, (dict, list)):
        return out
    text = out if isinstance(out, str) else str(out)
    idx_obj, idx_arr = text.find("{"), text.find("[")
    candidates = []
    if idx_obj != -1:
        candidates.append((idx_obj, "{", "}"))
    if idx_arr != -1:
        candidates.append((idx_arr, "[", "]"))
    candidates.sort(key=lambda item: item[0])
    last_err = None
    for _, opener, closer in candidates:
        start, end = text.find(opener), text.rfind(closer)
        if start == -1 or end <= start:
            continue
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as extra:
            last_err = extra
    raise ValueError(f"Unable to parse JSON blob: {last_err}; raw={text[:500]!r}")


def _nvme_gw_cli(ceph_cluster):
    orch = Orch(cluster=ceph_cluster, **{})
    return NvmeGw(orch.installer, "ceph")


def _show_created_gateways(nvme_gw, pool, group):
    out = nvme_gw.show(pool, group, format="json")
    data = _parse_json_blob(out)
    return data.get("Created Gateways:", data.get("gateways", [])) or []


def _list_namespaces(gateway, nqn):
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    data = _parse_json_blob(out)
    return data.get("namespaces") or []


def _ns_by_image(namespaces, image):
    for ns in namespaces:
        if ns.get("rbd_image_name") == image:
            return ns
    return None


def _show_location(entry):
    return (entry or {}).get("location") or ""


def _ana_holder(entries, ana_id):
    try:
        ana_id = int(ana_id)
    except (TypeError, ValueError):
        pass
    for entry in entries:
        states = string_to_dict(entry.get("ana states", ""))
        if str(states.get(ana_id, "")).upper() == "ACTIVE":
            return entry
    return None


def _assert_ns_at_location(ns, location, nvme_gw, pool, group):
    """Assert NS location and ACTIVE ANA holder location match *location*."""
    actual = ns.get("location") or ""
    if actual != location:
        raise AssertionError(
            f"Namespace {ns.get('nsid')} location '{actual}' != '{location}'"
        )
    ana_id = ns.get("load_balancing_group")
    holder = _ana_holder(_show_created_gateways(nvme_gw, pool, group), ana_id)
    holder_loc = _show_location(holder)
    if holder_loc != location:
        raise AssertionError(
            f"Namespace {ns.get('nsid')} ANA {ana_id} is active on location "
            f"'{holder_loc}', expected '{location}' "
            f"(holder={(holder or {}).get('gw-id')})"
        )
    LOG.info(
        "Namespace %s location=%s ANA=%s on %s",
        ns.get("nsid"),
        actual,
        ana_id,
        (holder or {}).get("gw-id"),
    )


def _scan_journals_for_encrypted_block(gateways, since_seconds=180):
    """Return journal hits for the encrypted-NS rebalance block, if any."""
    hits = []
    for gw in gateways:
        unit = getattr(gw, "system_unit_id", None)
        if not unit:
            continue
        cmd = (
            f"journalctl -u {unit} --since '{since_seconds} seconds ago' --no-pager "
            f"| grep -F '{ENC_BLOCK_MARKER}' || true"
        )
        out, _ = gw.node.exec_command(sudo=True, cmd=cmd, check_ec=False)
        text = (out or "").strip()
        if text:
            hits.append(f"[{gw.node.hostname}] journal hits:\n{text}")
    return "\n".join(hits)


def _wait_encrypted_ns_rebalance(
    gateway,
    nqn,
    nsid,
    location,
    nvme_gw,
    pool,
    group,
    expect_move,
    timeout,
    interval,
    gateways,
):
    """Wait for location ANA match, or confirm stuck/blocked for repro."""
    last = None
    moved = None
    LOG.info(
        "Waiting up to %ss (poll every %ss) for encrypted nsid %s ANA at %s "
        "(expect_encrypted_ns_rebalance=%s)",
        timeout,
        interval,
        nsid,
        location,
        expect_move,
    )
    for _ in WaitUntil(timeout=timeout, interval=interval):
        namespaces = _list_namespaces(gateway, nqn)
        moved = next((ns for ns in namespaces if ns.get("nsid") == nsid), None)
        if not moved:
            last = AssertionError(f"nsid {nsid} not listed")
            LOG.info("Waiting for namespace %s at %s: %s", nsid, location, last)
            continue
        try:
            _assert_ns_at_location(moved, location, nvme_gw, pool, group)
        except AssertionError as extra:
            last = extra
            LOG.info("Waiting for namespace %s at %s: %s", nsid, location, extra)
            if not expect_move:
                journal = _scan_journals_for_encrypted_block(
                    gateways, since_seconds=timeout + 60
                )
                if journal:
                    LOG.info(
                        "IBMCEPH-19516 repro: encrypted rebalance block observed:\n%s",
                        journal,
                    )
                    return moved
            continue

        # Placement matches target location
        if expect_move:
            LOG.info(
                "IBMCEPH-19516: encrypted nsid %s rebalanced to %s (PASS)",
                nsid,
                location,
            )
            return moved
        raise AssertionError(
            f"IBMCEPH-19516 repro expected encrypted nsid {nsid} to stay stuck, "
            f"but it rebalanced to {location}. Build may already include the fix."
        )

    if expect_move:
        journal = _scan_journals_for_encrypted_block(
            gateways, since_seconds=timeout + 60
        )
        raise TimeoutError(
            f"Encrypted nsid {nsid} did not rebalance to {location}: {last}"
            + (f"\n{journal}" if journal else "")
        )

    # Repro: still mismatched after timeout → bug present (PASS)
    LOG.info(
        "IBMCEPH-19516 repro PASS: encrypted nsid %s still not at %s after %ss (%s)",
        nsid,
        location,
        timeout,
        last,
    )
    return moved


def _add_plain_namespace(gateway, nqn, rbd_pool, location, size="2G"):
    image = f"{generate_unique_id(length=6)}-plain"
    gateway.namespace.add(
        **{
            "args": {
                "subsystem": nqn,
                "rbd-pool": rbd_pool,
                "rbd-image": image,
                "rbd-create-image": True,
                "rbd-image-size": size,
                "location": location,
            }
        }
    )
    LOG.info("Added plaintext namespace %s with location %s", image, location)
    return image


def run_encrypted_ns_location_rebalance(ceph_cluster, config, nvme_service, custom_data):
    """Execute IBMCEPH-19516 scenario against the deployed NVMe service."""
    expect_move = bool(config.get("expect_encrypted_ns_rebalance", True))
    loc_a = config.get("location_site_a", "site-A")
    loc_b = config.get("location_site_b", "site-B")
    timeout = int(config.get("rebalance_timeout_sec", 900))
    interval = int(config.get("rebalance_poll_sec", 60))
    bdev_size = config.get("bdev_size", "2G")
    nqn = config["subsystems"][0]["nqn"]
    rbd_pool = config["rbd_pool"]
    gw_nodes = config.get("gw_nodes") or []

    if len(gw_nodes) < 2:
        raise ValueError("IBMCEPH-19516 requires at least two gw_nodes")

    ha = HighAvailability(ceph_cluster, gw_nodes, **config)
    ha.gateways = nvme_service.gateways
    ha.nvme_service = nvme_service

    kmip_info, kmip_owned = acquire_kmip_for_nvmeof(
        ceph_cluster, config, custom_data, nvme_service=nvme_service
    )
    if not kmip_owned and config.get("luks_combos"):
        kmip_node = get_node_by_id(ceph_cluster, config["initiator_node"])
        register_kmip_passphrases(kmip_node, config["luks_combos"], config)

    LOG.info("Redeploying NVMe-oF service so GW daemons pick up KMIP certs")
    nvme_service.redeploy(wait_sec=30)
    nvme_service.init_gateways()
    nvme_service.wait_for_gateways_ready()
    ha.gateways = nvme_service.gateways
    ha.nvme_service = nvme_service
    gateway = nvme_service.gateways[0]

    node_to_loc = {
        gw_nodes[0]: loc_a,
        gw_nodes[1]: loc_b,
    }
    # Any extra GWs alternate sites
    for idx, node in enumerate(gw_nodes[2:]):
        node_to_loc[node] = loc_a if idx % 2 == 0 else loc_b

    try:
        ceph_version = get_ceph_version_from_cluster(
            ceph_cluster.get_nodes(role="client")[0]
        )
        configure_subsystems(nvme_service, ceph_cluster=ceph_cluster)
        if LooseVersion(ceph_version) <= LooseVersion("20.2.1"):
            configure_listeners(nvme_service.gateways, config)
        configure_hosts(gateway, config, ceph_cluster=ceph_cluster)

        gw_location_map = build_gw_location_map(nvme_service.gateways, node_to_loc)
        if not gw_location_map:
            raise RuntimeError(
                f"Could not map gateways to locations from {node_to_loc}"
            )
        LOG.info("Setting GW locations: %s", gw_location_map)
        if not ha.set_gateway_locations(gw_location_map):
            raise RuntimeError("set_gateway_locations failed")

        configure_kmip_endpoint_on_subsystem(gateway, nqn, kmip_info["kmip_cfg"])

        combos = config.get("luks_combos") or []
        if not combos:
            raise ValueError("luks_combos required for encrypted NS")
        combo = combos[0]
        key_id = combo.get("key_uid", combo["key_id"])
        luks_format = combo.get("format") or combo.get("luks_format", "luks2")
        luks_algo = combo.get("algo") or combo.get("luks_algo", "aes256")

        enc_image = add_encrypted_namespace(
            gateway,
            nqn,
            rbd_pool,
            key_id=key_id,
            luks_format=luks_format,
            luks_algo=luks_algo,
            size=bdev_size,
            location=loc_a,
        )
        _add_plain_namespace(gateway, nqn, rbd_pool, loc_a, size=bdev_size)
        _add_plain_namespace(gateway, nqn, rbd_pool, loc_b, size=bdev_size)

        namespaces = _list_namespaces(gateway, nqn)
        enc_ns = _ns_by_image(namespaces, enc_image)
        if not enc_ns:
            raise RuntimeError(f"Encrypted namespace for image {enc_image} not listed")
        nsid = enc_ns.get("nsid")
        LOG.info(
            "Encrypted NS baseline: nsid=%s location=%s lb_group=%s",
            nsid,
            enc_ns.get("location"),
            enc_ns.get("load_balancing_group"),
        )

        nvme_gw = _nvme_gw_cli(ceph_cluster)
        pool = nvme_service.nvme_metadata_pool
        group = nvme_service.group

        # Ensure baseline is correct before change_location
        _assert_ns_at_location(enc_ns, loc_a, nvme_gw, pool, group)

        LOG.info("Changing encrypted nsid %s location %s -> %s", nsid, loc_a, loc_b)
        gateway.namespace.change_location(
            args={"subsystem": nqn, "nsid": nsid, "location": loc_b}
        )
        # Brief settle so journals start emitting rebalance attempts
        time.sleep(5)

        _wait_encrypted_ns_rebalance(
            gateway,
            nqn,
            nsid,
            loc_b,
            nvme_gw,
            pool,
            group,
            expect_move=expect_move,
            timeout=timeout,
            interval=interval,
            gateways=nvme_service.gateways,
        )
        LOG.info(
            "IBMCEPH-19516 scenario complete (expect_encrypted_ns_rebalance=%s)",
            expect_move,
        )
        return 0
    finally:
        release_kmip_if_owned(kmip_info, kmip_owned)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """cephci entry point."""
    config = kwargs["config"]
    custom_data = kwargs.get("test_data", {})
    custom_cfg = custom_data.get("custom-config")

    config.update(
        {
            "do_not_create_image": True,
            "rep-pool-only": True,
            "rep_pool_config": {"pool": config["rbd_pool"]},
        }
    )

    rbd_obj = None
    nvme_service = None
    try:
        rbd_obj = initial_rbd_config(**kwargs)["rbd_reppool"]
        check_and_set_nvme_cli_image(ceph_cluster, config=custom_cfg)

        nvme_service = NVMeService(config, ceph_cluster)
        if config.get("install"):
            config.setdefault("spec_deployment", True)
            nvme_service.deploy()
        nvme_service.init_gateways()

        return run_encrypted_ns_location_rebalance(
            ceph_cluster, config, nvme_service, custom_data
        )
    except Exception as err:
        LOG.exception("IBMCEPH-19516 encrypted NS location rebalance FAILED: %s", err)
        return 1
    finally:
        if config.get("cleanup") and nvme_service is not None and rbd_obj is not None:
            teardown(nvme_service, rbd_obj)
