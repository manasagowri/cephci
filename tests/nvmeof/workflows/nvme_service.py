"""
NVMe Service, Gateway Group, and Gateway classes for NVMeoF workflows.
"""

import json
from copy import deepcopy
from typing import Optional

from ceph.ceph_admin.orch import Orch
from ceph.nvmegw_cli import NVMeGWCLI
from ceph.nvmeof.initiator import Initiator
from ceph.parallel import parallel
from ceph.utils import get_node_by_id, get_nodes_by_ids
from tests.cephadm import test_nvmeof
from tests.nvmeof.workflows.gateway import NVMeGateway
from utility.systemctl import SystemCtl
from utility.utils import generate_unique_id, get_ceph_version_from_cluster
from tests.nvmeof.workflows.constants import DEFAULT_NVME_RBD_POOL


class NVMeService:
    def __init__(
        self,
        config,
        ceph_cluster,
    ):
        self.config = config
        self.group = self.config.get("gw_group", None)
        self.mtls = config.get("mtls", False)
        self.dhchap_encryption_key = config.get("dhchap_encryption_key", None)
        self.rbd_pool = self._determine_rbd_pool()
        self.subsystems = []
        self.subsystem_config = config.get("subsystems", [])
        self.ceph_cluster = ceph_cluster
        self.gateways = self._init_gateways(ceph_cluster, self.config.get("gw_nodes", []))
        gw_nodes = config.get("gw_nodes", None) or config.get("gw_node", None)
        if not gw_nodes:
            raise ValueError("Please provide gateway nodes via gw_nodes or gw_node")

        if not isinstance(gw_nodes, list):
            gw_nodes = [gw_nodes]

        self.gw_nodes = get_nodes_by_ids(self.ceph_cluster, gw_nodes)

    def _determine_rbd_pool(self):
        """
        Determine the RBD pool name based on ceph_version.
        If ceph_version < 20.0, use config['rbd_pool'].
        If ceph_version >= 20.0, use DEFAULT_NVME_RBD_POOL.
        """
        current_ceph_version = get_ceph_version_from_cluster(self.ceph_cluster)
        if current_ceph_version < "20.0":
            if not self.config.get("rbd_pool"):
                raise ValueError("Please provide RBD pool name via rbd_pool or pool")
            return self.config.get("rbd_pool")
        else:
            return DEFAULT_NVME_RBD_POOL

    def delete_nvme_service(self, delete_pool=False, rbd_obj=None):
        """Delete the NVMe gateway service.
        Args:
            ceph_cluster: Ceph cluster object
            config: Test case config
        Test case config should have below important params,
        - rbd_pool
        - gw_nodes
        - gw_group      # optional, as per release
        - mtls          # optional
        """
        ceph_cluster = self.ceph_cluster

        gw_group = self.group
        pool = self.rbd_pool
        service_name = f"nvmeof.{pool}"
        service_name = f"{service_name}.{gw_group}" if gw_group else service_name
        cfg = {
            "no_cluster_state": False,
            "config": {
                "command": "remove",
                "service": "nvmeof",
                "args": {
                    "service_name": service_name,
                    "verify": True,
                },
            },
        }
        test_nvmeof.run(ceph_cluster, **cfg)
        if delete_pool and rbd_obj:
            rbd_obj[0].clean_up(pools=[pool])

    def _create_spec_deployment_config(self):
        """Create spec-based deployment configuration."""
        release = self.ceph_cluster.rhcs_version
        spec = {
            "service_type": "nvmeof",
            "service_id": self.rbd_pool,
            "mtls": self.mtls,
            "placement": self._get_placement_config(self.config, self.gw_nodes),
            "spec": {
                "pool": self.rbd_pool,
                "enable_auth": self.config.get("mtls", False),
            },
        }

        # Add encryption if specified
        if self.dhchap_encryption_key:
            spec["encryption"] = True

        # Add group if specified
        if self.group:
            spec["spec"]["group"] = self.group

        is_spec_or_mtls = self.mtls or self.config.get(
            "spec_deployment", False
        )

        if is_spec_or_mtls:
            cfg = {
                "no_cluster_state": False,
                "config": {
                    "command": "apply_spec",
                    "service": "nvmeof",
                    "validate-spec-services": self.config.get("validate-spec-services", True),
                    "specs": [spec],
                },
            }
            # Handle version-specific logic
            if release <= "7.1":
                return cfg
            elif release >= "8":
                if not self.group:
                    raise ValueError("Gateway group not provided for RHCS 8+")

                if is_spec_or_mtls:
                    cfg["config"]["specs"][0]["service_id"] = f"{self.rbd_pool}.{self.group}"
                    cfg["config"]["specs"][0]["spec"]["group"] = self.group
                else:
                    cfg["config"]["pos_args"].append(self.group)

                # Add rebalance period if specified
                if self.config.get("rebalance_period", False):
                    rebalance_sec = self.config.get("rebalance_period_sec", 0)
                    cfg["config"]["specs"][0]["spec"][
                        "rebalance_period_sec"
                    ] = rebalance_sec

                return cfg
        else:
            cfg = {
                "no_cluster_state": False,
                "config": {
                    "command": "apply",
                    "service": "nvmeof",
                    "args": {"placement": self._get_placement_config(self.config, self.gw_nodes)},
                    "pos_args": [self.rbd_pool],
                },
            }

        return cfg
    

    def _get_placement_config(self, config, gw_nodes):
        """Get placement configuration based on config options."""
        placement = {"nodes": [i.hostname for i in gw_nodes]}

        # Add label-based placement if specified
        if config.get("label"):
            placement["label"] = config["label"]

        # Add limit if specified
        if config.get("limit"):
            placement["limit"] = config["limit"]

        # Add separator if specified
        if config.get("sep"):
            placement["sep"] = config["sep"]

        return placement

    def deploy_service_and_update(self, install=False):
        """
        Deploy NVMe gateways using orchestrator, then fetch and update daemon and service names for each gateway node.
        After deployment, configure subsystems and listeners if config is provided.
        """
        if install:
            deploy_config = self._create_spec_deployment_config()
            if deploy_config:
                test_nvmeof.run(self.ceph_cluster, **deploy_config)

        # Fetch daemon info using Orch.ps
        orch = Orch(cluster=self.ceph_cluster)
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
            if self.subsystem_config:
                self.configure_subsystems()

            # Configure listeners for each gateway in the group
            for gateway in self.gateways:
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
                if gateway.subsystem_config:
                    gateway.configure_listeners(gateway.subsystem_config)

    def disconnect_initiator(self, ceph_cluster, node):
        """
        Disconnect an initiator node from the NVMeoF subsystem.
        """
        node = get_node_by_id(ceph_cluster, node)
        initiator = Initiator(node)
        initiator.disconnect_all()

    def teardown(self, ceph_cluster, rbd_obj, config):
        """
        Cleanup NVMeoF gateways, initiators, and pools for the given config.
        Handles both single and multiple gateway groups.
        """
        # Disconnect initiators
        if "initiators" in config.get("cleanup", []):
            if config.get("gw_groups"):
                for gw_group_config in config["gw_groups"]:
                    for initiator_cfg in gw_group_config.get("initiators", []):
                        self.disconnect_initiator(ceph_cluster, initiator_cfg["node"])
            else:
                for initiator_cfg in config.get("initiators", []):
                    self.disconnect_initiator(ceph_cluster, initiator_cfg["node"])

        # Delete the multiple subsystems across multiple gateways
        if "subsystems" in config["cleanup"]:
            config_sub_node = config["subsystems"]
            if not isinstance(config_sub_node, list):
                config_sub_node = [config_sub_node]
            for sub_cfg in config_sub_node:
                node = config["gw_node"] if "node" not in sub_cfg else sub_cfg["node"]
                nvmegwcli = NVMeGWCLI(
                    get_node_by_id(ceph_cluster, node),
                    port=sub_cfg.get("listener_port", 4420),
                    mtls=config.get("mtls", False),
                )
                nvmegwcli.subsystem.delete(
                    **{"args": {"subsystem": sub_cfg["nqn"], "force": True}}
                )

        # Delete gateways
        if "gateway" in config.get("cleanup", []):
            if "pool" in config.get("cleanup", []):
                delete_pool = True
            else:
                delete_pool = False
            self.delete_nvme_service(delete_pool=delete_pool, rbd_obj=rbd_obj)

    def configure_subsystems(self, pool: str = None):
        """
        Configure subsystems, hosts, and namespaces for this gateway group.
        This is done once per group, not per gateway.
        Args:
            subsystem_config: Configuration for the subsystems
            pool: RBD pool name (optional, will use self.pool_name or default if not provided)
        """
        if not pool:
            # Use self.pool_name if available, otherwise try to get from config or use default
            pool = self.pool_name or self.subsystem_config.get("pool", ".nvme")

        # Configure subsystem
        for sub_cfg in self.subsystem_config:
            nqn = sub_cfg.get("nqn") or sub_cfg.get("subnqn")
            if not nqn:
                raise ValueError("Subsystem NQN not provided in subsystem_config")

            # Add to subsystems list
            if nqn not in self.subsystems:
                self.subsystems.append(nqn)

            # Use the first gateway's nvmegwcli for subsystem configuration
            if not self.gateways:
                raise ValueError("No gateways available for subsystem configuration")

            nvmegwcli = self.gateways[0].nvmegwcli

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

            # Configure hosts if specified
            if sub_cfg.get("allow_host"):
                nvmegwcli.host.add(
                    **{"args": {**sub_args, **{"host": repr(sub_cfg["allow_host"])}}}
                )

            if sub_cfg.get("hosts"):
                for host in sub_cfg["hosts"]:
                    node_id = host.get("node") if isinstance(host, dict) else host
                    initiator_node = get_node_by_id(self.ceph_cluster, node_id)
                    initiator = Initiator(initiator_node)
                    nvmegwcli.host.add(
                        **{"args": {**sub_args, **{"host": initiator.nqn()}}}
                    )

            # Configure namespaces if specified
            if sub_cfg.get("bdevs"):
                bdev_configs = sub_cfg["bdevs"]
                if isinstance(bdev_configs, dict):
                    bdev_configs = [bdev_configs]

                for bdev_cfg in bdev_configs:
                    name = generate_unique_id(length=4)
                    namespace_args = {
                        **sub_args,
                        **{
                            "rbd-pool": pool,
                            "rbd-create-image": True,
                            "size": bdev_cfg["size"],
                        },
                    }

                    with parallel() as p:
                        for num in range(bdev_cfg["count"]):
                            ns_args = deepcopy(namespace_args)
                            ns_args["rbd-image"] = f"{name}-image{num}"
                            ns_args = {"args": ns_args}
                            p.spawn(nvmegwcli.namespace.add, **ns_args)

    def _init_gateways(self, ceph_cluster, ceph_nodes):
        """
        Initialize NVMeGateway objects for each ceph_node in the group.
        """
        gateways = []
        port = getattr(self, "port", 5500)
        mtls = getattr(self, "mtls", False)

        for node in ceph_nodes:
            daemon_name = f"nvme-gw.{self.group}.{getattr(node, 'hostname', str(node))}"
            service_name = f"nvmeof.{self.group}"
            gateways.append(
                NVMeGateway(
                    node,
                    ceph_cluster,
                    daemon_name,
                    service_name,
                    mtls=mtls,
                    port=port,
                    subsystem_config=self.subsystem_config,
                )
            )
        return gateways
