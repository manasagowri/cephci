#!/usr/bin/env groovy
/*
    Methods to fetch quota statistics from rhos-d.
*/

import groovy.json.JsonSlurperClassic
import org.yaml.snakeyaml.Yaml

def installOpenStackClient(){
    sh("source .venv/bin/activate; python -m pip install python-openstackclient")
    def content = sh(returnStdout: true, script: "cd ${HOME}; ls -l")
    return content
}

def fetchBaseCommand(args){
    osp_cred_file = args['osp-cred']
    project = args['project']
    def os_cred_yaml = sh(returnStdout: true, script: "cat ${osp_cred_file}")
    Map osp_cred = new Yaml().load(os_cred_yaml)
    osp_glbs = osp_cred["globals"]
    os_cred = osp_glbs["openstack-credentials"]

    os_auth_url = "${os_cred["auth-url"]}/v3"
    os_base_cmd = "source .venv/bin/activate; "
    os_base_cmd += "openstack --os-auth-url ${os_auth_url}"
    os_base_cmd += " --os-project-domain-name ${os_cred["domain"]}"
    os_base_cmd += " --os-user-domain-name ${os_cred["domain"]}"
    os_base_cmd += " --os-project-name ${project}"
    os_base_cmd += " --os-username ${os_cred["username"]}"
    os_base_cmd += " --os-password ${os_cred["password"]}"
    return os_base_cmd
}

def fetch_instance_usage(os_base_cmd){
    os_usage_cmd = "${os_base_cmd} usage show -f json"
    // fetch memory usage stats for all instances
    os_usage = sh(returnStdout: true, script: os_usage_cmd).trim()
    def jsonParser = new JsonSlurperClassic()
    os_usage_json = jsonParser.parseText(os_usage)
    os_instances_usage = os_usage_json["Servers"]
    return os_instances_usage
}

def fetch_instances_project(os_base_cmd){
    os_node_cmd = "${os_base_cmd} server list -f json"
    // fetch all instances created for the project
    os_nodes = sh(returnStdout: true, script: os_node_cmd)
    def jsonParser = new JsonSlurperClassic()
    os_nodes_json = jsonParser.parseText(os_nodes)
    return os_nodes_json
}

def fetchInstanceDetail(os_base_cmd, instance_id){
    def os_node_detail_cmd = "${os_base_cmd} server show ${instance_id} -f json"
    def os_node_detail = sh(returnStdout: true, script: os_node_detail_cmd).toString()
    def jsonParser = new JsonSlurperClassic()
    def os_node_detail_json = jsonParser.parseText(os_node_detail)
    return os_node_detail_json
}

def fetchUserDetail(os_base_cmd, user_id){
    os_user_cmd = "${os_base_cmd} user show ${user_id} -f json"
    os_user = sh(returnStdout: true, script: os_user_cmd).trim()
    def jsonParser = new JsonSlurperClassic()
    os_user_json = jsonParser.parseText(os_user)
    return os_user_json
}

def fetchUsageForInstance(os_instances_usage, instance_id, state){
    Map usage = ['memory': 0, 'vcpus': 0, 'volumestorage': 0]
    if (state == "ACTIVE"){
        // usage can be fetched only for those nodes which are not in error state
        instance = os_instances_usage.findAll({x -> x["instance_id"] == instance_id}).collect{it}
        usage.memory = instance[0]["memory_mb"]
        usage.vcpus = instance[0]["vcpus"]
        usage.volumestorage = instance[0]["local_gb"]
        return usage
    }
    return usage
}

