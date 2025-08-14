from copy import deepcopy

from ceph.ceph import Ceph
from ceph.nvmegw_cli import NVMeGWCLI
from ceph.nvmeof.initiator import Initiator
from ceph.utils import get_node_by_id
from tests.nvmeof.workflows.ns_masking import (
    add_host,
    add_namespaces,
    change_visibility,
    del_host,
)
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.rbd.rbd_utils import initial_rbd_config
from utility.log import Log

LOG = Log(__name__)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    LOG.info("Add ns and Configure NS masking")
    config = deepcopy(kwargs["config"])
    rbd_pool = config.get("rbd_pool", "rbd_pool")

    kwargs["config"].update(
        {
            "do_not_create_image": True,
            "rep-pool-only": True,
            "rep_pool_config": {"pool": rbd_pool},
        }
    )
    rbd_obj = initial_rbd_config(**kwargs)["rbd_reppool"]

    overrides = kwargs.get("test_data", {}).get("custom-config")
    for key, value in dict(item.split("=") for item in overrides).items():
        if key == "nvmeof_cli_image":
            NVMeGWCLI.NVMEOF_CLI_IMAGE = value
            break

    io_tasks = []
    executor = None
    try:
        steps = config.get("steps", [])
        init_nodes = config.get("initiators")
        hostnqn_dict = {}
        LOG.info(init_nodes)
        # ha = HighAvailability(ceph_cluster, config["nodes"], **config)
        nvme_service = NVMeService(config, ceph_cluster)

        for node in init_nodes:
            initiator_node = get_node_by_id(ceph_cluster, node)
            initiator = Initiator(initiator_node)
            initiator.disconnect_all()
            hostnqn, _ = initiator_node.exec_command(
                cmd="cat /etc/nvme/hostnqn",
                sudo=True,
            )
            hostnqn = hostnqn.strip()
            hostnqn_dict[node] = hostnqn
        LOG.info("Hostnqn's are: %s", hostnqn_dict)

        for step in steps:
            cfg = step["config"]
            command = cfg.pop("command")

            if command == "add":
                executor, io_tasks = add_namespaces(
                    cfg, command, init_nodes, hostnqn_dict, rbd_obj, nvme_service
                )
            if command == "add_host":
                executor, io_tasks = add_host(
                    cfg, command, init_nodes, hostnqn_dict, nvme_service
                )
            if command == "del_host":
                executor, io_tasks = del_host(
                    cfg, command, init_nodes, hostnqn_dict, nvme_service
                )
            if command == "change_visibility":
                executor, io_tasks = change_visibility(
                    cfg, command, init_nodes, hostnqn_dict, nvme_service
                )
        return 0
    except BaseException as be:
        LOG.error(be, exc_info=True)
        return 1
    finally:
        if io_tasks and executor:
            LOG.info("Waiting for completion of IOs.")
            executor.shutdown(wait=True, cancel_futures=True)
