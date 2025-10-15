import json
from collections import defaultdict

from ceph.utils import get_node_by_id
from tests.nvmeof.workflows.initiator import NVMeInitiator
from utility.log import Log
from utility.retry import retry

LOG = Log(__name__)


def validate_init_namespace_masking(
    ceph_cluster,
    gateway,
    command,
    init_nodes,
    expected_visibility,
    validate_config=None,
):
    """Validate that the namespace visibility is correct from all initiators."""
    for node in init_nodes:
        initiator_node = get_node_by_id(ceph_cluster, node)
        client = NVMeInitiator(initiator_node)
        client.disconnect_all()  # Reconnect NVMe targets
        client.connect_targets(gateway, config={"nqn": "connect-all"})
        serial_to_namespace = defaultdict(set)

        out, _ = initiator_node.exec_command(
            sudo=True, cmd="cat /etc/os-release | grep VERSION_ID"
        )
        rhel_version = out.split("=")[1].strip().strip('"')

        @retry(
            IOError,
            tries=4,
            delay=3,
        )
        def execute_nvme_command(client_node):
            devices_json, _ = client_node.exec_command(
                cmd="nvme list --output-format=json", sudo=True
            )
            return json.loads(devices_json)["Devices"]

        devices_json = execute_nvme_command(initiator_node)
        if not devices_json:
            LOG.info(f"No devices found on node {node}")
            continue

        for device in devices_json:
            if rhel_version == "9.5":
                key = device["NameSpace"]
                value = int(device["SerialNumber"])
                serial_to_namespace[key].add(value)
            elif rhel_version == "9.6":
                for subsys in device.get("Subsystems", []):
                    for controller in subsys.get("Controllers", []):
                        if controller.get("ModelNumber") == "Ceph bdev Controller":
                            serial = controller.get("SerialNumber", "")
                            value = int(serial)
                            for ns in subsys.get("Namespaces", []):
                                key = ns.get("NSID")
                                serial_to_namespace[key].add(value)

        def subsystem_nsid_found(dictionary, key, value):
            return key in dictionary and value in dictionary[key]

        if validate_config:
            args = (validate_config or {}).get("args", {})
            subsystem_to_nsid = {args["nsid"]: args["sub_num"]}
            init_node = args.get("init_node")
            ns_to_check, subsystem_to_check = next(iter(subsystem_to_nsid.items()))
            LOG.info(f"{subsystem_to_nsid} : {serial_to_namespace}")
            ns_subsys_found = subsystem_nsid_found(
                serial_to_namespace, ns_to_check, subsystem_to_check
            )

            if command == "add_host":
                if node == init_node:
                    if ns_subsys_found:
                        LOG.info(
                            f"Validated - Namespace:Subsystem pair {subsystem_to_nsid} is listed on {node}"
                        )
                    else:
                        LOG.error(
                            f"Namespace:Subsystem pair {subsystem_to_nsid} is not listed on {node}"
                        )
                        raise Exception(
                            f"Expected Namespace:Subsystem pair {subsystem_to_nsid} on {node} but did not find it"
                        )
                else:
                    if ns_subsys_found:
                        LOG.error(
                            f"Namespace:Subsystem pair {subsystem_to_nsid} is listed on {node}"
                        )
                        raise Exception(
                            f"Did not expect Namespace:Subsystem pair {subsystem_to_nsid} on {node} but found it"
                        )
                    else:
                        LOG.info(
                            f"Validated - Namespace:Subsystem pair {subsystem_to_nsid} is not listed on {node}"
                        )
            elif command == "del_host":
                if node == init_node:
                    if ns_subsys_found:
                        LOG.error(
                            f"Namespace:Subsystem pair {subsystem_to_nsid} is listed on {node}"
                        )
                        raise Exception(
                            f"Did not expect Namespace:Subsystem pair {subsystem_to_nsid} on {node} but found it"
                        )
                    else:
                        LOG.info(
                            f"Validated - Namespace:Subsystem pair {subsystem_to_nsid} is not listed on {node}"
                        )
                else:
                    if ns_subsys_found:
                        LOG.error(
                            f"Namespace:Subsystem pair {subsystem_to_nsid} is listed on {node}"
                        )
                        raise Exception(
                            f"Did not expect Namespace:Subsystem pair {subsystem_to_nsid} on {node} but found it"
                        )
                    else:
                        LOG.info(
                            f"Validated - Namespace:Subsystem pair {subsystem_to_nsid} is not listed on {node}"
                        )
        else:
            if (
                not expected_visibility
            ):  # If expected visibility is False, devices should be empty
                # Determine if devices list is empty (no Namespaces in any Subsystem)
                devices_json_empty = (
                    all(
                        not subsys.get("Namespaces")  # True if empty or missing
                        for device in devices_json
                        for subsys in device.get("Subsystems", [])
                    )
                    if rhel_version == "9.6"
                    else not devices_json
                )
                if not devices_json_empty:  # Check if Devices is not empty
                    LOG.error(
                        f"Expected no devices for initiator {node}, but found: {devices_json}"
                    )
                    raise Exception(
                        f"Initiator {node} has devices when NS visibility is restricted"
                    )
                else:
                    LOG.info(f"Validated - no devices found on {node}")
            elif (
                expected_visibility
            ):  # If expected visibility is True, devices should not be empty
                devices_json_empty = (
                    all(
                        not subsys.get("Namespaces")  # True if empty or missing
                        for device in devices_json
                        for subsys in device.get("Subsystems", [])
                    )
                    if rhel_version == "9.6"
                    else not devices_json
                )
                if devices_json_empty:
                    LOG.error(
                        f"Expected devices to be visible for node {node}, but found none."
                    )
                    raise Exception(
                        f"Initiator {node} has no devices when NS visibility is restricted"
                    )
                else:
                    # Log Namespace and SerialNumber from each device
                    for device in devices_json:
                        if rhel_version == "9.5":
                            namespace = device.get("NameSpace", None)
                            serial_number = device.get("SerialNumber", None)
                        elif rhel_version == "9.6":
                            for subsys in device.get("Subsystems", []):
                                for controller in subsys.get("Controllers", []):
                                    if (
                                        controller.get("ModelNumber")
                                        == "Ceph bdev Controller"
                                    ):
                                        serial_number = int(
                                            controller.get("SerialNumber", "")
                                        )
                                        for ns in subsys.get("Namespaces", []):
                                            namespace = ns.get("NSID")
                                            LOG.info(
                                                f"Namespace: {namespace}, SerialNumber: {serial_number}"
                                            )
                    LOG.info(f"Validated - {len(devices_json)} devices found on {node}")