def fetchInstanceMapForUser(os_base_cmd, os_nodes){
    def is_usage_available = false
    try{
        os_instances_usage = fetch_instance_usage(os_base_cmd)
        is_usage_available = true
    } catch(Exception ex){
        echo "Exception while fetching usage"
        echo ex.getMessage()
        is_usage_available = false
    }
    instance_detail = [:]
    user_detail = [:]

    for (instance in os_nodes){
        instance_id = instance["ID"]

        println "Fetching node detail for instance " + instance_id
        try {
            def node_detail_json = fetchInstanceDetail(os_base_cmd, instance_id)
            user_id = node_detail_json["user_id"]
            state = node_detail_json["status"]
            user_name = ""
            if (user_detail.containsKey(user_id)){
                user_name = user_detail[user_id]
            }
            else{
                os_user_json = fetchUserDetail(os_base_cmd, user_id)
                user_name = os_user_json["name"]
                user_detail.put(user_id, user_name)
            }

            usage = [:]
            if(is_usage_available){
                usage = fetchUsageForInstance(os_instances_usage, instance_id, state)
            }
            else{
                usage = ['memory': 0, 'vcpus': 0, 'volumestorage' : 0]
            }
            if (instance_detail.containsKey(user_name)){
                instance_detail[user_name]["Instances"].add(instance["Name"])
                instance_detail[user_name]["Instance States"].add(state)
                instance_detail[user_name]["RAM Used Per Instance in MB"].add(usage.memory)
                instance_detail[user_name]["VCPUs Used Per Instance"].add(usage.vcpus)
                instance_detail[user_name]["Volume Used Per Instance in GB"].add(usage.volumestorage)
            }
            else{
                instance_dict = [
                        "Instances": [instance["Name"]],
                        "Instance States": [state],
                        "RAM Used Per Instance in MB": [usage.memory],
                        "VCPUs Used Per Instance": [usage.vcpus],
                        "Volume Used Per Instance in GB": [usage.volumestorage]
                    ]
                instance_detail.put(user_name, instance_dict)
            }
        } catch(Exception ex){
            echo "Exception"
            echo ex.getMessage()
            continue
        }
    }
    return instance_detail
}

def fetch_complete_quota(os_base_cmd, instance_detail, project_name){

    total_instances_used = 0
    total_vcpus_used = 0
    total_ram_used = 0
    total_volume_used = 0
    def user_stats
    for (k in instance_detail.keySet()){
        user = k
        def stat_map = [
                "User" : user,
                "Project" : project_name,
                "Instance Count" : instance_detail[user]["Instances"].size(),
                "RAM Used in GB" : instance_detail[user]["RAM Used Per Instance in MB"].sum()/1024,
                "VCPU Used" : instance_detail[user]["VCPUs Used Per Instance"].sum(),
                "Volume Used in TB" : instance_detail[user]["Volume Used Per Instance in GB"].sum()/1024
            ]
        if(user_stats){
            user_stats.add(stat_map)
        }
        else{
            user_stats = [stat_map]
        }
        total_instances_used += stat_map["Instance Count"]
        total_ram_used += stat_map["RAM Used in GB"]
        total_vcpus_used += stat_map["VCPU Used"]
        total_volume_used += stat_map["Volume Used in TB"]
        /*instance_detail[user]["Num of Instances"] = instance_detail[user]["Instances"].size()
        total_instances_used += instance_detail[user]["Num of Instances"]
        instance_detail[user]["RAM Used in GB"] = instance_detail[user]["RAM Used Per Instance in MB"].sum()/1024
        total_ram_used += instance_detail[user]["RAM Used in GB"]
        instance_detail[user]["VCPUs Used"] = instance_detail[user]["VCPUs Used Per Instance"].sum()
        total_vcpus_used += instance_detail[user]["VCPUs Used"]
        instance_detail[user]["Volume Used in TB"] = instance_detail[user]["Volume Used Per Instance in GB"].sum()/1024
        total_volume_used += instance_detail[user]["Volume Used in TB"]*/
    }

    os_quota_cmd = "${os_base_cmd} quota show -f json"
    os_quota = sh(returnStdout: true, script: os_quota_cmd).trim()
    def jsonParser = new JsonSlurperClassic()
    os_quota_json = jsonParser.parseText(os_quota)
    def ram_percent = (total_ram_used * 100)/(os_quota_json["ram"]/1024)
    def vcpu_percent = (total_vcpus_used * 100)/os_quota_json["cores"]
    def storage_percent = (total_volume_used * 100)/(os_quota_json["gigabytes"]/1024)

    /*quota_usage_dict = [
        'Instances Available': os_quota_json["instances"],
        'Instances Used': total_instances_used,
        'RAM Available in GB': os_quota_json["ram"]/1024,
        'RAM Used in GB': total_ram_used,
        'VCPUs Available': os_quota_json["cores"],
        'VCPUs Used': total_vcpus_used,
        'Volume Available in TB': os_quota_json["gigabytes"]/1024,
        'Volume Used in TB': total_volume_used,
        'RAM usage in %': ram_percent,
        'VCPU usage in %': vcpu_percent,
        'Storage usage in %': storage_percent
    ]*/
    quota_usage_dict = [
        'Project Name' : project_name,
        'RAM usage in %' : ram_percent,
        'VCPU usage in %' : vcpu_percent,
        'Storage usage in %' : storage_percent
    ]

    quota_stats = [
        'project_stats': quota_usage_dict,
        'user_stats': user_stats
    ]

    return quota_stats
}

