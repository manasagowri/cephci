"""
IBMCEPH-19516: encrypted namespaces must rebalance when NS location
does not match the serving ANA / GW location.

Jira symptom (IBMCEPH-19516):
  - Mix of encrypted + plaintext NS with site-A / site-B locations
  - Encrypted NS stuck with location ≠ ACTIVE ANA holder
  - GW journal loops on: "not allowed rebalance: namespace is encrypted"

Buggy builds refuse the move (rebalance.py encrypted block). Fixed-in:
ceph-20.2.2-364.el10cp / ceph-nvmeof#2195 (#2194).

Config:
    expect_encrypted_ns_rebalance (bool):
        False = stuck repro (must stay mismatched AND journal the
        encrypted-block marker). True = verify (must move; after settle
        must not keep logging the encrypted-block for that nsid).
    jira_topology (bool): create a dense encrypted+plaintext mix like
        the Jira ns list (default True).
    location_site_a / location_site_b: GW/NS location labels.
    rebalance_timeout_sec / rebalance_poll_sec: wait for ANA placement.
    require_encrypted_block_journal (bool): default True for stuck repro.
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

# Compact Jira-faithful mix: encrypted across both sites + plaintext anchors
# (Jira had 16 encrypted + 4 plaintext; this keeps the same shape).
DEFAULT_JIRA_ENCRYPTED = [
    {"location": "site-A", "key_id": "key-1", "format": "luks1", "algo": "aes256"},
    {"location": "site-A", "key_id": "key-1", "format": "luks1", "algo": "aes256"},
    {"location": "site-B", "key_id": "key-1", "format": "luks2", "algo": "aes256"},
    {"location": "site-B", "key_id": "key-1", "format": "luks2", "algo": "aes256"},
    {"location": "site-A", "key_id": "key-2", "format": "luks1", "algo": "aes256"},
    {"location": "site-A", "key_id": "key-2", "format": "luks1", "algo": "aes256"},
    {"location": "site-B", "key_id": "key-2", "format": "luks2", "algo": "aes256"},
    {"location": "site-B", "key_id": "key-2", "format": "luks2", "algo": "aes256"},
    {"location": "site-A", "key_id": "key-3", "format": "luks1", "algo": "aes256"},
    {"location": "site-B", "key_id": "key-3", "format": "luks2", "algo": "aes256"},
    {"location": "site-A", "key_id": "key-4", "format": "luks2", "algo": "aes256"},
    {"location": "site-B", "key_id": "key-4", "format": "luks1", "algo": "aes256"},
]
DEFAULT_JIRA_PLAIN = ["site-A", "site-A", "site-B", "site-B"]


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


def _scan_journals_for_encrypted_block(gateways, since_seconds=180, nsid=None):
    """Return journal hits for the encrypted-NS rebalance block, if any."""
    hits = []
    nsid_filter = f"nsid {nsid}" if nsid is not None else None
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
        if not text:
            continue
        if nsid_filter:
            matched = [ln for ln in text.splitlines() if nsid_filter in ln]
            if not matched:
                continue
            text = "\n".join(matched)
        hits.append(f"[{gw.node.hostname}] journal hits:\n{text}")
    return "\n".join(hits)


def _count_encrypted_block_lines(journal_text):
    if not journal_text:
        return 0
    return sum(
        1 for ln in journal_text.splitlines() if ENC_BLOCK_MARKER in ln
    )


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
    require_journal=True,
):
    """Wait for location ANA match, or confirm stuck + journal loop for repro."""
    last = None
    moved = None
    journal_seen = ""
    LOG.info(
        "Waiting up to %ss (poll every %ss) for encrypted nsid %s ANA at %s "
        "(expect_encrypted_ns_rebalance=%s, require_journal=%s)",
        timeout,
        interval,
        nsid,
        location,
        expect_move,
        require_journal,
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
                    gateways, since_seconds=timeout + 60, nsid=nsid
                )
                if journal:
                    journal_seen = journal
                    LOG.info(
                        "IBMCEPH-19516: encrypted rebalance block observed "
                        "(Jira loop marker):\n%s",
                        journal,
                    )
                    if require_journal:
                        # Stuck + journal = faithful repro PASS
                        LOG.info(
                            "IBMCEPH-19516 repro PASS: nsid %s still mismatched "
                            "and GW journal shows '%s'",
                            nsid,
                            ENC_BLOCK_MARKER,
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
            f"IBMCEPH-19516 repro expected encrypted nsid {nsid} to stay stuck "
            f"(location≠ANA / journal '{ENC_BLOCK_MARKER}'), "
            f"but it rebalanced to {location}. Build may already include the fix."
        )

    if expect_move:
        journal = _scan_journals_for_encrypted_block(
            gateways, since_seconds=timeout + 60, nsid=nsid
        )
        raise TimeoutError(
            f"Encrypted nsid {nsid} did not rebalance to {location}: {last}"
            + (f"\n{journal}" if journal else "")
        )

    # Repro timeout path
    journal = journal_seen or _scan_journals_for_encrypted_block(
        gateways, since_seconds=timeout + 60, nsid=nsid
    )
    if require_journal and not journal:
        raise AssertionError(
            f"IBMCEPH-19516 repro: nsid {nsid} still not at {location} after "
            f"{timeout}s ({last}), but GW journal did NOT show "
            f"'{ENC_BLOCK_MARKER}'. Cannot claim faithful Jira repro."
        )
    LOG.info(
        "IBMCEPH-19516 repro PASS: encrypted nsid %s still not at %s after %ss "
        "(%s)%s",
        nsid,
        location,
        timeout,
        last,
        f"\n{journal}" if journal else "",
    )
    return moved


def _assert_no_ongoing_encrypted_block(gateways, nsid, settle_sec=90):
    """After a successful move, journal must not keep looping the block."""
    LOG.info(
        "Verify settle: wait %ss then assert no new '%s' for nsid %s",
        settle_sec,
        ENC_BLOCK_MARKER,
        nsid,
    )
    time.sleep(settle_sec)
    journal = _scan_journals_for_encrypted_block(
        gateways, since_seconds=settle_sec + 5, nsid=nsid
    )
    count = _count_encrypted_block_lines(journal)
    if count:
        raise AssertionError(
            f"IBMCEPH-19516 verify: after rebalance, GW journal still logged "
            f"'{ENC_BLOCK_MARKER}' for nsid {nsid} ({count} lines in last "
            f"{settle_sec}s):\n{journal}"
        )
    LOG.info(
        "IBMCEPH-19516 verify: no ongoing encrypted-block loop for nsid %s", nsid
    )


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


def _luks_combos_from_encrypted_specs(specs):
    """Dedupe luks_combos from encrypted NS specs for KMIP registration."""
    seen = {}
    for spec in specs:
        kid = spec["key_id"]
        if kid in seen:
            continue
        seen[kid] = {
            "key_id": kid,
            "format": spec.get("format", "luks2"),
            "algo": spec.get("algo", "aes256"),
        }
    return list(seen.values())


def _resolve_encrypted_specs(config, loc_a, loc_b):
    if config.get("encrypted_namespaces"):
        return config["encrypted_namespaces"]
    if config.get("jira_topology", True):
        specs = []
        for item in DEFAULT_JIRA_ENCRYPTED:
            loc = item["location"]
            if loc == "site-A":
                loc = loc_a
            elif loc == "site-B":
                loc = loc_b
            specs.append({**item, "location": loc})
        return specs
    # Thin fallback
    combo = (config.get("luks_combos") or [{}])[0]
    return [
        {
            "location": loc_a,
            "key_id": combo.get("key_id", "key-id-19516"),
            "format": combo.get("format") or combo.get("luks_format", "luks2"),
            "algo": combo.get("algo") or combo.get("luks_algo", "aes256"),
        }
    ]


def _resolve_plain_locations(config, loc_a, loc_b):
    if config.get("plain_namespaces"):
        return config["plain_namespaces"]
    if config.get("jira_topology", True):
        out = []
        for loc in DEFAULT_JIRA_PLAIN:
            if loc == "site-A":
                out.append(loc_a)
            elif loc == "site-B":
                out.append(loc_b)
            else:
                out.append(loc)
        return out
    return [loc_a, loc_b]


def run_encrypted_ns_location_rebalance(ceph_cluster, config, nvme_service, custom_data):
    """Execute IBMCEPH-19516 scenario against the deployed NVMe service."""
    expect_move = bool(config.get("expect_encrypted_ns_rebalance", True))
    loc_a = config.get("location_site_a", "site-A")
    loc_b = config.get("location_site_b", "site-B")
    timeout = int(config.get("rebalance_timeout_sec", 900))
    interval = int(config.get("rebalance_poll_sec", 60))
    bdev_size = config.get("bdev_size", "2G")
    require_journal = bool(
        config.get("require_encrypted_block_journal", not expect_move)
    )
    settle_sec = int(config.get("verify_journal_settle_sec", 90))
    nqn = config["subsystems"][0]["nqn"]
    rbd_pool = config["rbd_pool"]
    gw_nodes = config.get("gw_nodes") or []

    if len(gw_nodes) < 2:
        raise ValueError("IBMCEPH-19516 requires at least two gw_nodes")

    enc_specs = _resolve_encrypted_specs(config, loc_a, loc_b)
    plain_locs = _resolve_plain_locations(config, loc_a, loc_b)
    # Ensure KMIP has every key referenced by encrypted specs
    config["luks_combos"] = _luks_combos_from_encrypted_specs(enc_specs)

    ha = HighAvailability(ceph_cluster, gw_nodes, **config)
    ha.gateways = nvme_service.gateways
    ha.nvme_service = nvme_service

    kmip_info, kmip_owned = acquire_kmip_for_nvmeof(
        ceph_cluster, config, custom_data, nvme_service=nvme_service
    )
    # Suite-shared KMIP (owned=False) still needs passphrase registration.
    if (not kmip_owned) and config.get("luks_combos"):
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

        LOG.info(
            "Creating Jira-style mix: %s encrypted + %s plaintext namespaces",
            len(enc_specs),
            len(plain_locs),
        )
        # Prefer a site-A encrypted NS as the change_location target (Jira:
        # site-A encrypted served by site-B ANA / move toward matching ANA).
        target_image = None
        for spec in enc_specs:
            key_id = spec.get("key_uid", spec["key_id"])
            # Prefer registered KMIP uid when present on the combo
            for combo in config["luks_combos"]:
                if combo["key_id"] == spec["key_id"] and combo.get("key_uid"):
                    key_id = combo["key_uid"]
                    break
            image = add_encrypted_namespace(
                gateway,
                nqn,
                rbd_pool,
                key_id=key_id,
                luks_format=spec.get("format", "luks2"),
                luks_algo=spec.get("algo", "aes256"),
                size=bdev_size,
                location=spec["location"],
            )
            if target_image is None and spec["location"] == loc_a:
                target_image = image

        for loc in plain_locs:
            _add_plain_namespace(gateway, nqn, rbd_pool, loc, size=bdev_size)

        namespaces = _list_namespaces(gateway, nqn)
        if not target_image:
            raise RuntimeError("No site-A encrypted namespace to change_location")
        enc_ns = _ns_by_image(namespaces, target_image)
        if not enc_ns:
            raise RuntimeError(f"Encrypted namespace for image {target_image} not listed")
        nsid = enc_ns.get("nsid")
        LOG.info(
            "Target encrypted NS baseline: nsid=%s location=%s lb_group=%s "
            "(total namespaces=%s)",
            nsid,
            enc_ns.get("location"),
            enc_ns.get("load_balancing_group"),
            len(namespaces),
        )

        nvme_gw = _nvme_gw_cli(ceph_cluster)
        pool = nvme_service.nvme_metadata_pool
        group = nvme_service.group

        # Baseline should match location before we induce mismatch
        _assert_ns_at_location(enc_ns, loc_a, nvme_gw, pool, group)

        # Induce Jira mismatch: location becomes site-B while ANA may remain
        # on site-A until rebalance moves LB group (blocked on buggy builds).
        LOG.info("Changing encrypted nsid %s location %s -> %s", nsid, loc_a, loc_b)
        gateway.namespace.change_location(
            args={"subsystem": nqn, "nsid": nsid, "location": loc_b}
        )
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
            require_journal=require_journal,
        )

        if expect_move:
            _assert_no_ongoing_encrypted_block(
                nvme_service.gateways, nsid, settle_sec=settle_sec
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
