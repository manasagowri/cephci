"""KMIP (dummy server) helpers for NVMeoF BYOK tests.

Deploys ``quay.io/gdidi/kmip`` server/CLI containers on KMIP nodes, copies
TLS certs onto gateway hosts (and into the nvmeof container), and creates
named passphrases whose UUIDs are used as ``--key-id`` values.
"""

import json
import os
import re
import shutil
import tempfile
import time

from ceph.parallel import parallel
from cli.utilities.utils import get_running_containers
from utility.log import Log
from utility.systemctl import SystemCtl

LOG = Log(__name__)

DEFAULT_KMIP_IMAGE = "quay.io/gdidi/kmip/kmip-server:latest"
DEFAULT_KMIP_CLI_IMAGE = "quay.io/gdidi/kmip/kmip-cli:latest"
KMIP_CONTAINER_NAME = "kmip-server"
KMIP_PORT = 5696
CERT_FILES = ("ca_cert.pem", "client_cert.pem", "client_key.pem")
HOST_CERT_DIR = "/tmp/kmip-certs"
KMIP_DATA_DIR = "/var/lib/kmip-data"
KMIP_DATA_CERTS = f"{KMIP_DATA_DIR}/certs"
GW_KMIP_ROOT = "/etc/kmip"
CONTAINER_KMIP_CERT_DIR = "/certs/kmip"

PASSPHRASE_KEYS = ("parent_luks1", "parent_luks2", "clone")
CLONE_PASSPHRASE_NAME = "mypass_clone"
CLONE_PASSPHRASE_VALUE = "passwd"


def secret_log_id(value):
    """Return a loggable secret id. LogFilter redacts the token after ``passwd``."""
    text = str(value or "")
    if text == CLONE_PASSPHRASE_VALUE:
        return "shared"
    if text.startswith("passwd_"):
        return text[len("passwd_") :]
    return text


def short_hostname(node):
    """Return short hostname (tala002), not FQDN."""
    return getattr(node, "shortname", None) or str(node.hostname).split(".")[0]


def kmip_server_name(node):
    """KMIP endpoint name used by NVMeoF, e.g. tala002-kmip."""
    return f"{short_hostname(node)}-kmip"


def parse_kmip_list_uuids(output):
    """Parse uuid strings from ``kmip-cli list`` text or JSON."""
    text = str(output or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            uuids = data.get("uuids") or []
            return [str(item).strip() for item in uuids if item is not None]
    except (json.JSONDecodeError, TypeError):
        pass
    return [
        item.strip().strip("\",'")
        for item in re.findall(r"uuid:\s*(\S+)", text, re.I)
    ]


def parse_kmip_get_value(output):
    """Parse passphrase plaintext from ``kmip-cli get --uuid`` output."""
    text = str(output or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            value = data.get("value") or data.get("value_preview")
            if value is not None:
                return str(value)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"value_preview:\s*(.*)$", text, re.I | re.M)
    if match:
        return match.group(1).strip()
    match = re.search(r"value:\s*(.*)$", text, re.I | re.M)
    if match:
        return match.group(1).strip()
    return None


def parse_kmip_info_name(output):
    """Parse KMIP object name from ``kmip-cli info --uuid`` output."""
    text = str(output or "")
    match = re.search(r"NameValue\(value='([^']+)'\)", text)
    if match:
        return match.group(1)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            name = data.get("name")
            if name:
                return str(name)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def parse_kmip_uuid(output):
    """Parse uuid from kmip-cli create-passphrase stdout."""
    if not output:
        raise ValueError("Empty create-passphrase output")
    text = str(output).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            uuid = data.get("uuid") or data.get("id") or data.get("unique_identifier")
            if uuid is not None:
                return str(uuid)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"uuid:\s*(\S+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip("\",'")
    raise ValueError(f"Could not parse KMIP uuid from: {text}")


def kmip_cli(
    node, args, cli_image=DEFAULT_KMIP_CLI_IMAGE, hostname=None, check_ec=True
):
    """Run kmip-cli in a one-shot podman container (no bash alias).

    Args:
        node: CephNode hosting the KMIP server.
        args: CLI argv string, e.g. ``create-passphrase --name x --value y``.
        cli_image: kmip-cli image.
        hostname: Optional ``--hostname`` for the KMIP server (defaults to
            this node's IP, which is localhost via ``--network host``).
        check_ec: Fail if the container exits non-zero. Set False for
            destroy of missing uuids.
    """
    host_arg = ""
    if hostname:
        host_arg = f"--hostname {hostname} "
    cmd = (
        f"podman run --rm --network host "
        f"-v {KMIP_DATA_CERTS}:/kmip/certs:ro,Z "
        f"{cli_image} {host_arg}{args}"
    )
    LOG.info(f"[{short_hostname(node)}] kmip-cli: {args}")
    out, err = node.exec_command(cmd=cmd, sudo=True, check_ec=check_ec)
    LOG.debug(f"kmip-cli stdout={out} stderr={err}")
    return out, err