def fetch_quota_for_project(args){
    os_base_cmd = fetchBaseCommand(args)

    os_nodes = fetch_instances_project(os_base_cmd)
    echo "Fetching instance_detail"
    instance_detail = fetchInstanceMapForUser(os_base_cmd, os_nodes)
    echo "Fetching quota_stats"
    quota_stats = fetch_complete_quota(os_base_cmd, instance_detail, args["project"])
    echo "Fetched quota_stats"
    return quota_stats
}

def fetch_quota(args){
    projects = args["projects"]
    def overall_project_stats = []
    def overall_user_stats = []
    def quota_detail
    for (project in projects){
        proj_args = ["osp-cred" : args["osp-cred"],
                    "project": project]
        def proj_quota_detail = fetch_quota_for_project(proj_args)
        if(quota_detail){
            quota_detail["Project Stats"].add(proj_quota_detail["project_stats"])
            quota_detail["User Stats"].add(proj_quota_detail["user_stats"])
        }
        else{
            quota_detail = [
                    "Project Stats" : [proj_quota_detail["project_stats"]],
                    "User Stats" : [proj_quota_detail["user_stats"]]
                ]
        }
    }
    echo quota_detail.keySet().toString()
    echo quota_detail["Project Stats"].keySet().toString()
    def defaultFileDir = "/ceph/cephci-jenkins/latest-rhceph-container-info"
    def tierJson = "${defaultFileDir}/quota_stats.json"
    writeJSON file: tierJson, json: quota_detail
    quota_detail["Project Stats"].sort{ a, b -> a["RAM usage in %"] <=> b["RAM usage in %"]}
    quota_detail["User Stats"].sort{a, b -> a["Instance Count"] <=> b["Instance Count"]}
    return quota_detail
}

def fetch_html_for_project(def overall_project_stats){
    def overall_stat_body = "<u><h4>Consolidated Quota Usage Summary for Projects</h4></u><table>"
    for(project_stats in overall_project_stats){
        echo "fetching proj stats"
        echo project_stats.keySet().toString()
    }
}

def sendEmail(def quota_detail){
     /*
        Send an email notification.
    */
    def body = readFile(file: "pipeline/vars/emailable-report.html")
    body += "<body><u><h3>RHOS-D Quota Usage Summary</h3></u><br />"
    body += '''<p>Hi Team,
                Please go through the instances created by each one of you and
                please plan to clear unused instances in each project.</p><br />'''
    fetch_html_for_project(quota_detail["Project Stats"])
}

