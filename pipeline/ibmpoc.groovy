def sharedLib
def nodeName = "agent-01 || ceph-qe-ci"
def testResults = [:]
def repo = "https://github.com/manasagowri/cephci.git"
def branch = "ibm_poc"

node(nodeName) {
    stage('PrepareAgent') {
        if (env.WORKSPACE) { sh script: "sudo rm -rf * .venv" }
        checkout(
            scm: [
                $class: 'GitSCM',
                branches: [[name: branch]],
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
                    url: repo
                ]]
            ],
            changelog: false,
            poll: false
        )

        sharedLib = load("${env.WORKSPACE}/pipeline/vars/v3.groovy")
        sharedLib.prepareIbmNode()
    }
    stage("Execute test suite"){
        def tags = "dmfg,ibmc,tier-0,stage-1"
        def overrides = '{"build":"tier-0", "store": ""}'
//         def overrides = '{"build":"tier-0", "reuse": "rerun/test_run"}'
        overrides = readJSON text: "${overrides}"
        def workspace = "${env.WORKSPACE}"
        def build_number = "${currentBuild.number}"
        overrides.put("workspace", workspace.toString())
        overrides.put("build_number", build_number.toInteger())
        overrides.put("store-file", "${workspace}/rerun/test_run".toString())
        def rhcephVersion = "RHCEPH-5.3"
        fetchStages = sharedLib.fetchStages(tags, overrides, testResults, rhcephVersion)
        print("Stages fetched")
        testStages = fetchStages["testStages"]
        final_stage = fetchStages["final_stage"]
        println("final_stage : ${final_stage}")
        parallel testStages
        print("Stages executed")
        sh "scp ${workspace}/rerun/test_run cephuser@10.245.4.89:/data/site/."
        def infra="10.245.4.89"
        def fileExist = sh(
            returnStatus: true,
            script: "ssh $infra \"sudo ls -l /data/site/test_run\""
            )
        if (fileExist != 0) {
            error "File does not exist.."
        }
        print("File exists")
    }
}