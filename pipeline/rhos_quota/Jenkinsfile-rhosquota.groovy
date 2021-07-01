/*
    Pipeline script for fetching rhos-d quota.
*/
// Global variables section

def nodeName = "centos-7"
def sharedLib
def rhosLib
def projects = ["ceph-ci", "ceph-jenkins"]

node(nodeName){
    stage('Install prereq') {
        checkout([
            $class: 'GitSCM',
            branches: [[name: '*/rhosd_quota']],
            doGenerateSubmoduleConfigurations: false,
            extensions: [[
                $class: 'SubmoduleOption',
                disableSubmodules: false,
                parentCredentials: false,
                recursiveSubmodules: true,
                reference: '',
                trackingSubmodules: false
            ]],
            submoduleCfg: [],
            userRemoteConfigs: [[url: 'https://github.com/manasagowri/cephci.git']]
        ])
        script {
            sharedLib = load("${env.WORKSPACE}/pipeline/vars/common.groovy")
            sharedLib.prepareNode()
            rhosLib = load("${env.WORKSPACE}/pipeline/rhos_quota/quota_stats.groovy")
            content = rhosLib.installOpenStackClient()
        }
    }
    stage('Fetch quota and Send Email') {
        script{
            args = ["osp-cred" : "${HOME}/osp-cred-ci-2.yaml",
                    "projects": projects]
            def quota_detail = rhosLib.fetch_quota(args)
            echo "quota detail fetched"
            rhosLib.sendEmail(quota_detail)
            echo "Email sent"
        }
    }
}
