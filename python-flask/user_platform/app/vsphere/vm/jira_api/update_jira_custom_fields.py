import requests

def update_jira_custom_fields(ticket_id, form_data):
    jira_base = "https://sanbox888twuat.atlassian.net"
    auth = (
        "srv.sra@sanbox888.tw",
        "ATATT3xFfGF0R9x--AYy2vPdYkG25_w52yHTrGG4wGfBwMbnsyxDMoFmSPL54MtfecWNeoLQR2_0hY73MhBh0m1njA057j8b-9qdFX4TPVlngRu9mkYq1p9TVdXei1_a0FcSt_GgaK2Ae7f8fU8v-PiDSfljnMr63Ce1TuiFApMSdxeFih-_WUE=346474B9"
    )

    payload = {
        "fields": {
            "customfield_11426": form_data['environment'],
            "customfield_11427": form_data['resource'],
            "customfield_11428": form_data['os_type'],
            "customfield_11429": form_data['vsphere_datacenter'],
            "customfield_11430": form_data['vsphere_cluster'],
            "customfield_11431": form_data['vsphere_network'],
            "customfield_11432": form_data['vsphere_template'],
            "customfield_11433": form_data['vsphere_datastore'],
            "customfield_11434": form_data['vm_name_prefix'],
            "customfield_11435": form_data['vm_instance_type'],
            "customfield_11436": str(form_data['vm_num_cpus']),
            "customfield_11437": str(form_data['vm_memory']),
            "customfield_11438": form_data['vm_ipv4_gateway'],
            "customfield_11439": form_data['netbox_prefix'],
            "customfield_11440": form_data['netbox_tenant']
        }
    }

    try:
        response = requests.put(
            f"{jira_base}/rest/api/3/issue/{ticket_id}",
            json=payload,
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()

        return response.status_code, response.text

    except requests.exceptions.HTTPError as http_err:
        error_msg = f"HTTP error occurred: {http_err} - {response.text}"
        print(error_msg)
        raise
    except requests.exceptions.RequestException as req_err:
        error_msg = f"Request error occurred: {req_err}"
        print(error_msg)
        raise

# # Example usage for testing
# if __name__ == "__main__":
#     try:
#         sample_form_data = {
#             "env_name": "sandbox",
#             "vm_type": "vm",
#             "vm_num_cpus": 2,
#             "vm_memory": 4096,
#         }
#         status, text = update_jira_custom_fields("SJT-86", sample_form_data)
#         print(f"Update status: {status}, Response: {text}")
#     except Exception as e:
#         print(f"Failed to update ticket: {e}")