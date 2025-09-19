# app/vsphere/vm/gitlab_api/trigger_gitlab_pipeline.py
import requests
import json
from flask import current_app

def trigger_gitlab_pipeline(form_data):
    """
    觸發 GitLab CI pipeline ( Create VM)。
    """
    gitlab_url = current_app.config['GITLAB_URL']
    project_id = current_app.config['GITLAB_PROJECT_ID']
    token = current_app.config['GITLAB_TRIGGER_TOKEN']
    branch = current_app.config['GITLAB_BRANCH']
    trigger_url = f"{gitlab_url}/api/v4/projects/{project_id}/trigger/pipeline"

    variables = {
        "ACTION_TYPE": form_data.get('action_type', ''),
        "ENVIRONMENT": form_data.get('environment', ''),
        "RESOURCE": form_data.get('resource', ''),
        "OS_TYPE": form_data.get('os_type', ''),
        "VSPHERE_HOST": form_data.get('vsphere_host', ''),
        "VSPHERE_DATACENTER": form_data.get('vsphere_datacenter', ''),
        "VSPHERE_CLUSTER": form_data.get('vsphere_cluster', ''),
        "VSPHERE_ESXI_HOST": form_data.get('vsphere_esxi_host', ''),
        "VSPHERE_NETWORK": form_data.get('vsphere_network', ''),
        "VSPHERE_TEMPLATE": form_data.get('vsphere_template', ''),
        "VSPHERE_DATASTORE": form_data.get('vsphere_datastore', ''),
        "VM_NAME_PREFIX": form_data.get('vm_name_prefix', ''),
        "VM_INSTANCE_TYPE": form_data.get('vm_instance_type', ''),
        "VM_NUM_CPUS": str(form_data.get('vm_num_cpus', '')),
        "VM_MEMORY": str(form_data.get('vm_memory', '')),
        "VM_IPV4_GATEWAY": form_data.get('vm_ipv4_gateway', ''),
        "NETBOX_PREFIX": form_data.get('netbox_prefix', ''),
        "NETBOX_TENANT": form_data.get('netbox_tenant', ''),
    }

    payload = {"token": token, "ref": branch}
    for key, value in variables.items():
        payload[f"variables[{key}]"] = value

    try:
        response = requests.post(trigger_url, data=payload, timeout=30)
        response.raise_for_status()
        pipeline_data = response.json()
        return {
            "success": True,
            "pipeline_id": str(pipeline_data.get("id", "")),
            "id": str(pipeline_data.get("id", "")),
            "web_url": pipeline_data.get("web_url", ""),
            "sha": pipeline_data.get("sha", ""),
            "ref": pipeline_data.get("ref", branch),
            "status": pipeline_data.get("status", "pending"),
            "project_id": project_id,
            "variables": variables
        }
    except requests.exceptions.HTTPError as http_err:
        error_msg = f"HTTP error occurred: {http_err} - {getattr(http_err.response, 'text', '')}"
        return {"success": False, "error": error_msg, "pipeline_id": None}
    except requests.exceptions.RequestException as req_err:
        error_msg = f"Request error occurred: {req_err}"
        return {"success": False, "error": error_msg, "pipeline_id": None}
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        return {"success": False, "error": error_msg, "pipeline_id": None}