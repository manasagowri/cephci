from copy import deepcopy

from ceph.ceph import Ceph
from ceph.ceph_admin import CephAdmin
from ceph.ceph_admin.common import fetch_method
from ceph.ceph_admin.orch import Orch
from ceph.utils import get_node_by_id
from tests.nvmeof.workflows.gateway_entities import (
    manage_hosts,
    manage_listeners,
    manage_namespaces,
    manage_subsystems,
)
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import check_and_set_nvme_cli_image
from tests.rbd.rbd_utils import initial_rbd_config
from utility.log import Log

LOG = Log(__name__)


def execute_and_log_results(node, rbd_pool):
    commands = [
        "cephadm shell -- ceph df",
        f"cephadm shell -- rbd du -p {rbd_pool}",
        "cephadm shell -- ceph -s",
        "cephadm shell -- ceph health detail",
        "cephadm shell -- ceph orch ls | grep nvmeof",
        "cat /proc/meminfo | grep -iE 'hugepage|^mem|swap'",
        "cat /proc/cpuinfo | grep -iE 'cpu'",
        "ps -eo pid,ppid,cmd,comm,%mem,%cpu --sort=-%mem | head -20",
        "top -b | head -n 20",
    ]
    for cmd in commands:
        output, _ = node.installer.exec_command(cmd, sudo=True)
        LOG.info(output)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    LOG.info("Configure Ceph NVMeoF entities at scale over CLI.")
    config = deepcopy(kwargs["config"])
    node = get_node_by_id(ceph_cluster, config["node"])
    rbd_pool = config.get("rbd_pool", "rbd_pool")
    kwargs["config"].update(
        {
            "do_not_create_image": True,
            "rep-pool-only": True,
            "rep_pool_config": {"pool": rbd_pool},
        }
    )
    rbd_obj = initial_rbd_config(**kwargs)["rbd_reppool"]

    ceph = Orch(ceph_cluster, **{})
    nvmegwcli = None
    overrides = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=overrides)

    try:
        nvme_service = NVMeService(config, ceph_cluster)

        if config.get("install"):
            nvme_service.deploy()

        nvme_service.init_gateways()
        nvmegwcli = nvme_service.gateways[0]

        steps = config.get("steps", [])
        init_config = config.get("initiators")
        io = config.get("run_io")

        for step in steps:
            cfg = step["config"]
            service = cfg.pop("service")
            command = cfg.pop("command")

            _cls = fetch_method(nvmegwcli, service)

            if service == "subsystem":
                manage_subsystems(cfg, _cls, command)
            elif service == "listener":
                manage_listeners(cfg, _cls, command, ceph_cluster)
            elif service == "host":
                manage_hosts(cfg, _cls, command)
            elif service == "namespace":
                manage_namespaces(
                    cfg, _cls, command, ceph_cluster, node, init_config, rbd_obj, io
                )
            else:
                func = fetch_method(_cls, command)

                if "args" not in cfg:
                    cfg["args"] = {}

                func(**cfg["args"])

        instance = CephAdmin(cluster=ceph_cluster, **config)
        execute_and_log_results(instance, rbd_pool)

    except BaseException as be:
        LOG.error(be, exc_info=True)
        instance = CephAdmin(cluster=ceph_cluster, **config)
        execute_and_log_results(instance, rbd_pool)
        mem_usage, _ = node.exec_command(
            cmd="ps -eo pid,ppid,cmd,comm,%mem,%cpu --sort=-%mem | head -20",
            sudo=True,
        )
        LOG.info(mem_usage)
        top_usage, _ = node.exec_command(cmd="top -b | head -n 20", sudo=True)
        LOG.info(top_usage)

        return 1

    return 0
