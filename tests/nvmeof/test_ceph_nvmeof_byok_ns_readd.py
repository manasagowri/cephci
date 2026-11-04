"""Add back BYOK parent namespaces from existing encrypted RBD images.

Handles three starting states, in order of preference:

1. **Namespaces present and non-degraded** — snapshot ``format`` + ``key_id``
   from ``encryption_entries`` in ``ns list`` (no KMIP needed), delete all,
   wait for empty, then re-add from snapshot.

2. **Namespaces present but degraded / empty rbd_image_name** — the snapshot
   scan skips those; falls through to the KMIP + RBD-pool path.

3. **No namespaces at all** (``ns list`` is completely empty) — the RBD pool
   is scanned for ``byok_c*`` images.  The encryption format is decoded from
   the image name (``byok_c01_n02_luks2`` -> ``luks2``).  The ``key_id`` is
   fetched from KMIP via ``load_passphrases_all``.  The delete phase is
   skipped since there is nothing to delete.

In all cases only namespaces **missing** from ``ns list`` are added
(idempotent — safe to re-run).  The underlying RBD images are never deleted.

**add_only mode** (set ``add_only: true`` in the test config):
   Skip the snapshot-from-ns-list scan and the delete phase entirely.
   Go straight to KMIP + RBD pool scan and add only the missing namespaces.
   Use this when namespaces have already been deleted and you simply want to
   add them back against the pre-existing encrypted RBD images.
"""

import json
import re
import time

from ceph.ceph import Ceph, CommandFailed
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    _assign_kmip_endpoints,
    _existing_subsystems,
    _image_name,
    _init_rbd,
    _ns_add,
    _parent_format_and_key,
    _rbd_image_names,
    _sorted_kmip_nodes,
)
from tests.nvmeof.workflows.byok_kmip import (
    DEFAULT_KMIP_CLI_IMAGE,
    load_passphrases_all,
    short_hostname,
)
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

# Seconds to pause between the delete phase and re-add phase so the gateway
# OMAP can settle.  Overridable via config["delete_settle_sec"].
DELETE_SETTLE_SEC = 5

# Retries when checking that a subsystem is empty after bulk delete.
EMPTY_CHECK_TRIES = 12
EMPTY_CHECK_DELAY = 5

