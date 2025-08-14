from ceph.nvmegw_cli import NVMeGWCLI
from ceph.utils import get_node_by_id
from utility.utils import get_ceph_version_from_cluster


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
        clients,
        gateway_daemon_name: str,
        mtls: bool = False,
        port: int = 5500,
    ):
        self.ceph_node = get_node_by_id(ceph_cluster, ceph_node)
        self.ceph_cluster = ceph_cluster
        self.gateway_daemon_name = gateway_daemon_name
        self.mtls = mtls
        self.port = port
        self.cli = None
        self.clients = clients
        self._version = get_ceph_version_from_cluster(clients[0])

    @property
    def nvmegwcli(self):
        current_ceph_version = get_ceph_version_from_cluster(self.clients[0])
        if self.cli is None or self._version != current_ceph_version:
            self.cli = (
                NVMeGWCLI(
                    self.ceph_node,
                    port=self.port,
                    mtls=self.mtls,  # change this to new cli class for ceph >= 9.0
                )
                if current_ceph_version >= "20.0"
                else NVMeGWCLI(self.ceph_node, port=self.port, mtls=self.mtls)
            )
            self._version = current_ceph_version
        return self.cli
