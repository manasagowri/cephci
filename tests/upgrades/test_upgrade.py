import logging
import os

import yaml

from tests.ceph_ansible import switch_rpm_to_container, test_ansible_upgrade
from tests.ceph_installer import test_ansible, test_cephadm
from tests.cephadm import test_cephadm_upgrade

log = logging.getLogger(__name__)


def load_file(file_name):
    """Retrieve yaml data content from file."""
    file_path = os.path.abspath(file_name)
    with open(file_path, "r") as conf_:
        content = yaml.safe_load(conf_)
    return content


def get_ansible_conf(config, version, is_repo_present, is_ceph_conf_present):
    """

    Fetch ansible conf for the version specified
    Args:
        config: configuration specified in suite file
        version: version for which config needs to be fetched
        is_repo_present: True if repo and image information are specified by user in CLI
        is_ceph_conf_present: True if ceph_origin and related confs are specified by user in suite config

    Returns:
        ansible configuration for install or upgrade
    """
    # try setting required config to a new variable and use a new variable in the calling method as well
    # pass the ansi_config and config parameters correctly from test suite

    install_config = config.get("paths").get(version).get("config")
    # if config.get("paths").get(version).get("config").get("use_cdn"):
    #     config["use_cdn"] = config.get("paths").get(version).get("config").get("use_cdn")
    #     config["build"] = config.get("paths").get(version).get("config").get("build")
    if not is_ceph_conf_present:
        install_config["ansi_config"]["ceph_origin"] = "distro"
        install_config["ansi_config"]["ceph_repository"] = "rhcs"
        install_config["ansi_config"]["ceph_rhcs_version"] = int(version)

    release_info = load_file("release.yaml")

    if install_config.get("use_cdn"):
        install_config["ansi_config"]["ceph_origin"] = "repository"

    if 3 <= version < 4:
        install_config["rhbuild"] = str(version)
        return install_config

    platform = config["rhbuild"].split("-", 1)[1]
    install_config["rhbuild"] = "-".join([str(version), platform])

    if not is_repo_present:
        install_config["base_url"] = release_info["releases"][version]["composes"][platform]
        container_image = release_info["releases"][version]["image"]["ceph"]
    else:
        install_config["base_url"] = config["base_url"]
        container_image = config["container_image"]

    install_config["container_image"] = container_image
    install_config["ansi_config"]["ceph_docker_registry"] = container_image.split("/", 1)[0]
    install_config["ansi_config"]["ceph_docker_image"] = container_image.split("/", 1)[1].split(
        ":"
    )[0]
    install_config["ansi_config"]["ceph_docker_image_tag"] = container_image.split("/", 1)[
        1
    ].split(":")[1]
    install_config["ansi_config"]["node_exporter_container_image"] = release_info["releases"][
        version
    ]["image"]["nodeexporter"]
    install_config["ansi_config"]["grafana_container_image"] = release_info["releases"][
        version
    ]["image"]["grafana"]
    install_config["ansi_config"]["prometheus_container_image"] = release_info["releases"][
        version
    ]["image"]["prometheus"]
    install_config["ansi_config"]["alertmanager_container_image"] = release_info["releases"][
        version
    ]["image"]["alertmanager"]

    return install_config


def get_cephadm_upgrade_config(config, version):
    """

    Fetch configuration for cephadm upgrade
    Args:
        config: configuration specified in suite file
        version: version to which upgrade is to be done

    Returns:
        config required for cephadm upgrade
    """
    config["command"] = "start"
    config["service"] = "upgrade"
    config["base_cmd_args"] = {"verbose": True}
    config["benchmark"] = {
        "type": "rados",
        "pool_per_client": True,
        "pg_num": 128,
        "duration": 10,
    }
    config["verify_cluster_health"] = True
    release_info = load_file("release.yaml")
    config["container_image"] = release_info["releases"][version]["image"]["ceph"]
    return config