# Pattern that identifies BYOK parent images: byok_c01_n01_luks1
_BYOK_PARENT_RE = re.compile(r"^byok_c\d+_n\d+_(luks1|luks2)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_namespaces(gateway, nqn):
    """Return the raw namespace list for *nqn*."""
    out, _ = gateway.namespace.list(
        **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
    )
    return json.loads(out).get("namespaces", []) if out else []


def _snapshot_ns_metadata(gateway, subsystems, config):
    """Walk every subsystem and record namespace metadata from ``ns list``.

    Returns a list of dicts with keys:
      ``nqn``, ``nsid``, ``image``, ``pool``, ``format``, ``key_id``

    Only namespaces whose ``encryption_entries`` carry a non-empty
    ``format`` and ``key_id`` AND whose ``rbd_image_name`` is non-empty
    are included.  Degraded namespaces with an empty image name are
    skipped — the fallback path handles those via KMIP + RBD pool scan.
    """
    pool = config.get("rbd_pool", "rbd")
    snapshot = []
    skipped = 0
    for sub in subsystems:
        nqn = sub["group_nqn"]
        listed = _list_namespaces(gateway, nqn)
        if not listed:
            LOG.warning("_snapshot: no namespaces on %s", nqn)
            continue
        for ns in listed:
            image = (ns.get("rbd_image_name") or "").strip()
            if not image:
                skipped += 1
                continue
            entries = ns.get("encryption_entries") or []
            if not entries:
                skipped += 1
                continue
            entry = entries[0]
            enc_fmt = (str(entry.get("format") or "")).strip()
            key_id = (str(entry.get("key_id") or "")).strip()
            if not enc_fmt or not key_id:
                skipped += 1
                continue
            snapshot.append(
                {
                    "nqn": nqn,
                    "nsid": ns["nsid"],
                    "image": image,
                    "pool": ns.get("rbd_pool") or pool,
                    "format": enc_fmt,
                    "key_id": key_id,
                }
            )
    LOG.info("Snapshot: %s namespaces captured, %s skipped", len(snapshot), skipped)
    return snapshot


def _snapshot_from_kmip(gateway, subsystems, config, ceph_cluster, kwargs=None):
    """Reconstruct the namespace list from RBD pool + KMIP.

    Used both as a fallback when ``ns list`` shows only degraded namespaces
    with empty ``rbd_image_name`` AND as the primary path when ``add_only``
    mode is active (namespaces already deleted, RBD images still present).

    The image name encodes the format: ``byok_c{sub:02d}_n{ns:02d}_{fmt}``.
    The ``key_id`` comes from ``load_passphrases_all`` on the KMIP nodes —
    this requires KMIP to be running.

    *kwargs* is the full test kwargs dict; when provided it is passed to
    ``_init_rbd`` so that the RBD helper gets proper cluster credentials.
    """
    pool = config.get("rbd_pool", "rbd")
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    kmip_cli_image = config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE)
    subsystems_per_kmip = int(config.get("subsystems_per_kmip", 2))

    rbd_obj = _init_rbd(kwargs if kwargs is not None else {"config": config})
    existing_images = _rbd_image_names(rbd_obj, pool)
    byok_images = {img for img in existing_images if _BYOK_PARENT_RE.match(img)}
    LOG.info("Fallback: found %s byok_c* images in pool %s", len(byok_images), pool)

    kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
    LOG.info(
        "Fallback: loading passphrases from %s KMIP nodes: %s",
        len(kmip_nodes),
        [short_hostname(n) for n in kmip_nodes],
    )
    passphrases_by_node = load_passphrases_all(kmip_nodes, cli_image=kmip_cli_image)
    _assign_kmip_endpoints(subsystems, kmip_nodes, subsystems_per_kmip)

    snapshot = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        from tests.nvmeof.test_ceph_nvmeof_byok import _keys_for_node
        keys = _keys_for_node(passphrases_by_node, sub["kmip_node"])
        for ns_index in range(1, ns_per_sub + 1):
            fmt, key = _parent_format_and_key(ns_index, ns_per_sub, keys)
            image = _image_name(sub["num"], ns_index, fmt)
            if image not in byok_images:
                LOG.warning("Fallback: expected image %s not in RBD pool; skipping", image)
                continue
            snapshot.append(
                {
                    "nqn": nqn,
                    "image": image,
                    "pool": pool,
                    "format": fmt,
                    "key_id": key["uuid"],
                }
            )
    LOG.info("Fallback snapshot: %s namespaces reconstructed from KMIP", len(snapshot))
    return snapshot


def _delete_all_namespaces(gateway, subsystems):
    """Delete every namespace from every subsystem using ``ns del --force``.

    Namespaces that are already absent are silently skipped.
    Returns a mapping ``{nqn: [nsid, ...]}`` of what was submitted for deletion.
    """
    deleted = {}
    for sub in subsystems:
        nqn = sub["group_nqn"]
        listed = _list_namespaces(gateway, nqn)
        if not listed:
            LOG.info("%s: no namespaces to delete", nqn)
            deleted[nqn] = []
            continue
        nsids = [ns["nsid"] for ns in listed if ns.get("nsid") is not None]
        LOG.info("%s: deleting %s namespaces nsids=%s", nqn, len(nsids), nsids)

        def _del_one(nqn=nqn, nsid=None):
            try:
                gateway.namespace.delete(
                    **{"args": {"nqn": nqn, "nsid": nsid, "force": True}}
                )
                LOG.info("Deleted nsid=%s on %s", nsid, nqn)
            except CommandFailed as exc:
                text = str(exc).lower()
                if "not found" in text or "no such" in text:
                    LOG.warning("nsid=%s on %s already gone: %s", nsid, nqn, exc)
                    return
                raise

        with parallel() as p:
            for nsid in nsids:
                p.spawn(_del_one, nqn, nsid)
            for _ in p:
                pass
        deleted[nqn] = nsids
    return deleted


def _verify_subsystems_empty(gateway, subsystems):
    """Assert every subsystem lists zero namespaces after bulk deletion.

    Retries up to EMPTY_CHECK_TRIES times to allow gateway OMAP propagation.
    """
    for sub in subsystems:
        nqn = sub["group_nqn"]
        remaining = None
        for attempt in range(1, EMPTY_CHECK_TRIES + 1):
            remaining = _list_namespaces(gateway, nqn)
            if not remaining:
                break
            LOG.warning(
                "%s: %s namespaces still listed (attempt %s/%s)",
                nqn,
                len(remaining),
                attempt,
                EMPTY_CHECK_TRIES,
            )
            time.sleep(EMPTY_CHECK_DELAY)
        if remaining:
            raise RuntimeError(
                f"{nqn}: {len(remaining)} namespaces remain after deletion: "
                f"{[ns.get('nsid') for ns in remaining]}"
            )
        LOG.info("%s: confirmed empty", nqn)