def fetchSortedProjectStats(def quota_detail){
    def overall_stat_body = "<u><h4>Consolidated Quota Usage Summary for Projects</h4></u><table>"
    def user_stat_body = "<u><h4>Consolidated Quota Usage Summary for Users</h4></u>"
    def overall_header_tag = ""
    def overall_value_tag = ""
    def ram_percent = 0
    for(project in quota_detail.keySet()){
        def header_tag = ""
        def value_tag = ""
        for(stats in quota_detail[project]["overall_stats"].keySet()){
            if(!stats.contains('%')){
                continue
            }
            def value = quota_detail[project]["overall_stats"][stats]

            def color = ""
            if(value > 80){
                color = "red"
            }
            else if(value < 79 && value > 50){
                color = "yellow"
            }
            else{
                color = "green"
            }
            header_tag += "<th>${stats}</th>"
            value_tag +=  "<td bgcolor=\"${color}\">${value}</td>"
        }
        overall_header_tag = "<tr><th>Project Name</th>${header_tag}</tr>"
        if(quota_detail[project]["overall_stats"]["RAM usage in %"] > ram_percent){
            ram_percent = quota_detail[project]["overall_stats"]["RAM usage in %"]
            overall_value_tag = "<tr><td>${project}</td>${value_tag}</tr>" + overall_value_tag
        }
        else{
            overall_value_tag += "<tr><td>${project}</td>${value_tag}</tr>"
        }
    }
    overall_stat_body += "${overall_header_tag}${overall_value_tag}</table>"
    return overall_stat_body
}

def fetchSortedUserStat(def quota_detail){
    def user_stat_body = "<u><h4>Consolidated Quota Usage Summary for Users</h4></u><table>"
}

def sendEmailBackup(def quota_detail){
    /*
        Send an email notification.
    */
    def body = readFile(file: "pipeline/vars/emailable-report.html")
    body += "<body><u><h3>RHOS-D Quota Usage Summary</h3></u><br />"
    body += '''<p>Hi Team,
                Please go through the instances created by each one of you and
                please plan to clear unused instances in each project.</p><br />'''

    def overall_stat_body = fetchSortedProjectStats(quota_detail)
    def user_stat_body = "<u><h4>Consolidated Quota Usage Summary for Users</h4></u>"
    def overall_header_tag = ""
    def overall_value_tag = ""
    for (project in quota_detail.keySet()){
        def header_tag = ""
        def value_tag = ""
        for(stats in quota_detail[project]["overall_stats"].keySet()){
            if(!stats.contains('%')){
                continue
            }
            def value = quota_detail[project]["overall_stats"][stats]
            def color = ""
            if(value > 80){
                color = "red"
            }
            else if(value < 79 && value > 50){
                color = "yellow"
            }
            else{
                color = "green"
            }
            header_tag += "<th>${stats}</th>"
            value_tag +=  "<td bgcolor=\"${color}\">${value}</td>"
        }
        overall_header_tag = "<tr><th>Project Name</th>${header_tag}</tr>"
        overall_value_tag += "<tr><td>${project}</td>${value_tag}</tr>"
        body += "</table><h5>User Statistics</h5><table>"
        body += "<tr><th>User</th>"
        def headers = quota_detail["ceph-ci"]["user_stats"][quota_detail[project]["user_stats"].keySet()[0]].keySet()
        for(header in headers){
            body += "<th>${header}</th>"
        }
        body += "</tr>"
        for(user in quota_detail[project]["user_stats"].keySet()){
            if(user == "psi-ceph-jenkins"){
                continue
            }
            def quota_detail_value = quota_detail[project]["user_stats"][user]
            body += "<tr><td>${user}</td>"
            for(key in quota_detail_value.keySet()){
                def value = quota_detail_value[key]
                def strvalue = value.collect{ it.toString() }.join(", ")
                def mystrvalue = value.toString()[1..-2]
                echo "mystrvalue"
                echo mystrvalue
                body += "<td>${strvalue}</td>"
            }
            body += "</tr>"
        }
        body += "</table> "
    }
    overall_stat_body += "${overall_header_tag}${overall_value_tag}</table>"
    body += "</body> </html>"

    def to_list = "cephci@redhat.com"

    emailext(
        mimeType: 'text/html',
        subject: "Quota Usage Statistics for rhos-d projects.",
        body: "${body}",
        from: "cephci@redhat.com",
        to: "${to_list}"
    )
}

return this;