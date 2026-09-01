"""
NVMe Service, Gateway Group, and Gateway classes for NVMeoF workflows.
"""

import json
import time

import yaml
from looseversion import LooseVersion

from ceph.ceph_admin.orch import Orch
from ceph.utils import get_nodes_by_ids
from tests.cephadm import test_nvmeof
from tests.nvmeof.workflows.constants import DEFAULT_NVME_METADATA_POOL, DEFAULT_PORT
from tests.nvmeof.workflows.nvme_gateway import create_gateway
from tests.nvmeof.workflows.nvme_utils import (
    check_and_enable_nvmeof_module,
    nvme_gw_cli_version_adapter,
    setup_firewalld,
)
from utility.log import Log
from utility.utils import get_ceph_version_from_cluster

LOG = Log(__name__)


class NVMeService:
    def __init__(
        self,
        config,
        ceph_cluster,
    ):
        self.config = config
        self.group = self.config.get("gw_group", None)
        self.mtls = config.get("mtls", False)
        self.inband_auth_mode = config.get("inband_auth_mode", None)
        self.ceph_cluster = ceph_cluster
        self.clients = self.ceph_cluster.get_nodes(role="client")
        if not self.clients:
            raise ValueError("No client nodes found in the cluster")
        self.ceph_version = self._get_ceph_version()
        self.nvme_metadata_pool = self._determine_nvme_metadata_pool()
        self.rbd_pool = config.get("rbd_pool")
        if not self.rbd_pool:
            raise ValueError("Please provide RBD pool name via rbd_pool")
        gw_nodes = config.get("gw_nodes", None) or config.get("gw_node", None)
        if not gw_nodes:
            raise ValueError("Please provide gateway nodes via gw_nodes or gw_node")

        if not isinstance(gw_nodes, list):
            gw_nodes = [gw_nodes]

        self.gw_nodes = get_nodes_by_ids(self.ceph_cluster, gw_nodes)
        self.is_spec_or_mtls = self.mtls or self.config.get("spec_deployment", False)
        if self.inband_auth_mode:
            self.is_spec_or_mtls = True

    def _get_ceph_version(self):
        return get_ceph_version_from_cluster(self.clients[0])

    def _determine_nvme_metadata_pool(self):
        """
        Determine the NVMe metadata pool name based on ceph_version.
        If ceph_version >= 20.2.1, use DEFAULT_NVME_METADATA_POOL (.nvmeof).
        If ceph_version < 20.2.1, use config['nvme_metadata_pool'].
        """
        if LooseVersion(self.ceph_version) >= LooseVersion("20.2.1"):
            # print the nvmeof metadata pool
            LOG.info(f"Using NVMeoF metadata pool: {DEFAULT_NVME_METADATA_POOL}")
            return DEFAULT_NVME_METADATA_POOL
        else:
            LOG.info(
                f"Using NVMe metadata pool: {self.config.get('nvme_metadata_pool')}"
            )
            if not self.config.get("nvme_metadata_pool"):
                raise ValueError("Please provide RBD pool name via nvme_metadata_pool")
            return self.config.get("nvme_metadata_pool")

    def delete_nvme_service(self):
        """Delete the NVMe gateway service."""
        ceph_cluster = self.ceph_cluster

        service_name = self.service_name
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
        rc = test_nvmeof.run(ceph_cluster, **cfg)
        return rc

    def _create_spec_deployment_config(self):
        """Create spec-based deployment configuration."""
        release = self.ceph_cluster.rhcs_version
        spec = {
            "service_type": "nvmeof",
            "service_id": self.nvme_metadata_pool,
            "mtls": self.mtls,
            "placement": self._get_placement_config(self.config, self.gw_nodes),
            "spec": {
                "pool": self.nvme_metadata_pool,
                "enable_auth": self.config.get("mtls", False),
            },
        }

        # Delete pool key from spec if ceph_version >= 20.2.1
        if LooseVersion(self.ceph_version) >= LooseVersion("20.2.1"):
            spec["spec"].pop("pool")

        # Add encryption if specified (TLS pre-shared key generated on installer)
        if self.inband_auth_mode:
            spec["encryption"] = True

        # Add support for enable_encryption and encryption_key_path params
        # Refer https://ibm-ceph.atlassian.net/browse/IBMCEPH-16168
        if self.config.get("enable_encryption", False):
            spec["enable_encryption"] = True
        if self.config.get("encryption_key_path", False):
            spec["encryption_key_path"] = True

        # Add group if specified
        if self.group:
            spec["spec"]["group"] = self.group

        if self.config.get("gw_max_namespaces"):
            spec["spec"]["max_namespaces"] = int(self.config["gw_max_namespaces"])
        if self.config.get("max_namespaces_per_subsystem"):
            spec["spec"]["max_namespaces_per_subsystem"] = int(
                self.config["max_namespaces_per_subsystem"]
            )
        if self.config.get("max_namespaces_with_netmask"):
            spec["spec"]["max_namespaces_with_netmask"] = int(
                self.config["max_namespaces_with_netmask"]
            )

        if self.is_spec_or_mtls:
            cfg = {
                "no_cluster_state": False,
                "config": {
                    "command": "apply_spec",
                    "service": "nvmeof",
                    "validate-spec-services": self.config.get(
                        "validate-spec-services", True
                    ),
                    "specs": [spec],
                },
            }
            # Handle version-specific logic
            if release <= "7.1":
                return cfg
            elif release >= "8":
                if not self.group:
                    raise ValueError("Gateway group not provided for RHCS 8+")

                if self.is_spec_or_mtls:
                    cfg["config"]["specs"][0][
                        "service_id"
                    ] = f"{self.nvme_metadata_pool}.{self.group}"
                    cfg["config"]["specs"][0]["spec"]["group"] = self.group
                else:
                    if LooseVersion(self.ceph_version) >= LooseVersion("20.2.1"):
                        cfg["config"]["args"].update({"group": self.group})
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
            pos_args = [self.nvme_metadata_pool]
            # group name is optional in 7.x so ignore it in that case
            if self.group is not None:
                pos_args.append(self.group)
            cfg = {
                "no_cluster_state": False,
                "config": {
                    "command": "apply",
                    "service": "nvmeof",
                    "args": {
                        "placement": self._get_placement_config(
                            self.config, self.gw_nodes
                        )
                    },
                    "pos_args": pos_args,
                },
            }

            if LooseVersion(self.ceph_version) >= LooseVersion("20.2.1"):
                cfg["config"]["args"].update({"group": self.group})
                # Delete pos_args key from cfg
                cfg["config"].pop("pos_args")

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

    def deploy(self):
        """
        Deploy NVMe gateways using orchestrator, then fetch and update daemon and service names for each gateway node.
        """
        # Open up firewall ports if running.
        setup_firewalld(self.gw_nodes)
        # Enable ceph mgr module enable nvmeof if not enabled
        check_and_enable_nvmeof_module(
            ceph_cluster=self.ceph_cluster, ceph_version=self.ceph_version
        )
        deploy_config = self._create_spec_deployment_config()
        if deploy_config:
            test_nvmeof.run(self.ceph_cluster, **deploy_config)

        # Once the service is deployed, get the service name and service id and store it
        ceph = Orch(self.ceph_cluster, **{})
        cmd = "ceph orch ls nvmeof --format json"
        out, _ = ceph.shell(args=[cmd])
        services = json.loads(out)
        self.service_name = None
        self.service_id = None
        for service in services:
            # If we have multiple services in single cluster then we need to filter the service by group
            # so that we will get the correct service name and service id for the group.
            # when we take services[0]["service_name"] only first service name will be returned
            # so we need to filter the service by group.
            if "nvmeof" in service["service_name"]:
                if self.group:
                    if self.group in service["service_name"]:
                        service_name = service["service_name"]
                        service_id = service["service_id"]
                        LOG.info(
                            f"Service name: {service_name}, Service id: {service_id}"
                        )
                        self.service_name = service_name
                        self.service_id = service_id
                        break
                else:
                    service_name = service["service_name"]
                    service_id = service["service_id"]
                    LOG.info(f"Service name: {service_name}, Service id: {service_id}")
                    self.service_name = service_name
                    self.service_id = service_id
                    break

    def resolve_nvmeof_service(self):
        """Set service_name/service_id from ``ceph orch ls nvmeof`` if unset."""
        if getattr(self, "service_name", None):
            return self.service_name
        orch = Orch(self.ceph_cluster, **{})
        out, _ = orch.shell(args=["ceph orch ls nvmeof --format json"])
        services = json.loads(out) if out else []
        for service in services:
            if "nvmeof" not in service.get("service_name", ""):
                continue
            if self.group and self.group not in service["service_name"]:
                continue
            self.service_name = service["service_name"]
            self.service_id = service.get("service_id")
            LOG.info(
                "Resolved NVMeoF service_name=%s service_id=%s",
                self.service_name,
                self.service_id,
            )
            return self.service_name
        return None

    def _exported_nvmeof_spec(self, orch):
        """Return an orch-apply-safe spec dict for this gateway group."""
        self.resolve_nvmeof_service()
        if not getattr(self, "service_name", None):
            raise RuntimeError("NVMe-oF service name not set; deploy the service first")
        out, _ = orch.shell(
            args=[
                f"ceph orch ls --export --service-name {self.service_name} -f yaml"
            ]
        )
        docs = [doc for doc in yaml.safe_load_all(out or "") if doc]
        if not docs:
            raise RuntimeError(
                f"Empty orch export for NVMeoF service {self.service_name}"
            )
        doc = docs[0]
        apply_spec = {
            key: doc[key]
            for key in ("service_type", "service_id", "placement", "spec")
            if key in doc
        }
        apply_spec.setdefault("service_type", "nvmeof")
        apply_spec.setdefault("spec", {})
        return apply_spec

    def ensure_namespace_limits(
        self,
        max_namespaces=4096,
        max_namespaces_per_subsystem=512,
        max_namespaces_with_netmask=4096,
    ):
        """Raise group-wide NVMeoF spec limits when they are below the targets.

        ``max_namespaces`` here is the gateway-group total (up to 4096), not the
        per-subsystem ``--max-namespaces`` used at subsystem add.

        Returns True when the spec was applied (daemons are expected to restart).
        """
        orch = Orch(self.ceph_cluster, **{})
        apply_spec = self._exported_nvmeof_spec(orch)
        spec = apply_spec.setdefault("spec", {})
        updates = {
            "max_namespaces": int(max_namespaces),
            "max_namespaces_per_subsystem": int(max_namespaces_per_subsystem),
            "max_namespaces_with_netmask": int(max_namespaces_with_netmask),
        }
        changed = []
        for key, value in updates.items():
            current = int(spec.get(key) or 0)
            if current >= value:
                LOG.info(
                    "NVMeoF spec %s=%s already meets target %s",
                    key,
                    current,
                    value,
                )
                continue
            LOG.info("Updating NVMeoF spec %s %s -> %s", key, current, value)
            spec[key] = value
            changed.append(f"{key}:{current}->{value}")
        if not changed:
            LOG.info(
                "NVMeoF spec already has group limits "
                "max_namespaces=%s max_namespaces_per_subsystem=%s "
                "max_namespaces_with_netmask=%s",
                spec.get("max_namespaces"),
                spec.get("max_namespaces_per_subsystem"),
                spec.get("max_namespaces_with_netmask"),
            )
            return False

        path = "/tmp/cephci_nvmeof_ns_limits.yaml"
        content = yaml.safe_dump(apply_spec, sort_keys=False)
        spec_file = orch.installer.remote_file(
            sudo=True, file_name=path, file_mode="w"
        )
        spec_file.write(content)
        spec_file.flush()
        LOG.info(
            "Applying NVMeoF namespace limits on %s: %s",
            self.service_name,
            ", ".join(changed),
        )
        orch.shell(
            args=["ceph", "orch", "apply", "-i", path],
            base_cmd_args={"mount": "/tmp:/tmp"},
        )
        return True

    def ensure_rebalance_period(self, period_sec):
        """Set ``spec.rebalance_period_sec`` on the live NVMeoF service.

        ``ceph orch apply`` records the spec. Redeploy only if the exported
        spec still does not show the requested period after apply.

        Returns True when daemons were redeployed.
        """
        period_sec = int(period_sec)
        orch = Orch(self.ceph_cluster, **{})
        apply_spec = self._exported_nvmeof_spec(orch)
        spec = apply_spec.setdefault("spec", {})
        current = spec.get("rebalance_period_sec")
        try:
            current_int = int(current) if current is not None else None
        except (TypeError, ValueError):
            current_int = None
        if current_int == period_sec:
            LOG.info(
                "NVMeoF spec %s already has rebalance_period_sec=%s",
                self.service_name,
                period_sec,
            )
            return False

        spec["rebalance_period_sec"] = period_sec
        path = "/tmp/cephci_nvmeof_rebalance.yaml"
        content = yaml.safe_dump(apply_spec, sort_keys=False)
        spec_file = orch.installer.remote_file(
            sudo=True, file_name=path, file_mode="w"
        )
        spec_file.write(content)
        spec_file.flush()
        LOG.info(
            "Applying NVMeoF rebalance_period_sec=%s on %s",
            period_sec,
            self.service_name,
        )
        orch.shell(
            args=["ceph", "orch", "apply", "-i", path],
            base_cmd_args={"mount": "/tmp:/tmp"},
        )
        verify = self._exported_nvmeof_spec(orch).setdefault("spec", {})
        try:
            applied = int(verify.get("rebalance_period_sec"))
        except (TypeError, ValueError):
            applied = None
        if applied == period_sec:
            LOG.info(
                "rebalance_period_sec=%s visible in orch spec without redeploy",
                period_sec,
            )
            return False
        LOG.warning(
            "orch apply did not persist rebalance_period_sec=%s (saw %s); redeploying",
            period_sec,
            applied,
        )
        self.redeploy(wait_sec=0)
        return True

    def ensure_encryption_key(self, dest_path="/root/encryption.key"):
        """Enable NVMeoF spec encryption so DHCHAP in-band auth can run.

        Existing BYOK services are deployed without the gateway encryption
        PSK. ``subsystem change_key`` / host DHCHAP need that key, which is
        independent of RBD LUKS/KMIP.

        Tentacle DHCHAP deploy copies a PEM to each gateway host and sets
        ``enable_encryption`` + ``encryption_key_path``. ``ceph orch apply``
        only records the spec ("Scheduled update"); it does not bounce
        daemons. This method therefore ``ceph orch redeploy`` afterwards.

        Returns True when the spec was applied and redeploy was issued.
        """
        orch = Orch(self.ceph_cluster, **{})
        apply_spec = self._exported_nvmeof_spec(orch)
        spec = apply_spec.setdefault("spec", {})
        if spec.get("enable_encryption") and spec.get("encryption_key_path"):
            LOG.info(
                "NVMeoF spec %s already has enable_encryption + "
                "encryption_key_path; skip apply",
                self.service_name,
            )
            return False

        installer = orch.installer
        key_file = dest_path
        last_err = None
        for bits in (512, 2048):
            try:
                installer.exec_command(
                    cmd=(
                        f"openssl req -newkey rsa:{bits} -noenc -noout "
                        f"-keyout {key_file} -batch 2>/dev/null"
                    ),
                    sudo=True,
                )
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                LOG.warning("openssl rsa:%s keygen failed: %s", bits, exc)
        if last_err:
            raise RuntimeError(
                f"Could not generate NVMeoF encryption key on installer: {last_err}"
            )
        key, _ = installer.exec_command(cmd=f"cat {key_file}", sudo=True)
        key = (key or "").strip()
        if not key:
            raise RuntimeError(f"Empty encryption key from {key_file}")
        if not key.endswith("\n"):
            key += "\n"

        for node in self.gw_nodes:
            LOG.info("Writing NVMeoF encryption key to %s:%s", node.hostname, dest_path)
            key_fh = node.remote_file(
                sudo=True, file_name=dest_path, file_mode="w"
            )
            key_fh.write(key)
            key_fh.flush()

        spec.pop("encryption_key", None)
        spec["enable_encryption"] = True
        spec["encryption_key_path"] = dest_path

        path = "/tmp/cephci_nvmeof_encryption.yaml"
        content = yaml.safe_dump(apply_spec, sort_keys=False)
        spec_file = orch.installer.remote_file(
            sudo=True, file_name=path, file_mode="w"
        )
        spec_file.write(content)
        spec_file.flush()
        LOG.info(
            "Applying NVMeoF enable_encryption + encryption_key_path=%s on %s",
            dest_path,
            self.service_name,
        )
        out, _ = orch.shell(
            args=["ceph", "orch", "apply", "-i", path],
            base_cmd_args={"mount": "/tmp:/tmp"},
        )
        LOG.info(
            "orch apply returned %r; this does not restart daemons, redeploying",
            (out or "").strip(),
        )
        self.redeploy(wait_sec=0)
        return True

    def wait_for_gateways(self, tries=18, delay=10):
        """Re-init gateway objects and wait until each daemon reports ready."""
        self.init_gateways()
        for gateway in self.gateways:
            gateway.load_gateway_info(tries=tries, delay=delay)
        return self.gateways

    def redeploy(self, wait_sec=30):
        """Redeploy the NVMe-oF orchestrator service after spec apply."""
        self.resolve_nvmeof_service()
        if not getattr(self, "service_name", None):
            raise RuntimeError("NVMe-oF service name not set; deploy the service first")
        orch = Orch(self.ceph_cluster, **{})
        cmd = f"ceph orch redeploy {self.service_name}"
        LOG.info("Redeploying NVMe-oF service: %s", cmd)
        orch.shell(args=[cmd])
        if wait_sec:
            time.sleep(wait_sec)

    def restart(self, wait_sec=30):
        """Restart all NVMe-oF daemons in this gateway group."""
        self.resolve_nvmeof_service()
        if not getattr(self, "service_name", None):
            raise RuntimeError("NVMe-oF service name not set; deploy the service first")
        orch = Orch(self.ceph_cluster, **{})
        cmd = f"ceph orch restart {self.service_name}"
        LOG.info("Restarting NVMe-oF service: %s", cmd)
        orch.shell(args=[cmd])
        if wait_sec:
            time.sleep(wait_sec)

    def restart_daemon(self, gateway, wait_sec=5):
        """Restart one NVMe-oF gateway daemon via ``ceph orch daemon restart``."""
        if not gateway.daemon_name:
            gateway.load_gateway_info()
        daemon = gateway.daemon_name
        if not daemon:
            raise RuntimeError(
                f"No daemon name for gateway {gateway.node.hostname}"
            )
        orch = Orch(self.ceph_cluster, **{})
        cmd = f"ceph orch daemon restart {daemon}"
        LOG.info(
            "Restarting NVMe-oF daemon %s on %s",
            daemon,
            gateway.node.hostname,
        )
        orch.shell(args=[cmd])
        if wait_sec:
            time.sleep(wait_sec)

    def init_gateways(self):
        """
        Initialize NVMeGateway objects for each ceph_node in the group.
        """
        self.gateways = []
        port = getattr(self, "port", DEFAULT_PORT)

        ceph = Orch(self.ceph_cluster, **{})

        for node in self.gw_nodes:
            self.gateways.append(
                create_gateway(
                    nvme_gw_cli_version_adapter(self.ceph_cluster),
                    node,
                    mtls=self.mtls,
                    shell=getattr(ceph, "shell"),
                    port=port,
                    gw_group=self.group,
                )
            )
