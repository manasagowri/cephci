"""
NVMe High Availability Module.
"""

import json
import time

from ceph.utils import get_node_by_id, get_nodes_by_ids
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_gateway import NVMeGateway
from utility.log import Log
from utility.utils import log_json_dump

from tests.cephadm import test_nvmeof

LOG = Log(__name__)


class NVMeDeployArgumentError(Exception):
    pass


class NVMeDeployConfigParamRequired(Exception):
    pass


class NVMEService:
    def __init__(self, ceph_cluster, gateways, **config):
        """Initialize NVMeoF Gateway High Availability class.

        Args:
            cluster: Ceph cluster
            gateways: Gateway node Ids
            config: Service config
        """
        self.cluster = ceph_cluster
        self.config = config
        self.gateways = []
        self.mtls = config.get("mtls")
        self.gateway_group = config.get("gw_group", "")
        self.nvme_pool = config["rbd_pool"]

        for gateway in gateways:
            gw_node = get_node_by_id(self.cluster, gateway)
            self.gateways.append(NVMeGateway(gw_node, self.mtls))

        self.ana_ids = [i.ana_group_id for i in self.gateways]

    def check_gateway(self, node_id):
        """Check node is NVMeoF Gateway node.

        Args:
            node_id: Ceph node Id (ex., node6)
        """
        for gw in self.gateways:
            if gw.node.id == node_id:
                LOG.info(f"[{node_id}] {gw.node.hostname} is NVMeoF Gateway node.")
                return gw
        raise Exception(f"{node_id} doesn't match to any gateways provided...")

    def get_or_create_initiator(self, node_id, nqn):
        """Get existing NVMeInitiator or create a new one for each (node_id, nqn)."""
        key = (node_id, nqn)  # Use both as dictionary key

        if key not in self.initiators:
            node = get_node_by_id(self.cluster, node_id)
            self.initiators[key] = NVMeInitiator(node, self.gateways[0], nqn)

        return self.initiators[key]

    def create_dhchap_key(self, config, update_host_key=False):
        """Generate DHCHAP key for each initiator and store it."""
        subnqn = config["subnqn"]
        group = config["gw_group"]
        nqn = f"{subnqn}.{group}"

        for host_config in config["hosts"]:
            node_id = host_config["node"]
            initiator = self.get_or_create_initiator(node_id, nqn)

            # Generate key for subsystem NQN
            key, _ = initiator.gen_dhchap_key(n=config["subnqn"])
            LOG.info(f"{key.strip()} is generated for {nqn} and {node_id}")

            initiator.nqn = config["subnqn"]
            initiator.auth_mode = config.get("auth_mode")
            if initiator.auth_mode == "bidirectional" and not update_host_key:
                initiator.subsys_key = key.strip()
                initiator.host_key = key.strip()
            if initiator.auth_mode == "unidirectional":
                initiator.host_key = key.strip()
            if update_host_key:
                initiator.host_key = key.strip()
            config["dhchap-key"] = key.strip()

            self.clients.append(initiator)

    def catogorize(self, gws):
        """Categorize to-be failed and running GWs.

        Args:
            gws: gateways to be failed/stopped/scaled-down

        Returns:
            list of,
             - to-be failed gateways
             - rest of the gateways
        """
        fail_gws = []
        running_gws = []

        # collect impending Gateways to be failed.
        if isinstance(gws, str):
            gws = [gws]
        for gw_id in gws:
            fail_gws.append(self.check_gateway(gw_id))

        # Collect rest of the Gateways
        for gw in self.gateways:
            if gw.node.id not in gws:
                running_gws.append(gw)

        return fail_gws, running_gws

    @staticmethod
    def string_to_dict(string):
        """Parse ANA states from the string."""
        states = string.replace(" ", "").split(",")
        dict = {}
        for state in states:
            if not state:
                continue
            _id, _state = state.split(":")
            dict[int(_id)] = _state
        return dict

    def ana_states(self, gw_group=""):
        """Fetch ANA states and convert into python dict."""

        out, _ = self.orch.shell(
            args=["ceph", "nvme-gw", "show", self.nvme_pool, repr(self.gateway_group)]
        )
        states = {}
        if self.cluster.rhcs_version >= "8":
            out = json.loads(out)
            for gateway in out.get("Created Gateways:"):
                gw = gateway["gw-id"]
                states[gw] = gateway
                states[gw].update(self.string_to_dict(gateway["ana states"]))
        else:
            for data in out.split("}"):
                data = data.strip()
                if not data:
                    continue
                data = json.loads(f"{data}}}")
                if data.get("ana states"):
                    gw = data["gw-id"]
                    states[gw] = data
                    states[gw].update(self.string_to_dict(data["ana states"]))

        return states

    def check_gateway_availability(self, ana_id, state="AVAILABLE", ana_states=None):
        """Check for failed ANA GW become unavailable.

        Args:
            ana_id: Gateway ANA group id.
            state: Gateway availability state
            ana_states: Overall ana state. (output from self.ana_states)
        Return:
            True if Gateway availability is in expected state, else False
        """
        # get ANA states
        if not ana_states:
            ana_states = self.ana_states()

        # Check Availability of ANA Group Gateway
        for _, _state in ana_states.items():
            if _state["anagrp-id"] == ana_id:
                if _state["Availability"] == state:
                    return True
                return False
        return False

    def get_optimized_state(self, failed_ana_id):
        """Fetch the Optimized ANA states for failed gateway.

        Args:
            gateway: The gateway which is operational.
            failed_ana_id: failed gateway ANA Group Id.

        Returns:
            gateways which shows ACTIVE state for failed ANA Group Id
        """
        # get ANA states
        ana_states = self.ana_states()

        # Fetch failed ANA Group Id in ACTIVE state
        found = []

        for ana_gw_id, state in ana_states.items():
            if (
                state["Availability"] == "AVAILABLE"
                and state.get(failed_ana_id) == "ACTIVE"
            ):
                found.append({ana_gw_id: state})

        return found

    def fetch_namespaces(self, gateway, failed_ana_grp_ids=[], get_list=False):
        """Fetch all namespaces for failed gateways.

        Args:
            gateway: Operational gateway
            failed_ana_grp_ids: Failed or to-be failed gateway ids
        Returns:
            list of namespaces
        """
        args = {"base_cmd_args": {"format": "json"}}
        _, subsystems = gateway.subsystem.list(**args)
        subsystems = json.loads(subsystems)

        namespaces = []
        all_ns = []
        for subsystem in subsystems["subsystems"]:
            sub_name = subsystem["nqn"]
            cmd_args = {"args": {"subsystem": subsystem["nqn"]}}
            _, nspaces = gateway.namespace.list(**{**args, **cmd_args})
            nspaces = json.loads(nspaces)["namespaces"]
            all_ns.extend(nspaces)

            if failed_ana_grp_ids:
                for ns in nspaces:
                    if ns["load_balancing_group"] in failed_ana_grp_ids:
                        # <subsystem>|<nsid>|<pool_name>|<image>
                        ns_info = f"nsid-{ns['nsid']}|{ns['rbd_pool_name']}|{ns['rbd_image_name']}"
                        if get_list:
                            namespaces.append(
                                {"list": ns, "info": f"{sub_name}|{ns_info}"}
                            )
                        else:
                            namespaces.append(f"{sub_name}|{ns_info}")
        if not failed_ana_grp_ids:
            LOG.info(f"All namespaces : {log_json_dump(all_ns)}")
            return all_ns

        LOG.info(
            f"Namespaces found for ANA-grp-id [{failed_ana_grp_ids}]: {log_json_dump(namespaces)}"
        )
        return namespaces
    
    def apply_nvme_sdk_cli_support(ceph_cluster, config):
        """Configure NVMe deployment CLI w.r.t release support.

        This definition helps to select deployment CLI as supported
        from a downstream release perspective.

        Currently,
        7.x - Only RBD pool name has to be provided as positional arg
        8.0 - Along RBD pool name, the Gateway group name has to be provided.

        And in future any change in deployment could be handled here.

        Args:
        ceph_cluster: Ceph cluster object
        config: test case configuration parameters

        ::Example:
            config:
                rbd_pool: rbd               # rbd pool name
                gw_group: gateway_group1    # NVMe Gateway group name
        """

        release = ceph_cluster.rhcs_version
        rbd_pool = config.get("rbd_pool") or config.get("pool")
        if not rbd_pool:
            raise NVMeDeployConfigParamRequired(
                "Please provide RBD pool name nodes via rbd_pool or pool"
            )

        gw_nodes = config.get("gw_nodes", None) or config.get("gw_node", None)

        if not gw_nodes:
            raise NVMeDeployConfigParamRequired(
                "Please provide gateway nodes via gw_nodes or gw_node"
            )

        if not isinstance(gw_nodes, list):
            gw_nodes = [gw_nodes]

        gw_nodes = get_nodes_by_ids(ceph_cluster, gw_nodes)
        is_spec_or_mtls = config.get("mtls", False) or config.get("spec_deployment", False)
        gw_group = config.get("gw_group")

        cfg = {
            "no_cluster_state": False,
            "config": {
                "command": "apply",
                "service": "nvmeof",
                "args": {"placement": {"nodes": [i.hostname for i in gw_nodes]}},
                "pos_args": [rbd_pool],
            },
        }
        if is_spec_or_mtls:
            cfg = {
                "no_cluster_state": False,
                "config": {
                    "command": "apply_spec",
                    "service": "nvmeof",
                    "validate-spec-services": True,
                    "specs": [
                        {
                            "service_type": "nvmeof",
                            "service_id": rbd_pool,
                            "mtls": config.get("mtls", False),
                            "placement": {"nodes": [i.hostname for i in gw_nodes]},
                            "spec": {
                                "pool": rbd_pool,
                                "enable_auth": config.get("mtls", False),
                            },
                        }
                    ],
                },
            }

        if release <= ("7.1"):
            return cfg
        elif release >= "8":
            if not gw_group:
                raise NVMeDeployArgumentError("Gateway group not provided..")

            if is_spec_or_mtls:
                cfg["config"]["specs"][0]["service_id"] = f"{rbd_pool}.{gw_group}"
                cfg["config"]["specs"][0]["spec"]["group"] = gw_group
            else:
                cfg["config"]["pos_args"].append(gw_group)

            if config.get("rebalance_period", False):
                cfg["config"]["specs"][0]["spec"]["rebalance_period_sec"] = config.get(
                    "rebalance_period_sec"
                )
            return cfg
    
    def deploy_nvme_service(self, ceph_cluster, config):
        """Deploy NVMe Service with apply or with spec

        Args:
            ceph_cluster: Ceph cluster object
            config: Test case config

        Test case config should have below important params,
        - rbd_pool
        - gw_nodes
        - gw_group      # optional, as per release
        - mtls          # optional
        """
        _cfg = self.apply_nvme_sdk_cli_support(ceph_cluster, config)
        test_nvmeof.run(ceph_cluster, **_cfg)

    def delete_nvme_service(self, ceph_cluster, config):
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
        gw_groups = config.get("gw_groups", [{"gw_group": config.get("gw_group", "")}])

        for gwgroup_config in gw_groups:
            gw_group = gwgroup_config["gw_group"]
            service_name = f"nvmeof.{config['rbd_pool']}"
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
