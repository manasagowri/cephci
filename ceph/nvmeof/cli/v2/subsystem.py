from ceph.nvmeof.cli.v2.base_cli import BaseCLI

from .common import substitute_keys

KEY_MAP = {
    # TODO: Temporary solution until https://tracker.ceph.com/issues/72636
    #       is fixed.
    "force": "force=true",
}


class Subsystem:

    def __init__(self, base: BaseCLI) -> None:
        self.base = base
        self.name = "subsystem"

    def add(self, **kwargs):
        return self.base.run_nvme_cli(self.name, "add", **kwargs)

    def add_network(self, **kwargs):
        """Add a network mask for auto-listeners on the subsystem."""
        return self.base.run_nvme_cli(self.name, "add_network", **kwargs)

    def change_key(self, **kwargs):
        """Change DHCHAP key for subsystem."""
        return self.base.run_nvme_cli(self.name, "change_key", **kwargs)

    @substitute_keys(KEY_MAP)
    def delete(self, **kwargs):
        return self.base.run_nvme_cli(self.name, "del", **kwargs)

    def del_key(self, **kwargs):
        """Delete DHCHAP key from subsystem."""
        return self.base.run_nvme_cli(self.name, "del_key", **kwargs)

    def del_network(self, **kwargs):
        """Remove a network mask from the subsystem."""
        return self.base.run_nvme_cli(self.name, "del_network", **kwargs)

    def get(self, **kwargs):
        """Get subsystem details."""
        return self.base.run_nvme_cli(self.name, "get", **kwargs)

    def list(self, **kwargs):
        return self.base.run_nvme_cli(self.name, "list", **kwargs)

    def add_kmip_server_endpoint(self, **kwargs):
        """Add a KMIP server endpoint to a subsystem.

        Prefer ``positional_args`` of ``[nqn, name, address, port]`` to match
        ``ceph nvmeof subsystem add_kmip_server_endpoint <nqn> <name> <ip> <port>``.
        Flag form uses ``args`` keys ``nqn``/``subsystem``, ``name``, ``address``,
        ``port``.
        """
        return self.base.run_nvme_cli(self.name, "add_kmip_server_endpoint", **kwargs)

    def list_kmip_server_endpoints(self, **kwargs):
        """List KMIP server endpoints configured on a subsystem."""
        return self.base.run_nvme_cli(self.name, "list_kmip_server_endpoints", **kwargs)

    def del_kmip_server_endpoint(self, **kwargs):
        """Remove a KMIP server endpoint from a subsystem."""
        return self.base.run_nvme_cli(self.name, "del_kmip_server_endpoint", **kwargs)