def _readd_namespaces(gateway, snapshot):
    """Re-add only the namespaces from *snapshot* that are not already listed.

    Skips any image that is already present and non-degraded in ``ns list``
    so the function is safe to call after a partial failure.
    ``rbd-create-image`` is intentionally omitted — the image already exists.
    """
    # Build a per-NQN set of images already listed and non-degraded.
    already_present: dict = {}
    nqns = {rec["nqn"] for rec in snapshot}
    for nqn in nqns:
        listed = _list_namespaces(gateway, nqn)
        already_present[nqn] = {
            (ns.get("rbd_image_name") or "").strip()
            for ns in listed
            if (ns.get("rbd_image_name") or "").strip()
            and ns.get("degraded") not in (True, "true", "True", 1, "yes")
        }

    to_add = [
        rec for rec in snapshot
        if rec["image"] not in already_present.get(rec["nqn"], set())
    ]
    skipped = len(snapshot) - len(to_add)
    if skipped:
        LOG.info("Re-add: %s namespaces already present and non-degraded; skipping", skipped)
    if not to_add:
        LOG.info("Re-add: nothing to do — all %s namespaces already present", len(snapshot))
        return

    LOG.info("Re-add: adding %s namespaces", len(to_add))

    def _add_one(rec):
        LOG.info(
            "ns add %s image=%s format=%s key-id=%s",
            rec["nqn"], rec["image"], rec["format"], rec["key_id"],
        )
        _ns_add(
            gateway,
            {
                "nqn": rec["nqn"],
                "rbd_pool": rec["pool"],
                "rbd_image_name": rec["image"],
                "encryption-format": rec["format"],
                "key-id": rec["key_id"],
            },
        )

    sub_errors = []
    with parallel() as p:
        for rec in to_add:
            p.spawn(_add_one, rec)
        for result in p:
            if isinstance(result, Exception):
                sub_errors.append(result)

    if sub_errors:
        raise RuntimeError(
            f"{len(sub_errors)} namespace re-add failures: "
            + "; ".join(str(e) for e in sub_errors[:5])
        )
    LOG.info("Re-add complete: %s namespaces added", len(to_add))