def validate_namespace_masking(
    nsid,
    subnqn,
    namespaces_sub,
    hostnqn_dict,
    ns_visibility,
    command,
    expected_visibility,
):
    """Validate that the namespace visibility is correct."""

    if command == "add_host":
        LOG.info(command)
        num_namespaces_per_node = namespaces_sub // len(hostnqn_dict)

        # Determine the initiator node responsible for this nsid based on the calculated range
        node_index = (nsid - 1) // num_namespaces_per_node
        expected_host = list(hostnqn_dict.values())[node_index]

        # Log the visibility of the namespace
        if expected_host in ns_visibility:
            LOG.info(
                f"Validated - Namespace {nsid} of {subnqn} has the correct nqn {ns_visibility}"
            )
        else:
            LOG.error(
                f"Namespace {nsid} of {subnqn} has incorrect NQN. Expected {expected_host}, but got {ns_visibility}"
            )
            raise Exception(
                f"Namespace {nsid} of {subnqn} has incorrect NQN. Expected {expected_host}, but got {ns_visibility}"
            )

    elif command == "del_host":
        LOG.info(command)
        num_namespaces_per_node = namespaces_sub // len(hostnqn_dict)

        # Determine the initiator node responsible for this nsid based on the calculated range
        node_index = (nsid - 1) // num_namespaces_per_node
        expected_host = list(hostnqn_dict.values())[node_index]

        # Log the visibility of the namespace
        if expected_host not in ns_visibility:
            LOG.info(
                f"Validated - Namespace {nsid} of {subnqn} does not has {expected_host}"
            )
        else:
            LOG.error(
                f"Namespace {nsid} of {subnqn} has {ns_visibility} which was removed"
            )
            raise Exception(
                f"Namespace {nsid} of {subnqn} has incorrect NQN. Not expecting {ns_visibility} in {expected_host}"
            )

    else:
        # Validate visibility based on the expected value (for non-add/del host commands)
        if str(ns_visibility).lower() == str(expected_visibility).lower():
            LOG.info(
                f"Validated - Namespace {nsid} has correct visibility: {ns_visibility}"
            )
        else:
            LOG.error(
                f"NS {nsid} of {subnqn} has wrong visibility.Expected {expected_visibility} got {ns_visibility}"
            )
            raise Exception(
                f"NS {nsid} of {subnqn} has wrong visibility.Expected {expected_visibility} got {ns_visibility}"
            )
