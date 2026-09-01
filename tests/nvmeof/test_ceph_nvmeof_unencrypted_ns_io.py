"""Add 512 unencrypted namespaces on existing BYOK subsystems and run FIO.

Reuses the group2 subsystem list. Creates ``namespaces_per_subsystem``
plain RBD namespaces per NQN (default 16 x 32 = 512) without KMIP/LUKS,
masks them across clients, then runs time-based FIO on those devices only.

FIO uses one job per client (all devices in ``filename=``) so ``iodepth``
is outstanding IOs for that client, not per namespace. Set
``resume_from: io`` to skip ns add and masking and only reconnect + FIO
existing ``plain_c*`` namespaces.
"""

import json
import shlex
import time

from ceph.ceph import Ceph
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _existing_subsystems,
    _init_rbd,
    _listed_ns_map,
    _ns_add,
)
from tests.nvmeof.test_ceph_nvmeof_byok_clone_io import (
    _check_gateways,
    _snapshot_gateways,
    _watch_gateways,
)
from tests.nvmeof.test_ceph_nvmeof_byok_continuous_io import (
    _apply_ns_mask,
    _configure_subsystem_hosts,
    _connect_client,
    _norm_uuid,
    _ns_record,
    _paths_for_uuids,
    _run_fio_job,
)
from tests.nvmeof.workflows.byok_kmip import copy_certs_into_gateway_containers
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log

LOG = Log(__name__)

PLAIN_JOB = "/tmp/plain_ns_io.fio"
DEVICE_WAIT_TRIES = 12
DEVICE_WAIT_DELAY = 10


def _plain_image(sub_num, ns_index):
    return f"plain_c{sub_num:02d}_n{ns_index:02d}"


def _discover_plain_namespaces(gateway, subsystems, config):
    """Record ``plain_c*`` images already present; do not create any."""
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    found = []
    for sub in subsystems:
        ns_map = _listed_ns_map(gateway, sub["group_nqn"])
        images = []
        for ns_index in range(1, ns_per_sub + 1):
            image = _plain_image(sub["num"], ns_index)
            if image in ns_map:
                images.append(image)
        if len(images) != ns_per_sub:
            raise RuntimeError(
                f"{sub['group_nqn']}: expected {ns_per_sub} plain namespaces, "
                f"found {len(images)} ({images})"
            )
        sub["plain"] = sorted(images)
        found.extend(sub["plain"])
        LOG.info(
            "%s: reused %s existing unencrypted namespaces",
            sub["group_nqn"],
            len(sub["plain"]),
        )
    LOG.info("resume_from=io: %s existing plain namespaces", len(found))
    return found


def _add_plain_namespaces(gateway, subsystems, config):
    """Create unencrypted namespaces; skip images already on the subsystem."""
    pool = config.get("rbd_pool", "rbd")
    ns_per_sub = int(config.get("namespaces_per_subsystem", 16))
    size = config.get("image_size", "5G")
    created = []

    def _add_one(sub, ns_index, ns_map):
        image = _plain_image(sub["num"], ns_index)
        nqn = sub["group_nqn"]
        if image in ns_map:
            LOG.info("ns %s already on %s; skipping add", image, nqn)
            return image
        LOG.info("ns add plain %s image=%s size=%s", nqn, image, size)
        _ns_add(
            gateway,
            {
                "nqn": nqn,
                "rbd_pool": pool,
                "rbd_image_name": image,
                "size": size,
                "rbd-create-image": True,
            },
        )
        return image

    for sub in subsystems:
        ns_map = _listed_ns_map(gateway, sub["group_nqn"])
        images = []
        with parallel() as p:
            for ns_index in range(1, ns_per_sub + 1):
                p.spawn(_add_one, sub, ns_index, ns_map)
            for image in p:
                images.append(image)
        sub["plain"] = sorted(images)
        created.extend(sub["plain"])
        LOG.info(
            "%s: %s unencrypted namespaces present",
            sub["group_nqn"],
            len(sub["plain"]),
        )
    expected = len(subsystems) * ns_per_sub
    if len(created) != expected:
        raise RuntimeError(
            f"Expected {expected} unencrypted namespaces, have {len(created)}"
        )
    return created


def _plain_ns_records(gateway, subsystems, clients, host_nqns):
    """Build masking records for the unencrypted images only."""
    if not clients:
        raise ValueError("unencrypted NS IO requires a client node")
    records = []
    for sub in subsystems:
        nqn = sub["group_nqn"]
        images = list(sub.get("plain") or [])
        if not images:
            raise RuntimeError(f"No unencrypted images recorded for {nqn}")
        listed = {}
        out, _ = gateway.namespace.list(
            **{"base_cmd_args": {"format": "json"}, "args": {"subsystem": nqn}}
        )
        for ns in json.loads(out).get("namespaces", []) if out else []:
            name = ns.get("rbd_image_name")
            if name in images:
                listed[name] = ns
        for image in images:
            ns = listed.get(image)
            if not ns:
                raise RuntimeError(f"Unencrypted image {image} not listed on {nqn}")
            records.append(_ns_record(nqn, ns))
    for index, record in enumerate(records):
        client = clients[index % len(clients)]
        record["owner"] = client.hostname
        record["owner_nqn"] = host_nqns[client.hostname]
    assigned = {client.hostname: [] for client in clients}
    for record in records:
        assigned[record["owner"]].append(record)
    return records, assigned


