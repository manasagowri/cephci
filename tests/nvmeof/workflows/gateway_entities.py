import json
from copy import deepcopy

from ceph.ceph_admin.orch import Orch
from ceph.nvmegw_cli import NVMeGWCLI
from ceph.nvmeof.initiator import Initiator
from ceph.parallel import parallel
from ceph.utils import get_node_by_id
from utility.systemctl import SystemCtl
from utility.utils import generate_unique_id


def configure_subsystem(nvme_service, sub_cfg):
    """
    Configure a single subsystem based on the provided configuration.
    Args:
        nvme_service: NvmeService instance
        sub_cfg: Subsystem configuration dictionary
    """
    nqn = sub_cfg.get("nqn") or sub_cfg.get("subnqn")
    if not nqn:
        raise ValueError("Subsystem NQN not provided in subsystem_config")

    # Use the first gateway's nvmegwcli for subsystem configuration
    if not nvme_service.gateways:
        raise ValueError("No gateways available for subsystem configuration")

    nvmegwcli = nvme_service.gateways[0].nvmegwcli

    # Configure subsystem using nvmegwcli
    sub_args = {"subsystem": nqn}

    # Add Subsystem
    nvmegwcli.subsystem.add(
        **{
            "args": {
                **sub_args,
                **{
                    "max-namespaces": sub_cfg.get("max_ns", 32),
                    "enable-ha": sub_cfg.get("enable_ha", False),
                    "no-group-append": sub_cfg.get("no-group-append", True),
                },
            }
        }
    )


def configure_subsystems(nvme_service, exec_parallel=False):
    """
    Configure subsystems, hosts, and namespaces for this gateway group.
    This is done once per group, not per gateway.
    Args:
        subsystem_config: Configuration for the subsystems
        pool: RBD pool name (optional, will use self.pool_name or default if not provided)
    """
    # Configure subsystem
    subsystem_config = nvme_service.config.get("subsystems", [])
    for sub_cfg in subsystem_config:
        if exec_parallel:
            with parallel() as p:
                p.spawn(configure_subsystem, nvme_service, sub_cfg)
        else:
            configure_subsystem(nvme_service, sub_cfg)


def configure_hosts(gateway, config: dict):
    """
    Configure hosts for this specific gateway.
    This is called per gateway since each gateway needs its own hosts.
    Args:
        config: Test configuration.
    """
    # Configure hosts if specified
    subsystem_config = config.get("subsystems", [])
    for sub_cfg in subsystem_config:
        # Configure hosts if specified
        nqn = sub_cfg.get("nqn") or sub_cfg.get("subnqn")
        sub_args = {"subsystem": nqn}
        if sub_cfg.get("allow_host"):
            gateway.nvmegwcli.host.add(
                **{"args": {**sub_args, **{"host": repr(sub_cfg["allow_host"])}}}
            )
        if sub_cfg.get("hosts"):
            hosts = sub_cfg["hosts"]
            if not isinstance(hosts, list):
                hosts = [hosts]

            for host in hosts:
                node_id = host.get("node") if isinstance(host, dict) else host
                initiator_node = get_node_by_id(gateway.ceph_cluster, node_id)
                initiator = Initiator(initiator_node)
                gateway.nvmegwcli.host.add(
                    **{"args": {"subsystem": nqn, "host": initiator.nqn()}}
                )


def configure_namespaces(nvme_service, gateway):
    """
    Configure namespaces for this specific gateway.
    This is called per gateway since each gateway needs its own namespaces.
    Args:
        config: Test configuration.
    """
    # Configure namespaces if specified
    subsystem_config = nvme_service.config.get("subsystems", [])
    for sub_cfg in subsystem_config:
        nqn = sub_cfg.get("nqn") or sub_cfg.get("subnqn")
        if not nqn:
            raise ValueError("Namespace NQN not provided in subsystem_config")

        # Configure namespaces if specified
        sub_args = {"subsystem": nqn}
        if sub_cfg.get("bdevs"):
            bdev_configs = sub_cfg["bdevs"]
            if isinstance(bdev_configs, dict):
                bdev_configs = [bdev_configs]

            for bdev_cfg in bdev_configs:
                name = generate_unique_id(length=4)
                namespace_args = {
                    **sub_args,
                    **{
                        "rbd-pool": nvme_service.rbd_pool,
                        "rbd-create-image": True,
                        "size": bdev_cfg["size"],
                    },
                }

                with parallel() as p:
                    for num in range(bdev_cfg["count"]):
                        ns_args = deepcopy(namespace_args)
                        ns_args["rbd-image"] = f"{name}-image{num}"
                        ns_args = {"args": ns_args}
                        p.spawn(gateway.nvmegwcli.namespace.add, **ns_args)


