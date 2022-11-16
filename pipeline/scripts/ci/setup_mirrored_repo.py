import logging
import os
import pdb

from docopt import docopt
from run import create_nodes, load_file, generate_unique_id, create_run_dir
from tests.misc_env.install_prereq import setup_subscription_manager, enable_rhel_rpms
from utility.log import Log

import sys

from utility.utils import configure_logger

doc = """                                                                               
Module to setup mirrored repos for rhel composes at openstack and ibmc environments        

 Usage:                                                                                 
  setup_mirrored_repo.py --platform <name>                                                             
        (--global-conf FILE | --cluster-conf FILE)                                      
        [--cloud <openstack> | <ibmc> ]                                                                
        [--inventory FILE]                                                              
        [--osp-cred <file>]                                             
        [--log-level <LEVEL>]                                                           
        [--log-dir  <directory-name>]                                                   
        [--instances-name <name>]                                                       
        [--osp-image <image>]                                                           
        [--vm-size <size>]                                                        

Options:                                                                                
  -h --help                         show this screen                                    
  -v --version                      run version           
  --global-conf <file>              global cloud configuration file                     
  --cluster-conf <file>             cluster configuration file                          
  --inventory <file>                hosts inventory file                                
  --cloud <cloud_type>              cloud type [default: openstack]                     
  --osp-cred <file>                 openstack credentials as separate file       
  --platform <rhel-8>               select platform version eg., rhel-8, rhel-7    
  --log-level <LEVEL>               Set logging level                                   
  --log-dir <LEVEL>                 Set log directory                                   
  --instances-name <name>           Name that will be used for instances creation       
  --osp-image <image>               Image for osp instances, default value is taken from
                                    conf file                                           
  --vm-size <size>                  VM size for osp instances, default value is taken from
                                    conf file                             
"""
log = Log()


def create_node(args):
    """

    Args:
        args:

    Returns:

    """
    glb_file = args.get("--global-conf")
    inventory_file = args.get("--inventory")
    osp_cred_file = args.get("--osp-cred")
    cloud_type = args.get("--cloud") or "openstack"
    vm_size = args.get("--vm-size")
    osp_image = args.get("--osp-image")

    service = None

    instances_name = args.get("--instances-name")
    if instances_name:
        instances_name = instances_name.replace(".", "-")

    if args.get("--cluster-conf"):
        glb_file = args["--cluster-conf"]

    conf = load_file(glb_file)

    if glb_file is None:
        raise Exception("Unable to gather information about cluster layout.")

    if osp_cred_file is None and cloud_type in ["openstack", "ibmc"]:
        raise Exception("Require cloud credentials to create cluster.")

    if inventory_file is None and cloud_type in ["openstack", "ibmc"]:
        raise Exception("Require system configuration information to provision.")

    if inventory_file:
        inventory = load_file(inventory_file)

        if osp_image and inventory.get("instance", {}).get("create"):
            inventory.get("instance").get("create").update({"image-name": osp_image})

        if vm_size and inventory.get("instance", {}).get("create"):
            inventory.get("instance").get("create").update({"vm-size": vm_size})

        image_name = inventory.get("instance", {}).get("create", {}).get("image-name")

    osp_cred = load_file(osp_cred_file)
    run_id = args.get("run_id")

    platform = args["--platform"]
    try:
        ceph_cluster_dict, clients = create_nodes(
            conf,
            inventory,
            osp_cred,
            run_id,
            cloud_type,
            service,
            instances_name,
            enable_eus=False,
            rp_logger=None,
        )
        sys.exit(0)
    except Exception as err:
        log.error(err)
        sys.exit(1)


def setup_subscription(ceph):
    """
    Add steps to setup initial prerequisites, ca cert, etc., setup subscription manager, enable rhel repos
    Args:
        ceph: Ceph node object

    Returns:

    """
    # instead of the below, try doing similar to upload_compose.py, install subscription manager in the jenkins node
    # and download using reposync there itself and copy it to magna and ibmcos
    setup_subscription_manager(ceph)
    distro_info = ceph.distro_info
    distro_ver = distro_info["VERSION_ID"]
    repos = enable_rhel_rpms(ceph, distro_ver)
    ceph.exec_command(cmd="sudo yum -y upgrade", check_ec=False)
    return repos


def download_rhel_repo(ceph, repos):
    """
    Use reposync utility to download rhel repos necessary
    Returns:

    """
    for repo in repos:
        ceph.exec_command(cmd=f"reposync -p /repos/ --download-metadata --repo-id={repo}", long_running=True)


def copy_repo():
    """
    Copy the downloaded repo to both openstack and IBM environment

    Returns:

    """


def setup_logging(id):
    log_directory = arguments.get("--log-dir")
    run_dir = create_run_dir(id, log_directory)
    startup_log = os.path.join(run_dir, "startup.log")
    handler = logging.FileHandler(startup_log)
    log.logger.addHandler(handler)
    log_link = configure_logger("setup_subscription", run_dir)
    return log_link


if __name__ == "__main__":
    arguments = docopt(doc)
    run_id = generate_unique_id(length=6)
    arguments["run_id"] = run_id
    setup_logging(run_id)
    ceph_cluster_dict = create_node(arguments)
    repos = setup_subscription(ceph_cluster_dict["ceph"])
    download_rhel_repo(ceph=ceph_cluster_dict["ceph"], repos=repos)
    copy_repo()

