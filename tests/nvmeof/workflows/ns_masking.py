import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from ceph.utils import get_node_by_id
from tests.nvmeof.workflows.initiator import Initiator, NVMeInitiator
from utility.log import Log
from utility.retry import retry
from utility.utils import generate_unique_id

LOG = Log(__name__)

io_tasks = []
executor = None


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
        # ns_visibility = str(ns_visibility)
        LOG.info(command)
        # if ns_visibility.lower() == expected_visibility.lower():
        if ns_visibility == expected_visibility:
            LOG.info(
                f"Validated - Namespace {nsid} has correct visibility: {ns_visibility}"
            )
            LOG.error(
                f"NS {nsid} of {subnqn} has wrong visibility.Expected {expected_visibility} got{ns_visibility}"
            )
            raise Exception(
                f"NS {nsid} of {subnqn} has wrong visibility.Expected {expected_visibility} got {ns_visibility}"
            )


def validate_init_namespace_masking(
    nvme_service,
    command,
    init_nodes,
    expected_visibility,
    validate_config=None,
):
    """Validate that the namespace visibility is correct from all initiators."""
    for node in init_nodes:
        initiator_node = get_node_by_id(nvme_service.cluster, node)
        client = NVMeInitiator(initiator_node, nvme_service.gateways[0])
        client.disconnect_all()  # Reconnect NVMe targets
        client.connect_targets(config={"nqn": "connect-all"})
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


def execute_io_ns_masking(nvme_service, init_nodes, images, negative=False):
    """
    Execute IO on the namespaces after validating the namespace masking.
    """
    for node in init_nodes:
        initiator = Initiator(get_node_by_id(nvme_service.ceph_cluster, node))
        initiator.prepare_io_execution(
            [{"nqn": "connect-all", "listener_port": 5500, "node": node}]
        )
        if isinstance(images, dict):
            images_node = images.get(node, [])
        else:
            images_node = images
        num_devices = len(images_node)
        max_workers = num_devices if num_devices > 32 else 32
        executor = ThreadPoolExecutor(
            max_workers=max_workers,
        )
        init = [client for client in initiator.clients if client.node.id == node][0]
        io_tasks.append(executor.submit(init.start_fio))
        time.sleep(20)  # time sleep for IO to Kick-in

        initiator.validate_io(images_node, negative=negative)


def add_namespaces(config, command, init_nodes, hostnqn_dict, rbd_obj, nvme_service):
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    image_size = config["args"].pop("image_size")
    group = config["args"].pop("group", None)
    pool = config["args"].pop("pool")
    no_auto_visible = config["args"].pop("no-auto-visible")
    validate_initiators = config["args"].pop("validate_ns_masking_initiators", None)
    namespaces_sub = int(namespaces / subsystems)
    base_cmd_args = {"format": "json"}
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    expected_visibility = False if no_auto_visible else True
    nvmegwcli = nvme_service.gateways[0]
    rbd_images_subsys = []

    for sub_num in range(1, subsystems + 1):
        subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"
        name = generate_unique_id(length=4)
        LOG.info(f"Subsystem {sub_num}/{subsystems}")
        for num in range(1, namespaces_sub + 1):
            LOG.info(f"Namespace {num}/{namespaces_sub}")
            # Create the image
            rbd_obj.create_image(pool, f"{name}-image{num}", image_size)
            LOG.info(f"Creating image {name}-image{num}/{namespaces}")

            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "rbd-image": f"{name}-image{num}",
                    "rbd-pool": pool,
                    "subsystem": subnqn,
                },
            }
            if no_auto_visible:
                config["args"]["no-auto-visible"] = ""
                expected_visibility = False
            _, namespaces_response = nvmegwcli.namespace.add(**config)
            nsid = json.loads(namespaces_response)["nsid"]

            _config = {
                "base_cmd_args": base_cmd_args,
                "args": {
                    "nsid": nsid,
                    "subsystem": subnqn,
                },
            }

            _, namespace_response = nvmegwcli.namespace.list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0][
                "auto_visible"
            ]
            LOG.info(ns_visibility)
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )
            rbd_images_subsys.append(f"{subnqn}|{pool}|{name}-image{num}")
    if validate_initiators:
        validate_init_namespace_masking(
            nvme_service, command, init_nodes, expected_visibility
        )

    negative = not expected_visibility
    execute_io_ns_masking(
        nvme_service, init_nodes, rbd_images_subsys, negative=negative
    )
    return executor, io_tasks


