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
from utility.systemctl import SystemCtl
from utility.utils import generate_unique_id

# Default NVMe RBD pool for ceph_version >= 9.0
DEFAULT_NVME_RBD_POOL = ".nvme"


class NVMeService:
    """
    NVMe Service abstraction for NVMeoF workflows.

    Attributes:
        rbd_pool: RBD pool name (depends on ceph_version)
        ceph_cluster: Ceph cluster object
        config: Test configuration dict
        gateway_groups: List of NVMeGatewayGroup objects
    """

    def __init__(
        self,
        ceph_cluster,
        config: dict,
    ):
        self.ceph_cluster = ceph_cluster
        self.config = config
        self.rbd_pool = self._determine_rbd_pool()
        self.gateway_groups = self._init_gateway_groups(ceph_cluster)

    def _determine_rbd_pool(self):
        """
        Determine the RBD pool name based on ceph_version.
        If ceph_version <= 8.x, use config['rbd_pool'].
        If ceph_version >= 9.0, use DEFAULT_NVME_RBD_POOL.
        """
        version = str(getattr(self.ceph_cluster, "rhcs_version", ""))
        if version and version.split(".")[0].isdigit():
            major = int(version.split(".")[0])
            if major < 9:
                return self.config.get("rbd_pool")
            else:
                return DEFAULT_NVME_RBD_POOL
        # Fallback
        return self.config.get("rbd_pool", DEFAULT_NVME_RBD_POOL)

    def _init_gateway_groups(self, ceph_cluster):
        """
        Initialize NVMeGatewayGroup objects from config['gw_group'].
        config['gw_group'] should be a list of dicts, each with keys:
            - 'group_name': str
            - 'nodes': list of ceph_node objects
            - 'load_balancing_id': str (optional)
            - 'mtls': bool (optional)
            - 'dhchap_encryption_key': str (optional)
            - 'subsystems': dict (optional)
            - 'pool_name': str (optional)
        """
        groups = []
        gw_groups = self.config.get("gw_groups", [])
        for group in gw_groups:
            group_name = group.get("group_name")
            nodes = group.get("gw_nodes", [])
            load_balancing_id = group.get("load_balancing_id", None)
            mtls = group.get("mtls", False)
            dhchap_encryption_key = group.get("dhchap_encryption_key")
            subsystem_config = group.get("subsystems")
            pool_name = (
                group.get("rbd_pool")
                or self.config.get("rbd_pool")
                or DEFAULT_NVME_RBD_POOL
            )
            groups.append(
                NVMeGatewayGroup(
                    ceph_nodes=nodes,
                    ceph_cluster=ceph_cluster,
                    gateway_group_name=group_name,
                    load_balancing_id=load_balancing_id,
                    mtls=mtls,
                    dhchap_encryption_key=dhchap_encryption_key,
                    subsystem_config=subsystem_config,
                    pool_name=pool_name,
                )
            )
        return groups

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
        config = self.config
        ceph_cluster = self.ceph_cluster
        gw_groups = config.get("gw_groups", [{"gw_group": config.get("gw_group", "")}])

        for gwgroup_config in gw_groups:
            gw_group = gwgroup_config["gw_group"]
            pool = gwgroup_config.get(
                "rbd_pool", config.get("rbd_pool", DEFAULT_NVME_RBD_POOL)
            )
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

    def deploy_nvme_service(self, **kwargs):
        """
        Comprehensive NVMe service deployment method that handles all deployment patterns.

        This method supports:
        - Multiple gateway groups
        - mTLS authentication
        - Encryption keys
        - Rebalance periods
        - Different RBD pools per group
        - Custom placement options
        - Validation options
        - Custom CLI images

        Args:
            **kwargs: Additional deployment options that override self.config

        Returns:
            dict: Deployment configuration for test_nvmeof.run
        """
        config = self.config.copy()
        config.update(kwargs)

        # Handle multiple gateway groups
        gw_groups = config.get("gw_groups", [])
        if gw_groups:
            # Deploy each gateway group separately
            deploy_config = []
            for gwgroup_config in gw_groups:
                # Merge group config with base config
                group_config = config.copy()
                group_config.update(gwgroup_config)
                deploy_config.append(self._deploy_single_gateway_group(group_config))
            return deploy_config
        else:
            # Single gateway group deployment
            return self._deploy_single_gateway_group(config)

    def _deploy_single_gateway_group(self, config):
        """
        Deploy a single gateway group with all supported options.

        Args:
            config: Configuration for the gateway group

        Returns:
            dict: Deployment configuration for test_nvmeof.run
        """
        release = self.ceph_cluster.rhcs_version
        rbd_pool = config.get("rbd_pool") or config.get("pool")
        if not rbd_pool:
            raise ValueError("Please provide RBD pool name via rbd_pool or pool")

        gw_nodes = config.get("gw_nodes", None) or config.get("gw_node", None)
        if not gw_nodes:
            raise ValueError("Please provide gateway nodes via gw_nodes or gw_node")

        if not isinstance(gw_nodes, list):
            gw_nodes = [gw_nodes]

        gw_nodes = get_nodes_by_ids(self.ceph_cluster, gw_nodes)
        gw_group = config.get("gw_group")

        # Determine deployment type
        is_spec_or_mtls = config.get("mtls", False) or config.get(
            "spec_deployment", False
        )
        use_encryption = config.get("encryption", False)

        # Base configuration
        if is_spec_or_mtls:
            cfg = self._create_spec_deployment_config(
                config, rbd_pool, gw_nodes, gw_group, use_encryption
            )
        else:
            cfg = self._create_apply_deployment_config(
                config, rbd_pool, gw_nodes, gw_group
            )

        # Handle version-specific logic
        if release <= "7.1":
            return cfg
        elif release >= "8":
            if not gw_group:
                raise ValueError("Gateway group not provided for RHCS 8+")

            if is_spec_or_mtls:
                cfg["config"]["specs"][0]["service_id"] = f"{rbd_pool}.{gw_group}"
                cfg["config"]["specs"][0]["spec"]["group"] = gw_group
            else:
                cfg["config"]["pos_args"].append(gw_group)

            # Add rebalance period if specified
            if config.get("rebalance_period", False):
                rebalance_sec = config.get("rebalance_period_sec", 0)
                cfg["config"]["specs"][0]["spec"][
                    "rebalance_period_sec"
                ] = rebalance_sec

            return cfg

        return cfg

    def _create_spec_deployment_config(
        self, config, rbd_pool, gw_nodes, gw_group, use_encryption
    ):
        """Create spec-based deployment configuration."""
        spec = {
            "service_type": "nvmeof",
            "service_id": rbd_pool,
            "mtls": config.get("mtls", False),
            "placement": self._get_placement_config(config, gw_nodes),
            "spec": {
                "pool": rbd_pool,
                "enable_auth": config.get("mtls", False),
            },
        }

        # Add encryption if specified
        if use_encryption:
            spec["encryption"] = True

        # Add group if specified
        if gw_group:
            spec["spec"]["group"] = gw_group

        cfg = {
            "no_cluster_state": False,
            "config": {
                "command": "apply_spec",
                "service": "nvmeof",
                "validate-spec-services": config.get("validate-spec-services", True),
                "specs": [spec],
            },
        }

        return cfg

    def _create_apply_deployment_config(self, config, rbd_pool, gw_nodes, gw_group):
        """Create apply-based deployment configuration."""
        cfg = {
            "no_cluster_state": False,
            "config": {
                "command": "apply",
                "service": "nvmeof",
                "args": {"placement": self._get_placement_config(config, gw_nodes)},
                "pos_args": [rbd_pool],
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

    def _get_deploy_config(self):
        """
        Prepare config for test_nvmeof.run based on self.config.
        """
        # This is a direct copy of the deploy_nvme_service logic
        from tests.nvmeof.workflows.nvme_utils import apply_nvme_sdk_cli_support

        return apply_nvme_sdk_cli_support(self.ceph_cluster, self.config)

    def deploy_service_and_update(self, install=False):
        """
        Deploy NVMe gateways using orchestrator, then fetch and update daemon and service names for each gateway node.
        After deployment, configure subsystems and listeners if config is provided.
        """
        # Deploy gateways using orchestrator (adapted from deploy_nvme_service)
        if install:
            deploy_config = self.deploy_nvme_service()
            if deploy_config:
                if isinstance(deploy_config, list):
                    for d_config in deploy_config:
                        test_nvmeof.run(self.ceph_cluster, **d_config)
                else:
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

        # Configure subsystems and listeners after deployment
        for group in self.gateway_groups:
            # Configure subsystems for the group (done once per group)
            if group.subsystem_config:
                group.configure_subsystems(group.subsystem_config)

            # Configure listeners for each gateway in the group
            for gateway in group.gateways:
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


class NVMeGatewayGroup:
    """
    NVMe Gateway Group abstraction.

    Attributes:
        gateways: List of NVMeGateway objects
        gateway_group_name: Name of the gateway group
        load_balancing_id: Load balancing group ID
        mtls: Whether mTLS is enabled for this group
        dhchap_encryption_key: DHCHAP encryption key for this group
        subsystems: List of configured subsystem NQNs
        pool_name: RBD pool name for this gateway group
    """

    def __init__(
        self,
        ceph_nodes,
        ceph_cluster,
        gateway_group_name: str,
        load_balancing_id: Optional[str] = None,
        mtls: bool = False,
        dhchap_encryption_key: Optional[str] = None,
        subsystem_config: dict = None,
        pool_name: str = None,
    ):
        self.gateway_group_name = gateway_group_name
        self.load_balancing_id = load_balancing_id
        self.mtls = mtls
        self.dhchap_encryption_key = dhchap_encryption_key
        self.subsystem_config = subsystem_config
        self.pool_name = pool_name
        self.subsystems = []
        self.ceph_cluster = ceph_cluster
        self.gateways = self._init_gateways(ceph_cluster, ceph_nodes)

    def configure_subsystems(self, subsystem_config: list, pool: str = None):
        """
        Configure subsystems, hosts, and namespaces for this gateway group.
        This is done once per group, not per gateway.

        Args:
            subsystem_config: Configuration for the subsystems
            pool: RBD pool name (optional, will use self.pool_name or default if not provided)
        """
        if not pool:
            # Use self.pool_name if available, otherwise try to get from config or use default
            pool = self.pool_name or subsystem_config.get("pool", ".nvme")

        # Configure subsystem
        for sub_cfg in subsystem_config:
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
            daemon_name = f"nvme-gw.{self.gateway_group_name}.{getattr(node, 'hostname', str(node))}"
            service_name = f"nvmeof.{self.gateway_group_name}"
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


class NVMeGateway:
    """
    NVMe Gateway abstraction (minimal, for workflow use).
    For full gateway logic, see tests/nvmeof/workflows/nvme_gateway.py:NVMeGateway.

    Attributes:
        ceph_node: Node where the gateway is deployed
        ceph_cluster: Ceph cluster object
        gateway_daemon_name: Name of the gateway daemon
        gateway_service_name: Name of the gateway service
        nvmegwcli: NVMeGWCLI instance for this gateway (or future class for >= 9.0)
    """

    def __init__(
        self,
        ceph_node,
        ceph_cluster,
        gateway_daemon_name: str,
        gateway_service_name: str,
        mtls: bool = False,
        port: int = 5500,
        subsystem_config: dict = None,
    ):
        self.ceph_node = get_node_by_id(ceph_cluster, ceph_node)
        self.ceph_cluster = ceph_cluster
        self.gateway_daemon_name = gateway_daemon_name
        self.gateway_service_name = gateway_service_name
        self.nvmegwcli = self._init_cli_object(port, mtls)
        self.subsystem_config = subsystem_config

    def _init_cli_object(self, port: int, mtls: bool):
        """
        Initialize the appropriate CLI object based on Ceph version.
        For ceph_version < 9.0: use NVMeGWCLI
        For ceph_version >= 9.0: placeholder for future class
        """
        version = str(getattr(self.ceph_cluster, "rhcs_version", ""))
        if version and version.split(".")[0].isdigit():
            major = int(version.split(".")[0])
            # make is ceph version >= 20
            # whenever there is a ceph version change, add a method to update nvmegwcli object to the existing class
            # using decorator
            if major < 9:
                return NVMeGWCLI(self.ceph_node, port=port, mtls=mtls)
            else:
                # TODO: Replace with new CLI class for RHCS 9.0+
                # For now, use NVMeGWCLI as placeholder
                # return NewNVMeGWCLI(self.ceph_node, port=port, mtls=mtls)
                return NVMeGWCLI(self.ceph_node, port=port, mtls=mtls)
        # Fallback to NVMeGWCLI if version cannot be determined
        return NVMeGWCLI(self.ceph_node, port=port, mtls=mtls)

    def configure_listeners(self, subsystem_config: dict):
        """
        Configure listeners for this specific gateway.
        This is called per gateway since each gateway needs its own listeners.

        Args:
            subsystem_config: Configuration for the subsystem
        """
        # Configure listeners if specified
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
                        "traddr": getattr(self.ceph_node, "ip_address", None),
                        "trsvcid": sub_cfg.get("listener_port", 4420),
                        "host-name": getattr(
                            self.ceph_node, "hostname", str(self.ceph_node)
                        ),
                    }
                }
                self.nvmegwcli.listener.add(**listener_config)