def _wait_for_paths(initiator, uuids, hostname):
    last_paths = []
    for attempt in range(1, DEVICE_WAIT_TRIES + 1):
        last_paths = _paths_for_uuids(initiator, uuids)
        LOG.info(
            "%s unencrypted devices visible=%s expected=%s (attempt %s/%s)",
            hostname,
            len(last_paths),
            len(uuids),
            attempt,
            DEVICE_WAIT_TRIES,
        )
        if len(last_paths) >= len(uuids):
            return last_paths
        time.sleep(DEVICE_WAIT_DELAY)
    raise RuntimeError(
        f"{hostname}: expected {len(uuids)} unencrypted devices, found {len(last_paths)}"
    )


def _write_light_fio_job(node, job_path, devices, global_opts):
    """One FIO section for all devices so iodepth is per client, not per NS."""
    script = (
        "from pathlib import Path\n"
        f"devices = {json.dumps(list(devices))}\n"
        f"opts = {json.dumps(global_opts)}\n"
        "lines = ['[global]']\n"
        "for key, value in opts.items():\n"
        "    lines.append(f'{key}={value}')\n"
        "lines.append('[plain]')\n"
        "lines.append('filename=' + ':'.join(devices))\n"
        f"Path({json.dumps(job_path)}).write_text('\\n'.join(lines) + '\\n')\n"
    )
    node.exec_command(cmd=f"python3 -c {shlex.quote(script)}", sudo=True)


def _run_plain_io(mapped, config, gateways, before):
    io_runtime = int(config.get("io_runtime", 600))
    io_type = config.get("io_type", "randrw")
    bs = config.get("bs", "64k")
    iodepth = str(config.get("iodepth", 4))
    interval = int(config.get("gw_poll_interval", 15))
    total = sum(len(paths) for _, _, paths in mapped.values())
    LOG.info(
        "FIO %s on %s unencrypted devices for %ss (bs=%s iodepth=%s, one job/client)",
        io_type,
        total,
        io_runtime,
        bs,
        iodepth,
    )
    for client, _, paths in mapped.values():
        if not paths:
            raise RuntimeError(f"No unencrypted devices on {client.hostname}")
        _write_light_fio_job(
            client,
            PLAIN_JOB,
            paths,
            {
                "ioengine": "libaio",
                "direct": "1",
                "bs": bs,
                "rw": io_type,
                "iodepth": iodepth,
                "group_reporting": "1",
                "time_based": "1",
                "runtime": str(io_runtime),
                "numjobs": "1",
            },
        )
    errors = []
    with parallel() as p:
        for client, _, _ in mapped.values():
            p.spawn(_run_fio_job, client, PLAIN_JOB)
        p.spawn(
            _watch_gateways,
            gateways,
            before,
            io_runtime + 30,
            interval,
            "during unencrypted IO",
        )
        for result in p:
            if isinstance(result, int) and result != 0:
                errors.append(result)
    if errors:
        raise RuntimeError(f"FIO {PLAIN_JOB} failed with {errors}")


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Create unencrypted namespaces on existing subsystems and FIO them.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)
    started = time.time()

    try:
        _init_rbd(kwargs)
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
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
            LOG.info("NVMeoF spec updated; waiting for gateways and recopying KMIP certs")
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
            raise ValueError("unencrypted NS IO requires a client node")

        subsystems = _existing_subsystems(gateway, config)
        resume_from = config.get("resume_from")
        resume_io = resume_from == "io"

        before = _snapshot_gateways(gateways)
        if resume_io:
            LOG.info("resume_from=io: reuse existing plain namespaces; skip ns add and masking")
            _discover_plain_namespaces(gateway, subsystems, config)
        else:
            _add_plain_namespaces(gateway, subsystems, config)
            _check_gateways(
                gateways,
                before,
                "after unencrypted ns add",
                since_seconds=time.time() - started,
            )

        host_nqns = {}
        initiators = {}
        for client in clients:
            initiator = NVMeInitiator(client)
            initiator.disconnect_all()
            initiators[client.hostname] = initiator
            host_nqns[client.hostname] = initiator.initiator_nqn()
            LOG.info("Client %s host NQN %s", client.hostname, host_nqns[client.hostname])

        _configure_subsystem_hosts(gateway, subsystems, host_nqns)
        records, assigned = _plain_ns_records(
            gateway, subsystems, clients, host_nqns
        )
        if resume_io:
            LOG.info(
                "resume_from=io: skipping mask of %s namespaces already assigned",
                len(records),
            )
        else:
            LOG.info(
                "Masking %s unencrypted namespaces across %s clients",
                len(records),
                len(clients),
            )
            with parallel(max_workers=2) as p:
                for ns in records:
                    p.spawn(_apply_ns_mask, gateway, ns)
                for _ in p:
                    pass

        port = config.get("listener_port", DEFAULT_LISTENER_PORT)
        mapped = {}
        for client in clients:
            initiator = initiators[client.hostname]
            _connect_client(initiator, gateways, subsystems, port)
            time.sleep(5)
            uuids = [_norm_uuid(ns["uuid"]) for ns in assigned[client.hostname]]
            paths = _wait_for_paths(initiator, uuids, client.hostname)
            mapped[client.hostname] = (client, initiator, paths)
            LOG.info(
                "%s connected with %s unencrypted namespaces",
                client.hostname,
                len(paths),
            )

        _run_plain_io(mapped, config, gateways, before)
        _check_gateways(
            gateways,
            before,
            "after unencrypted NS IO",
            since_seconds=time.time() - started,
        )
        LOG.info(
            "Unencrypted NS IO passed: %s namespaces, gateways stayed up",
            len(records),
        )
        return 0
    except Exception as err:
        LOG.exception("NVMeoF unencrypted NS IO test failed: %s", err)
        return 1
    finally:
        try:
            for node in ceph_cluster.get_nodes(role="client"):
                NVMeInitiator(node).disconnect_all()
        except Exception as exc:
            LOG.warning("Initiator disconnect during cleanup: %s", exc)
