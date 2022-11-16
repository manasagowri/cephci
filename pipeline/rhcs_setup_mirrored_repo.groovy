// The primary objective of this script is to deploy a RHCeph cluster for OCS CI.

def argsMap = [
    "7.8":[
        "inventory":"conf/inventory/rhel-7.8-server-x86_64.yaml",
    ],
    "7.9":[
        "inventory": "conf/inventory/rhel-7.9-server-x86_64.yaml"
    ],
    "8.4":[
        "inventory": "conf/inventory/rhel-8.4-server-x86_64.yaml"
    ],
    "8.5":[
        "inventory": "conf/inventory/rhel-8.5-server-x86_64.yaml"
    ],
    "8.6":[
        "inventory": "conf/inventory/rhel-8.6-server-x86_64.yaml"
    ],
    "9.0":[
        "inventory": "conf/inventory/rhel-9.0-server-x86_64.yaml"
    ]
]
def ciMap = [:]
def sharedLib
def vmPrefix

println("I am here")

node ("rhel-8-medium || ceph-qe-ci") {

    stage("prepareJenkinsAgent") {
        if (env.WORKSPACE) { sh script: "sudo rm -rf * .venv" }

        checkout(
            scm: [
                $class: 'GitSCM',
                branches: [[name: 'origin/mirrored_repos']],
                extensions: [[
                    $class: 'CleanBeforeCheckout',
                    deleteUntrackedNestedRepositories: true
                ], [
                    $class: 'WipeWorkspace'
                ], [
                    $class: 'CloneOption',
                    depth: 1,
                    noTags: true,
                    shallow: true,
                    timeout: 10,
                    reference: ''
                ]],
                userRemoteConfigs: [[
                    url: 'https://github.com/manasagowri/cephci.git'
                ]]
            ],
            changelog: false,
            poll: false
        )

        sharedLib = load("${env.WORKSPACE}/pipeline/vars/v3.groovy")
        sharedLib.prepareNode()
    }

    stage("setupMirroredRepo") {
        def rhel_version = "${params.rhel_version}"
        println("${params.rhel_version}")
        println(rhel_version)
        if (rhel_version?.trim()) {
            def clusterName = "setup_${rhel_version}"

            def cliArgs = ""

            // Prepare the CLI arguments
            cliArgs += " --platform ${rhel_version}"
            cliArgs += " --inventory ${env.WORKSPACE}/${argsMap[rhel_version]['inventory']}"
            cliArgs += " --global-conf ${env.WORKSPACE}/conf/single-node.yaml"
            cliArgs += " --vm-size m1.large"

            println "Debug: ${cliArgs}"

            returnStatus = sharedLib.setupMirroredRepo(cliArgs, false, false)
            if ( returnStatus.result == "FAIL") {
                error "Deployment failed."
            }

            vmPrefix = returnStatus["instances-name"]
        }
        else{
            println("Parameter rhel_version is not specified.")
        }
    }
//
//     stage('postMessage') {
//         def sutInfo = readYaml file: "sut.yaml"
//         sutInfo["instances-name"] = vmPrefix
//
//         def msgMap = [
//             "artifact": [
//                 "type": "product-build",
//                 "name": "Red Hat Ceph Storage",
//                 "nvr": "RHCEPH-${ciMap.build}",
//                 "phase": "integration",
//             ],
//             "extra": sutInfo,
//             "contact": [
//                 "name": "Downstream Ceph QE",
//                 "email": "cephci@redhat.com",
//             ],
//             "system": [
//                 "os": "centos-7",
//                 "label": "centos-7",
//                 "provider": "openstack",
//             ],
//             "pipeline": [
//                 "name": "rhceph-deploy-cluster",
//                 "id": currentBuild.number,
//             ],
//             "run": [
//                 "url": env.BUILD_URL,
//                 "log": "${env.BUILD_URL}console",
//             ],
//             "test": [
//                 "type": "integration",
//                 "category": "system",
//                 "result": currentBuild.currentResult,
//             ],
//             "generated_at": env.BUILD_ID,
//             "version": "1.1.0"
//         ]
//
//         def msg = writeJSON returnText: true, json: msgMap
//         println msg
//
//         sharedLib.SendUMBMessage(
//             msg,
//             "VirtualTopic.qe.ci.rhcs.deploy.complete",
//             "ProductBuildInStaging"
//         )
//     }
}