def configure_listeners(gateway, config: dict):
    """
    Configure listeners for this specific gateway.
    This is called per gateway since each gateway needs its own listeners.
    Args:
        config: Test configuration.
    """
    # Configure listeners if specified
    subsystem_config = config.get("subsystems", [])
    for sub_cfg in subsystem_config:
        if sub_cfg.get("listeners"):
            listeners = sub_cfg["listeners"]
            if not isinstance(listeners, list):
                listeners = [listeners]

            nqn = sub_cfg.get("nqn") or sub_cfg.get("subnqn")

            # for listener in listeners:
            listener_config = {
                "args": {
                    "subsystem": nqn,
                    "traddr": getattr(gateway.ceph_node, "ip_address", None),
                    "trsvcid": sub_cfg.get("listener_port", 4420),
                    "host-name": getattr(
                        gateway.ceph_node, "hostname", str(gateway.ceph_node)
                    ),
                }
            }
            gateway.nvmegwcli.listener.add(**listener_config)


def configure_gw_entities(nvme_service, exec_parallel=False):
    """
    Configure gateway entities for the NVMe service.
    """
    # Fetch daemon info using Orch.ps
    orch = Orch(cluster=nvme_service.ceph_cluster)
    out, _ = orch.ps(
        {"base_cmd_args": {"format": "json"}, "args": {"daemon_type": "nvmeof"}}
    )
    daemons = json.loads(out)
    # Map node hostnames to daemon names
    node_to_daemon = {}
    for d in daemons:
        # d['hostname'], d['daemon_name']
        node_to_daemon[d["hostname"]] = d["daemon_name"]

        # Configure subsystems for the group (done once per group)
        subsystem_config = nvme_service.config.get("subsystems", [])
        if subsystem_config:
            configure_subsystems(nvme_service, exec_parallel=exec_parallel)

        # Configure listeners for each gateway in the group
        for gateway in nvme_service.gateways:
            # For each gateway, update daemon and service names
            node = gateway.ceph_node
            hostname = getattr(node, "hostname", str(node))
            daemon_name = node_to_daemon.get(hostname)
            gateway.gateway_daemon_name = daemon_name
            # Fetch service name using SystemCtl
            systemctl = SystemCtl(node)
            try:
                service_name = systemctl.get_service_unit("*@nvmeof*")
            except Exception:
                service_name = None
            gateway.gateway_service_name = service_name
            if subsystem_config:
                configure_listeners(gateway, nvme_service.config)
                configure_hosts(gateway, nvme_service.config)
                configure_namespaces(nvme_service, gateway)


def teardown(nvme_service, rbd_obj):
    """
    Cleanup NVMeoF gateways, initiators, and pools for the given config.
    Handles both single and multiple gateway groups.
    """
    # Disconnect initiators
    if "initiators" in nvme_service.config.get("cleanup", []):
        if nvme_service.config.get("gw_groups"):
            for gw_group_config in nvme_service.config["gw_groups"]:
                for initiator_cfg in gw_group_config.get("initiators", []):
                    node = get_node_by_id(
                        nvme_service.ceph_cluster, initiator_cfg["node"]
                    )
                    initiator = Initiator(node)
                    initiator.disconnect_all()
        else:
            for initiator_cfg in nvme_service.config.get("initiators", []):
                node = get_node_by_id(nvme_service.ceph_cluster, initiator_cfg["node"])
                initiator = Initiator(node)
                initiator.disconnect_all()

    # Delete the multiple subsystems across multiple gateways
    if "subsystems" in nvme_service.config["cleanup"]:
        config_sub_node = nvme_service.config["subsystems"]
        if not isinstance(config_sub_node, list):
            config_sub_node = [config_sub_node]
        for sub_cfg in config_sub_node:
            node = (
                nvme_service.config["gw_node"]
                if "node" not in sub_cfg
                else sub_cfg["node"]
            )
            nvmegwcli = NVMeGWCLI(
                get_node_by_id(nvme_service.ceph_cluster, node),
                port=sub_cfg.get("port", 5500),
                mtls=nvme_service.config.get("mtls", False),
            )
            nvmegwcli.subsystem.delete(
                **{"args": {"subsystem": sub_cfg["nqn"], "force": True}}
            )

    # Delete gateways
    if "gateway" in nvme_service.config.get("cleanup", []):
        nvme_service.delete_nvme_service()

    # Delete the pool
    if "pool" in nvme_service.config["cleanup"]:
        rbd_obj.clean_up(pools=[nvme_service.config["rbd_pool"]])