def add_host(config, command, init_nodes, hostnqn_dict, nvme_service):
    """Add host NQN to namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    validate_initiators = config["args"].pop("validate_ns_masking_initiators", None)
    LOG.info(hostnqn_dict)
    expected_visibility = False
    nvmegwcli = nvme_service.gateways[0]
    namespaces_sub = int(namespaces / subsystems)
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    num_namespaces_per_initiator = namespaces_sub // len(hostnqn_dict)
    rbd_images_subsys = {}

    for sub_num in range(1, subsystems + 1):
        LOG.info(f"Subsystem {sub_num}/{subsystems}")
        for num in range(1, namespaces_sub + 1):
            LOG.info(f"Namespace {num}/{namespaces_sub}")
            config["args"].clear()
            subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"

            # Determine the correct host for the current namespace
            for i, node in enumerate(
                hostnqn_dict.keys(), start=1
            ):  # Iterate over hostnqn_dict # Calculate the range of namespaces this initiator should handle
                if num in range(
                    (i - 1) * num_namespaces_per_initiator + 1,
                    i * num_namespaces_per_initiator + 1,
                ):
                    host = node
                    LOG.info(host)
                    host_nqn = hostnqn_dict[node]
                    break

            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "host": host_nqn,
                    "subsystem": subnqn,
                },
            }

            nvmegwcli.namespace.add_host(**config)
            _config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "subsystem": subnqn,
                },
            }
            # list namespaces and check visibility
            _, namespace_response = nvmegwcli.namespace.list(**_config)
            ns_obj = json.loads(namespace_response)["namespaces"][0]
            if rbd_images_subsys.get(host):
                rbd_images_subsys[host].extend(
                    [f"{subnqn}|{ns_obj['rbd_pool_name']}|{ns_obj['rbd_image_name']}"]
                )
            else:
                rbd_images_subsys[host] = [
                    f"{subnqn}|{ns_obj['rbd_pool_name']}|{ns_obj['rbd_image_name']}"
                ]
            ns_visibility = ns_obj["hosts"]
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )
            if validate_initiators:
                validate_config = {
                    "args": {
                        "sub_num": sub_num,
                        "nsid": num,
                        "init_node": host,
                    },
                }
                validate_init_namespace_masking(
                    nvme_service,
                    command,
                    init_nodes,
                    expected_visibility,
                    validate_config,
                )

    execute_io_ns_masking(nvme_service, init_nodes, rbd_images_subsys, negative=False)
    return executor, io_tasks


def del_host(config, command, init_nodes, hostnqn_dict, nvme_service):
    """Delete host NQN to namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    validate_initiators = config["args"].pop("validate_ns_masking_initiators", None)
    LOG.info(hostnqn_dict)
    namespaces_sub = int(namespaces / subsystems)
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    num_namespaces_per_initiator = namespaces_sub // len(hostnqn_dict)
    nvmegwcli = nvme_service.gateways[0]
    rbd_images_subsys = {}

    for sub_num in range(1, subsystems + 1):
        LOG.info(f"Subsystem {sub_num}/{subsystems}")
        for num in range(1, namespaces_sub + 1):
            LOG.info(f"Namespace {num}/{namespaces_sub}")
            config["args"].clear()
            subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"

            # Determine the correct host for the current namespace
            for i, node in enumerate(hostnqn_dict.keys(), start=1):
                # Iterate over hostnqn_dict # Calculate the range of namespaces this initiator should handle
                if num in range(
                    (i - 1) * num_namespaces_per_initiator + 1,
                    i * num_namespaces_per_initiator + 1,
                ):
                    host = node
                    LOG.info(host)
                    host_nqn = hostnqn_dict[node]
                    break

            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "host": host_nqn,
                    "subsystem": subnqn,
                },
            }
            nvmegwcli.namespace.del_host(**config)

            _config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "subsystem": subnqn,
                },
            }
            # list namespaces and check visibility
            _, namespace_response = nvmegwcli.namespace.list(**_config)
            ns_obj = json.loads(namespace_response)["namespaces"][0]
            if rbd_images_subsys.get(host):
                rbd_images_subsys[host].extend(
                    [f"{subnqn}|{ns_obj['rbd_pool_name']}|{ns_obj['rbd_image_name']}"]
                )
            else:
                rbd_images_subsys[host] = [
                    f"{subnqn}|{ns_obj['rbd_pool_name']}|{ns_obj['rbd_image_name']}"
                ]
            ns_visibility = ns_obj["hosts"]
            expected_visibility = []
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )

            if validate_initiators:
                validate_config = {
                    "args": {
                        "sub_num": sub_num,
                        "nsid": num,
                        "init_node": host,
                    },
                }
                validate_init_namespace_masking(
                    nvme_service,
                    command,
                    init_nodes,
                    expected_visibility,
                    validate_config,
                )

    execute_io_ns_masking(nvme_service, init_nodes, rbd_images_subsys, negative=True)
    return executor, io_tasks


def change_visibility(config, command, init_nodes, hostnqn_dict, nvme_service):
    """Change visibility of namespaces."""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    validate_initiators = config["args"].pop("validate_ns_masking_initiators", None)
    auto_visible = config["args"].pop("auto-visible")
    namespaces_sub = int(namespaces / subsystems)
    nvmegwcli = nvme_service.gateways[0]
    base_cmd_args = {"format": "json"}
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    expected_visibility = True if auto_visible == "yes" else False
    rbd_images_subsys = []

    for sub_num in range(1, subsystems + 1):
        subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"
        LOG.info(f"Subsystem {sub_num}/{subsystems}")
        for num in range(1, namespaces_sub + 1):
            LOG.info(f"Namespace {num}/{namespaces_sub}")
            config["args"].clear()
            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "subsystem": subnqn,
                    "auto-visible": auto_visible,
                    "force": "",
                },
            }
            nvmegwcli.namespace.change_visibility(**config)
            # Listing namespace and checking visibility
            _config = {
                "base_cmd_args": base_cmd_args,
                "args": {"nsid": num, "subsystem": subnqn},
            }
            _, namespace_response = nvmegwcli.namespace.list(**_config)
            ns_obj = json.loads(namespace_response)["namespaces"][0]
            rbd_images_subsys.append(
                f"{subnqn}|{ns_obj['rbd_pool_name']}|{ns_obj['rbd_image_name']}"
            )
            ns_visibility = ns_obj["auto_visible"]

            # Validate the visibility of the namespace
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )
    if validate_initiators:
        validate_init_namespace_masking(
            nvme_service, command, init_nodes, expected_visibility
        )

    negative = not expected_visibility
    execute_io_ns_masking(
        nvme_service, init_nodes, rbd_images_subsys, negative=negative
    )
    return executor, io_tasks