def _wait_for_container(node, name, tries=12, delay=5):
    """Wait until a named podman container is running."""
    for attempt in range(1, tries + 1):
        out, _ = node.exec_command(
            cmd=f"podman ps --filter name={name} --format '{{{{.ID}}}}'",
            sudo=True,
            check_ec=False,
        )
        if out and out.strip():
            return out.strip().splitlines()[0]
        LOG.info(
            f"Waiting for {name} on {short_hostname(node)} "
            f"({attempt}/{tries})"
        )
        time.sleep(delay)
    raise RuntimeError(
        f"KMIP container {name} did not start on {short_hostname(node)}"
    )


def deploy_kmip_server(
    node,
    image=DEFAULT_KMIP_IMAGE,
    container_name=KMIP_CONTAINER_NAME,
):
    """Pull/run the dummy KMIP server with --network host. Return container id."""
    LOG.info(f"Deploying KMIP server on {short_hostname(node)}")
    node.exec_command(
        cmd=f"podman rm -f {container_name}",
        sudo=True,
        check_ec=False,
    )
    node.exec_command(
        cmd=f"podman run -d --network host --name {container_name} {image}",
        sudo=True,
    )
    return _wait_for_container(node, container_name)


def deploy_kmip_servers(nodes, image=DEFAULT_KMIP_IMAGE):
    """Deploy KMIP servers on all nodes in parallel. Return {node: container_id}."""
    results = {}

    def _deploy(n):
        results[n] = deploy_kmip_server(n, image=image)

    with parallel() as p:
        for node in nodes:
            p.spawn(_deploy, node)
    return results


def export_certs(node, container_id=None, container_name=KMIP_CONTAINER_NAME):
    """Copy TLS certs from the KMIP container to host paths used later."""
    if not container_id:
        container_id = _wait_for_container(node, container_name)
    node.exec_command(cmd=f"mkdir -p {HOST_CERT_DIR} {KMIP_DATA_DIR}", sudo=True)
    node.exec_command(cmd=f"rm -rf {KMIP_DATA_CERTS}", sudo=True)
    # Full certs dir for kmip-cli volume mount → /var/lib/kmip-data/certs
    node.exec_command(
        cmd=f"podman cp {container_id}:/kmip/certs {KMIP_DATA_DIR}/",
        sudo=True,
    )
    for cert in CERT_FILES:
        node.exec_command(
            cmd=f"podman cp {container_id}:/kmip/certs/{cert} {HOST_CERT_DIR}/",
            sudo=True,
        )
    node.exec_command(cmd=f"chmod 644 {HOST_CERT_DIR}/*.pem", sudo=True)
    _assert_unencrypted_client_key(node, f"{HOST_CERT_DIR}/client_key.pem")
    LOG.info(f"Exported KMIP certs on {short_hostname(node)}")
    return HOST_CERT_DIR


def _assert_unencrypted_client_key(node, key_path):
    """Fail if the KMIP client key is PKCS#8 encrypted.

    PyKMIP ``load_cert_chain`` has no passphrase, so an
    ``ENCRYPTED PRIVATE KEY`` file fails with ``[SSL] PEM lib``.
    The dummy KMIP image stores an unencrypted key in the container.
    """
    out, _ = node.exec_command(cmd=f"head -1 {key_path}", sudo=True)
    first = (out or "").strip()
    if "ENCRYPTED" not in first.upper():
        return
    raise RuntimeError(
        f"{key_path} is an encrypted PKCS#8 key ({first}). "
        "Copy the unencrypted key from the KMIP container: "
        "podman cp kmip-server:/kmip/certs/client_key.pem "
        "(header must be '-----BEGIN PRIVATE KEY-----')."
    )


def export_certs_all(nodes, container_ids=None):
    """Export certs on every KMIP node."""
    container_ids = container_ids or {}
    with parallel() as p:
        for node in nodes:
            p.spawn(export_certs, node, container_ids.get(node))