def _verify_readded_namespaces(gateway, snapshot):
    """Assert every snapshotted image is back and non-degraded after re-add."""
    by_nqn: dict = {}
    for rec in snapshot:
        by_nqn.setdefault(rec["nqn"], []).append(rec)

    failures = []
    total_ok = 0
    for nqn, records in by_nqn.items():
        listed = _list_namespaces(gateway, nqn)
        by_image = {ns.get("rbd_image_name"): ns for ns in listed if ns.get("rbd_image_name")}
        for rec in records:
            image = rec["image"]
            ns = by_image.get(image)
            if ns is None:
                failures.append(f"{nqn}: {image} not listed after re-add")
                continue
            if ns.get("degraded") in (True, "true", "True", 1, "yes"):
                failures.append(f"{nqn}: {image} nsid={ns.get('nsid')} degraded after re-add")
                continue
            total_ok += 1
        LOG.info(
            "%s: %s/%s namespaces verified OK",
            nqn,
            len([r for r in records if r["image"] in by_image]),
            len(records),
        )

    if failures:
        raise RuntimeError(
            f"{len(failures)} namespace verification failures:\n"
            + "\n".join(failures[:20])
        )
    LOG.info("All %s re-added namespaces verified non-degraded", total_ok)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _any_namespaces_listed(gateway, subsystems):
    """Return True if at least one namespace (any state) exists across subsystems."""
    for sub in subsystems:
        if _list_namespaces(gateway, sub["group_nqn"]):
            return True
    return False


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Add back BYOK parent namespaces from existing encrypted RBD images.

    Normal mode — resolves the snapshot in this order:
      1. ``ns list`` encryption_entries  (no KMIP, preferred)
      2. RBD pool scan + KMIP passphrases (when ns list is empty or all degraded)

    The delete phase only runs when namespaces are actually present (state 1/2).
    When ns list is already empty (state 3) the delete phase is skipped and
    re-add goes straight to adding missing namespaces from the RBD pool.

    **add_only mode** (``config.add_only: true``):
      Skip snapshot-from-ns-list and the delete phase completely.  Go straight
      to KMIP + RBD pool scan and add only the namespaces that are missing.
      Use this when the namespaces have already been deleted outside of this
      test and you just want to add them back from the pre-existing encrypted
      RBD images.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    add_only = bool(config.get("add_only", False))
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    try:
        _init_rbd(kwargs)
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        gateway = nvme_service.gateways[0]

        subsystems = _existing_subsystems(gateway, config)

        # ------------------------------------------------------------------
        # Phase 0: build the snapshot
        # ------------------------------------------------------------------
        if add_only:
            # add_only: namespaces are already deleted — scan RBD pool + KMIP
            # directly without touching ns list or running a delete phase.
            LOG.info(
                "Phase 0 (add_only): scanning RBD pool + KMIP for existing "
                "encrypted images (delete phase will be skipped)"
            )
            snapshot = _snapshot_from_kmip(
                gateway, subsystems, config, ceph_cluster, kwargs=kwargs
            )
            if not snapshot:
                raise RuntimeError(
                    "add_only: no byok_c* images found in RBD pool. "
                    "Nothing to add."
                )
            LOG.info(
                "Phase 0 (add_only): %s namespaces reconstructed from "
                "KMIP + RBD pool",
                len(snapshot),
            )
        else:
            LOG.info("Phase 0: checking ns list for existing namespace metadata")
            snapshot = _snapshot_ns_metadata(gateway, subsystems, config)

            if snapshot:
                LOG.info(
                    "Phase 0: snapshot from ns list — %s namespaces captured",
                    len(snapshot),
                )
            else:
                # ns list is empty OR all namespaces are degraded/no-image.
                # Either way we can't get key IDs from it — use KMIP + RBD pool.
                LOG.warning(
                    "Phase 0: ns list has no usable encrypted namespaces. "
                    "Reconstructing from RBD pool + KMIP."
                )
                snapshot = _snapshot_from_kmip(
                    gateway, subsystems, config, ceph_cluster, kwargs=kwargs
                )
                if not snapshot:
                    raise RuntimeError(
                        "No byok_c* images found in RBD pool and ns list is empty. "
                        "Nothing to add."
                    )
                LOG.info(
                    "Phase 0: snapshot from KMIP + RBD pool — %s namespaces",
                    len(snapshot),
                )

        # ------------------------------------------------------------------
        # Phase 1: delete (skipped in add_only mode or when ns list is empty)
        # ------------------------------------------------------------------
        if add_only:
            LOG.info(
                "Phase 1: skipped (add_only mode) — "
                "going straight to ns add"
            )
        elif _any_namespaces_listed(gateway, subsystems):
            LOG.info(
                "Phase 1: deleting all existing namespaces via ns del "
                "(RBD images preserved)"
            )
            _delete_all_namespaces(gateway, subsystems)
            _verify_subsystems_empty(gateway, subsystems)
            LOG.info("Phase 1 complete: all namespaces deleted")

            settle = int(config.get("delete_settle_sec", DELETE_SETTLE_SEC))
            if settle > 0:
                LOG.info("Settling %ss before re-add", settle)
                time.sleep(settle)
        else:
            LOG.info(
                "Phase 1: skipped — ns list is already empty, "
                "going straight to re-add"
            )

        # ------------------------------------------------------------------
        # Phase 2: add back only what is missing
        # ------------------------------------------------------------------
        LOG.info(
            "Phase 2: adding %s namespaces from existing RBD images "
            "(skipping any already present)",
            len(snapshot),
        )
        _readd_namespaces(gateway, snapshot)

        # ------------------------------------------------------------------
        # Phase 3: verify
        # ------------------------------------------------------------------
        LOG.info("Phase 3: verifying %s namespaces are non-degraded", len(snapshot))
        _verify_readded_namespaces(gateway, snapshot)

        LOG.info(
            "BYOK ns add-back test passed: %s namespaces added successfully",
            len(snapshot),
        )
        return 0
    except Exception as err:
        LOG.exception("BYOK ns add-back test failed: %s", err)
        return 1