def run(ceph_cluster, **kw):
    """
    Runs ceph-ansible and cephadm deployment and upgrade according to the path specified
    Args:
        ceph_cluster (ceph.ceph.Ceph): Ceph cluster object
    """
    log.info("Running test")
    log.info("Running ceph upgrade test")
    config = kw.get("config")
    paths = config.get("paths")
    versions = list(paths)
    install_version = versions[0]
    upgrade_versions = versions[1:]
    ceph_cluster_dict = kw.get("ceph_cluster_dict")

    for cluster_name, cluster in ceph_cluster_dict.items():

        if install_version >= 5.0:
            config["steps"] = config["suite_setup"]["steps"]
            rc = test_cephadm.run(
                ceph_cluster=ceph_cluster_dict[cluster_name],
                ceph_nodes=ceph_cluster_dict[cluster_name],
                config=config,
                test_data=kw.get("test_data"),
                ceph_cluster_dict=ceph_cluster_dict,
                clients=kw.get("clients"),
            )
            if rc != 0:
                return rc
        else:
            is_ceph_conf_present = (
                True if config.get("paths").get(install_version).get("config").get("ceph_origin") else False
            )
            is_repo_present = True if config.get("container_image") else False
            install_config = get_ansible_conf(
                config, install_version, False, is_ceph_conf_present
            )
            rc = test_ansible.run(
                ceph_cluster=ceph_cluster_dict[cluster_name],
                ceph_nodes=ceph_cluster_dict[cluster_name],
                config=install_config,
                test_data=kw.get("test_data"),
                ceph_cluster_dict=ceph_cluster_dict,
                clients=kw.get("clients"),
            )
            if rc != 0:
                return rc

        for version in upgrade_versions:
            upgrade_steps = paths[version]['upgrade_steps']
            for steps in upgrade_steps:
                if upgrade_steps[steps]['command'] == 'upgrade_all':
                    index = upgrade_versions.index(version)
                    prev_version = upgrade_versions[index - 1] if index > 0 else install_version
                    if version >= 5.0 and prev_version >= 5.0:
                        if config.get("paths").get(install_version).get("config"):
                            config = config.get("paths").get(install_version).get("config")
                        else:
                            config = get_cephadm_upgrade_config(config, version)
                        rc = test_cephadm_upgrade.run(
                            ceph_cluster=ceph_cluster_dict[cluster_name],
                            ceph_nodes=ceph_cluster_dict[cluster_name],
                            config=config,
                            test_data=kw.get("test_data"),
                            ceph_cluster_dict=ceph_cluster_dict,
                            clients=kw.get("clients"),
                        )
                        if rc != 0:
                            return rc
                    elif version >= 5.0 and 4.0 <= prev_version <= 5.0:
                        upgrade_config = get_ansible_conf(config, version, False, False)
                        if not ceph_cluster.containerized:
                            rc = switch_rpm_to_container.run(
                                ceph_cluster=ceph_cluster_dict[cluster_name],
                                ceph_nodes=ceph_cluster_dict[cluster_name],
                                config=upgrade_config,
                                test_data=kw.get("test_data"),
                                ceph_cluster_dict=ceph_cluster_dict,
                                clients=kw.get("clients"),
                            )
                            if rc != 0:
                                return rc
                            upgrade_config["ansi_config"]["containerized_deployment"] = True
                        rc = test_ansible_upgrade.run(
                            ceph_cluster=ceph_cluster_dict[cluster_name],
                            ceph_nodes=ceph_cluster_dict[cluster_name],
                            config=upgrade_config,
                            test_data=kw.get("test_data"),
                            ceph_cluster_dict=ceph_cluster_dict,
                            clients=kw.get("clients"),
                        )
                        if rc != 0:
                            return rc
                    else:
                        upgrade_config = get_ansible_conf(config, version, False, False)
                        rc = test_ansible_upgrade.run(
                            ceph_cluster=ceph_cluster_dict[cluster_name],
                            ceph_nodes=ceph_cluster_dict[cluster_name],
                            config=upgrade_config,
                            test_data=kw.get("test_data"),
                            ceph_cluster_dict=ceph_cluster_dict,
                            clients=kw.get("clients"),
                        )
                        if rc != 0:
                            return rc
    return 0