def distribute_certs(kmip_nodes, gw_nodes):
    """Copy each KMIP node's certs to /etc/kmip/<hostname>-kmip/ on every GW."""
    for kmip_node in kmip_nodes:
        server_name = kmip_server_name(kmip_node)
        local_dir = tempfile.mkdtemp(prefix=f"kmip-{server_name}-")
        try:
            for cert in CERT_FILES:
                src = f"{HOST_CERT_DIR}/{cert}"
                kmip_node.download_file(
                    src=src, dst=os.path.join(local_dir, cert), sudo=True
                )
            for gw in gw_nodes:
                dest_dir = f"{GW_KMIP_ROOT}/{server_name}"
                gw.exec_command(cmd=f"mkdir -p {dest_dir}", sudo=True)
                for cert in CERT_FILES:
                    gw.upload_file(
                        src=os.path.join(local_dir, cert),
                        dst=f"{dest_dir}/{cert}",
                        sudo=True,
                    )
                gw.exec_command(cmd=f"chmod 644 {dest_dir}/*.pem", sudo=True)
                LOG.info(
                    f"Copied {server_name} certs to {short_hostname(gw)}:{dest_dir}"
                )
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)


def _nvmeof_container_id(node):
    """Return the NVMeoF gateway container id on a GW node."""
    out, _ = get_running_containers(
        node,
        expr="name=nvmeof",
        format="{{.ID}}",
        sudo=True,
    )
    container_ids = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if not container_ids:
        raise RuntimeError(f"No NVMe-oF container found on {short_hostname(node)}")
    return container_ids[0]


def copy_certs_into_gateway_containers(gw_nodes):
    """Copy host /etc/kmip into the nvmeof container KMIP cert_dir.

    Default gateway config uses ``./certs/kmip/{server_name}`` which resolves
    to ``/certs/kmip/<name>`` when the container cwd is ``/``.
    """
    for gw in gw_nodes:
        ctr = _nvmeof_container_id(gw)
        gw.exec_command(
            cmd=f"podman exec {ctr} mkdir -p {CONTAINER_KMIP_CERT_DIR}",
            sudo=True,
            check_ec=False,
        )
        gw.exec_command(
            cmd=f"podman cp {GW_KMIP_ROOT}/. {ctr}:{CONTAINER_KMIP_CERT_DIR}/",
            sudo=True,
        )
        gw.exec_command(
            cmd=(
                f"podman exec {ctr} bash -c "
                f"'find {CONTAINER_KMIP_CERT_DIR} -name \"*.pem\" -exec chmod 644 {{}} +'"
            ),
            sudo=True,
            check_ec=False,
        )
        LOG.info(
            f"Copied {GW_KMIP_ROOT} into nvmeof container {ctr} "
            f"on {short_hostname(gw)}"
        )


def ensure_gw_kmip_certs(
    kmip_nodes, gw_nodes, container_ids=None, nvme_service=None
):
    """Export certs, copy to /etc/kmip on GWs, redeploy, then copy into containers."""
    export_certs_all(kmip_nodes, container_ids=container_ids)
    distribute_certs(kmip_nodes, gw_nodes)
    if nvme_service:
        LOG.info(
            "Redeploying NVMeoF gateways after copying KMIP certs to %s",
            GW_KMIP_ROOT,
        )
        nvme_service.redeploy(wait_sec=60)
        nvme_service.init_gateways()
        gw_nodes = [gw.node for gw in nvme_service.gateways]
    copy_certs_into_gateway_containers(gw_nodes)


def _passphrase_spec(node):
    """Named passphrase definitions for one KMIP node."""
    host = short_hostname(node)
    return {
        "parent_luks1": {
            "name": f"mypass_{host}_1",
            "value": f"passwd_{host}_1",
        },
        "parent_luks2": {
            "name": f"mypass_{host}_2",
            "value": f"passwd_{host}_2",
        },
        "clone": {
            "name": CLONE_PASSPHRASE_NAME,
            "value": CLONE_PASSPHRASE_VALUE,
        },
    }


def _create_one_passphrase(
    node,
    spec,
    cli_image=DEFAULT_KMIP_CLI_IMAGE,
    tries=10,
    delay=3,
):
    """Create one passphrase and return its uuid."""
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            out, _ = kmip_cli(
                node,
                f'create-passphrase --name "{spec["name"]}" '
                f'--value "{spec["value"]}"',
                cli_image=cli_image,
                hostname=node.ip_address,
            )
            return parse_kmip_uuid(out)
        except Exception as exc:
            last_err = exc
            LOG.warning(
                f"create-passphrase {spec['name']} attempt {attempt}/{tries} "
                f"failed: {exc}"
            )
            time.sleep(delay)
    raise RuntimeError(
        f"Failed to create passphrase {spec['name']} on "
        f"{short_hostname(node)}: {last_err}"
    )


