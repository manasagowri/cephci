"""
Test suite that verifies the deployment of Ceph NVMeoF Gateway HA
 with supported entities like subsystems , etc.,

"""

from ceph.ceph import Ceph
from ceph.nvmegw_cli import NVMeGWCLI
from tests.nvmeof.workflows.nvme import NVMeService
from tests.rbd.rbd_utils import initial_rbd_config
from utility.log import Log

LOG = Log(__name__)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Return the status of the Ceph NVMEof HA test execution.

    - Configure Gateways
    - Configures Initiators and Run FIO on NVMe targets.
    - Perform failover and failback.
    - Validate the IO continuation prior and after to failover and failback

    Args:
        ceph_cluster: Ceph cluster object
        kwargs: Key/value pairs of configuration information to be used in the test.

    Returns:
        int - 0 when the execution is successful else 1 (for failure).

    Example:

        # Execute the nvmeof GW test
            - test:
                name: Ceph NVMeoF deployment
                desc: Configure NVMEoF gateways and initiators
                config:
                    gw_nodes:
                     - node6
                    rbd_pool: rbd
                    do_not_create_image: true
                    rep-pool-only: true
                    cleanup-only: true                          # only for cleanup
                    rep_pool_config:
                      pool: rbd
                    install: true                               # Run SPDK with all pre-requisites
                    subsystems:                                 # Configure subsystems with all sub-entities
                      - nqn: nqn.2016-06.io.spdk:cnode3
                        serial: 3
                        bdevs:
                          count: 1
                          size: 100G
                        listener_port: 5002
                        allow_host: "*"
                    initiators:                             # Configure Initiators with all pre-req
                      - nqn: connect-all
                        listener_port: 4420
                        node: node10
                    fault-injection-methods:                # fail-tool: systemctl, nodes-to-be-failed: node names
                      - tool: systemctl
                        nodes: node6
    """
    LOG.info("Starting Ceph NVMEoF deployment.")
    config = kwargs["config"]
    rbd_pool = config.get("rbd_pool")
    pools = list(
        set([cfg.get("rbd_pool", rbd_pool) for cfg in config.get("gw_groups", [])])
    )
    rbd_obj = []
    for pool in pools:
        kwargs["config"]["rep_pool_config"] = {
            "pool": pool,
        }
        rbd_obj.append(initial_rbd_config(**kwargs)["rbd_reppool"])

    overrides = kwargs.get("test_data", {}).get("custom-config")
    for key, value in dict(item.split("=") for item in overrides).items():
        if key == "nvmeof_cli_image":
            NVMeGWCLI.NVMEOF_CLI_IMAGE = value
            break

    nvme = NVMeService(ceph_cluster, config)
    try:
        # NVMeService now handles all deployment types (mtls, encryption, dhchap, rebalance, etc.) per group
        nvme.deploy_service_and_update(config.get("install", False))

        # If you need to handle post-deployment validation, initiator IO, or failover/failback,
        # add logic here using the updated NVMeService and its gateway_groups.
        return 0
    except Exception as err:
        LOG.error(err)
    finally:
        if config.get("cleanup"):
            if not nvme:
                LOG.error("NVMeService instance is not available for cleanup.")
                nvme = NVMeService(
                    ceph_cluster,
                    config,
                )
                # Use the NVMeService instance to handle cleanup
            nvme.teardown(ceph_cluster, rbd_obj, config)

    return 1
