"""NVMeoF BYOK feature tests: FIO fill, masking, DHCHAP, HA, optional sample IO.

Assumes ``test_ceph_nvmeof_byok`` has already created subsystems, KMIP
endpoints, encrypted parent namespaces, and clones.
"""

from ceph.ceph import Ceph
from ceph.parallel import parallel
from tests.nvmeof.test_ceph_nvmeof_byok import (
    DEFAULT_LISTENER_PORT,
    _assign_kmip_endpoints,
    _existing_subsystems,
    _sorted_kmip_nodes,
)
from tests.nvmeof.workflows.byok_feature_tests import run_feature_tests
from tests.nvmeof.workflows.byok_kmip import (
    DEFAULT_KMIP_CLI_IMAGE,
    load_passphrases_all,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from utility.log import Log
from utility.utils import run_fio

LOG = Log(__name__)


def _allow_any_host(gateway, subsystems):
    for sub in subsystems:
        gateway.host.add(
            **{"args": {"subsystem": sub["group_nqn"], "host": repr("*")}}
        )


def _run_sample_io(ceph_cluster, gateway, config):
    """Connect from the first client and FIO a sample of devices."""
    clients = ceph_cluster.get_nodes(role="client")
    if not clients:
        raise ValueError("run_io requires a client node")
    client = clients[0]
    initiator = NVMeInitiator(client)
    initiator.disconnect_all()
    initiator.connect_targets(
        gateway,
        {
            "nqn": "connect-all",
            "listener_port": config.get("listener_port", DEFAULT_LISTENER_PORT),
        },
    )
    paths = initiator.list_devices()
    if not paths:
        raise RuntimeError(f"No NVMe devices on {client.hostname}")
    sample = int(config.get("io_sample", 6))
    targets = paths[:sample]
    LOG.info(f"Running sample FIO on {len(targets)} of {len(paths)} devices")
    io_args = config.get("io_args", {"size": "100M", "runtime": 10})
    with parallel() as p:
        for path in targets:
            p.spawn(
                run_fio,
                **{
                    **io_args,
                    "device_name": path,
                    "client_node": client,
                    "long_running": True,
                    "cmd_timeout": "notimeout",
                },
            )
        for op in p:
            if isinstance(op, int) and op != 0:
                raise RuntimeError(f"FIO failed with exit code: {op}")
    initiator.disconnect_all()


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Run BYOK feature tests against an already-configured gateway.

    Returns 0 on success, 1 on failure.
    """
    config = kwargs["config"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    try:
        nvme_service = NVMeService(config, ceph_cluster)
        nvme_service.init_gateways()
        gateway = nvme_service.gateways[0]

        kmip_nodes = _sorted_kmip_nodes(ceph_cluster, config)
        subsystems = _existing_subsystems(gateway, config)
        _assign_kmip_endpoints(
            subsystems,
            kmip_nodes,
            int(config.get("subsystems_per_kmip", 2)),
        )
        passphrases_by_node = load_passphrases_all(
            kmip_nodes,
            cli_image=config.get("kmip_cli_image", DEFAULT_KMIP_CLI_IMAGE),
        )

        run_feature_tests(
            nvme_service,
            gateway,
            subsystems,
            ceph_cluster,
            config,
            passphrases_by_node=passphrases_by_node,
        )

        if config.get("run_io"):
            _allow_any_host(gateway, subsystems)
            _run_sample_io(ceph_cluster, gateway, config)
        return 0
    except Exception as err:
        LOG.exception("NVMeoF BYOK feature tests failed: %s", err)
        return 1