def create_passphrases(
    node,
    cli_image=DEFAULT_KMIP_CLI_IMAGE,
    tries=10,
    delay=3,
):
    """Create parent and shared-clone passphrases on a KMIP node and return uuid mapping.

    Returns::
        {
          "parent_luks1": {"name", "value", "uuid"},
          ...
        }
    """
    specs = _passphrase_spec(node)
    created = {}
    for key, spec in specs.items():
        uuid = _create_one_passphrase(
            node, spec, cli_image=cli_image, tries=tries, delay=delay
        )
        created[key] = {**spec, "uuid": uuid}
        LOG.info(
            f"[{short_hostname(node)}] {key} name={spec['name']} uuid={uuid}"
        )

    out, _ = kmip_cli(node, "list", cli_image=cli_image, hostname=node.ip_address)
    listed = out or ""
    for spec in created.values():
        if spec["name"] not in listed and spec["uuid"] not in listed:
            raise RuntimeError(
                f"Passphrase {spec['name']} (uuid={spec['uuid']}) not found in "
                f"kmip-cli list on {short_hostname(node)}:\n{listed}"
            )
    return created


def create_passphrases_all(nodes, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Create passphrases on all KMIP nodes. Return {node: passphrase_dict}."""
    results = {}

    def _create(n):
        results[n] = create_passphrases(n, cli_image=cli_image)

    with parallel() as p:
        for node in nodes:
            p.spawn(_create, node)
    return results


def _kmip_objects(node, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Return KMIP objects as ``{uuid, name, value}``, newest uuid first."""
    objects = []
    for uuid in _list_kmip_uuids(node, cli_image=cli_image):
        value = _fetch_kmip_value(node, uuid, cli_image=cli_image)
        info_out, _ = kmip_cli(
            node,
            f"info --uuid {uuid} -o json",
            cli_image=cli_image,
            hostname=node.ip_address,
            check_ec=False,
        )
        name = parse_kmip_info_name(info_out)
        LOG.info(
            f"[{short_hostname(node)}] kmip uuid={uuid} name={name} "
            f"secret_id={secret_log_id(value)!r} secret_len={len(value or '')}"
        )
        objects.append({"uuid": str(uuid), "name": name, "value": value})
    return objects


def _find_kmip_uuid(objects, spec):
    """Return uuid whose name and value both match ``spec``.

    Dummy KMIP allows duplicate names (tala002 has ``mypass_clone`` at
    uuid 14 with a leftover secret and uuid 16 with ``passwd``). Value-only
    matching can also pick the wrong object (``mypass_tala002_clone``).
    List order is newest-first, so the first name+value hit is used.
    """
    for obj in objects:
        if obj.get("name") == spec["name"] and obj.get("value") == spec["value"]:
            return obj["uuid"]
    return None


def load_passphrases(node, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Reload passphrase UUIDs from an already-running dummy KMIP server.

    Map each spec by KMIP name and stored value. Do not assume sequential
    ids 1/2/3 or unique values: leftover objects share names or the
    ``passwd`` secret.
    """
    specs = _passphrase_spec(node)
    objects = _kmip_objects(node, cli_image=cli_image)
    if not objects:
        raise RuntimeError(
            f"{short_hostname(node)}: kmip-cli list returned no uuids"
        )

    loaded = {}
    missing = []
    for key, spec in specs.items():
        uuid = _find_kmip_uuid(objects, spec)
        if not uuid:
            missing.append(f"{key} name={spec['name']} value={spec['value']}")
            continue
        loaded[key] = {**spec, "uuid": uuid}
        LOG.info(
            f"[{short_hostname(node)}] {key} kmip_name={spec['name']} "
            f"uuid={uuid} secret_id={secret_log_id(spec['value'])}"
        )
    if missing:
        listed = [
            f"{obj['uuid']}:{obj.get('name')}:{secret_log_id(obj.get('value'))}"
            for obj in objects
        ]
        raise RuntimeError(
            f"{short_hostname(node)}: could not map KMIP uuids for "
            f"{missing}. listed={listed}"
        )
    extra = sorted(
        {obj["uuid"] for obj in objects} - {item["uuid"] for item in loaded.values()}
    )
    if extra:
        LOG.warning(
            f"[{short_hostname(node)}] extra KMIP uuids not used: {extra}"
        )
    return loaded


def load_passphrases_all(nodes, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Load existing passphrases on all KMIP nodes. Return {node: passphrase_dict}."""
    results = {}

    def _load(n):
        results[n] = load_passphrases(n, cli_image=cli_image)

    with parallel() as p:
        for node in nodes:
            p.spawn(_load, node)
    return results


def _list_kmip_uuids(node, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Return uuid strings from ``kmip-cli list``."""
    out, _ = kmip_cli(
        node, "list -o json", cli_image=cli_image, hostname=node.ip_address
    )
    uuids = parse_kmip_list_uuids(out)
    if not uuids:
        uuids = parse_kmip_list_uuids(
            kmip_cli(node, "list", cli_image=cli_image, hostname=node.ip_address)[0]
        )
    return [str(item) for item in uuids]


def _fetch_kmip_value(node, uuid, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Return stored passphrase plaintext for a uuid, or None."""
    get_out, _ = kmip_cli(
        node,
        f"get --uuid {uuid} -o json",
        cli_image=cli_image,
        hostname=node.ip_address,
    )
    value = parse_kmip_get_value(get_out)
    if value is None:
        get_out, _ = kmip_cli(
            node,
            f"get --uuid {uuid}",
            cli_image=cli_image,
            hostname=node.ip_address,
        )
        value = parse_kmip_get_value(get_out)
    return value


def ensure_clone_passphrase(node, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Return the shared clone passphrase ``mypass_clone`` / ``passwd``.

    Requires both the KMIP name and value so leftover objects such as
    ``mypass_tala002_clone`` or ``mypass_clone`` with an old secret are
    not selected as ``--key-id``.
    """
    spec = _passphrase_spec(node)["clone"]
    host = short_hostname(node)
    uuid = _find_kmip_uuid(_kmip_objects(node, cli_image=cli_image), spec)
    if uuid:
        LOG.info(
            f"[{host}] clone secret {spec['name']} already at uuid={uuid}"
        )
        return {**spec, "uuid": str(uuid)}
    uuid = _create_one_passphrase(node, spec, cli_image=cli_image)
    LOG.info(f"[{host}] created clone secret {spec['name']} uuid={uuid}")
    return {**spec, "uuid": uuid}


def ensure_clone_passphrases_all(nodes, cli_image=DEFAULT_KMIP_CLI_IMAGE):
    """Ensure shared clone passphrase exists on all KMIP nodes."""
    with parallel() as p:
        for node in nodes:
            p.spawn(ensure_clone_passphrase, node, cli_image)


def open_kmip_firewall(nodes, port=KMIP_PORT):
    """Open KMIP TCP port on nodes where firewalld is active."""
    for node in nodes:
        if not SystemCtl(node).is_active("firewalld"):
            LOG.info(f"firewalld inactive on {short_hostname(node)}; skip port {port}")
            continue
        node.exec_command(
            cmd=f"firewall-cmd --add-port={port}/tcp --permanent",
            sudo=True,
        )
        node.exec_command(cmd="firewall-cmd --reload", sudo=True)
        LOG.info(f"Opened {port}/tcp on {short_hostname(node)}")


def setup_kmip_infrastructure(
    kmip_nodes,
    gw_nodes,
    kmip_image=DEFAULT_KMIP_IMAGE,
    kmip_cli_image=DEFAULT_KMIP_CLI_IMAGE,
    kmip_port=KMIP_PORT,
    nvme_service=None,
    reuse_existing=False,
):
    """Full KMIP bring-up: servers, certs, gateway redeploy, passphrases, firewall.

    When ``reuse_existing`` is True, leave running KMIP containers and
    passphrases in place; recopy certs, redeploy gateways, and reload
    passphrase UUIDs.

    Returns (container_ids, passphrases) where passphrases maps node -> dict.
    """
    if reuse_existing:
        LOG.info("Reusing existing KMIP servers; not recreating containers")
        container_ids = {
            node: _wait_for_container(node, KMIP_CONTAINER_NAME) for node in kmip_nodes
        }
        ensure_gw_kmip_certs(
            kmip_nodes,
            gw_nodes,
            container_ids=container_ids,
            nvme_service=nvme_service,
        )
        passphrases = load_passphrases_all(kmip_nodes, cli_image=kmip_cli_image)
        return container_ids, passphrases

    container_ids = deploy_kmip_servers(kmip_nodes, image=kmip_image)
    ensure_gw_kmip_certs(
        kmip_nodes,
        gw_nodes,
        container_ids=container_ids,
        nvme_service=nvme_service,
    )
    passphrases = create_passphrases_all(kmip_nodes, cli_image=kmip_cli_image)
    open_kmip_firewall(kmip_nodes, port=kmip_port)
    return container_ids, passphrases
