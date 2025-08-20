import requests

def get_pipeline_status_from_gitlab(pipeline_id):
    """
    從 GitLab API 查詢 pipeline 狀態

    Args:
        pipeline_id (str): Pipeline ID

    Returns:
        dict: Pipeline 狀態資訊
    """
    gitlab_url = "http://172.26.1.176:8080"
    project_id = "15"

    # 這裡需要 GitLab access token，而不是 trigger token
    # 你可能需要提供一個有讀取權限的 access token
    headers = {
        "PRIVATE-TOKEN": "glpat-L8_6TMifNGL6uby92h_f"
    }

    try:
        response = requests.get(
            f"{gitlab_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()

        pipeline_data = response.json()
        return {
            "success": True,
            "pipeline_id": str(pipeline_data.get("id", "")),
            "status": pipeline_data.get("status", "unknown"),
            "web_url": pipeline_data.get("web_url", ""),
            "ref": pipeline_data.get("ref", ""),
            "sha": pipeline_data.get("sha", ""),
            "created_at": pipeline_data.get("created_at", ""),
            "updated_at": pipeline_data.get("updated_at", ""),
            "finished_at": pipeline_data.get("finished_at", ""),
            "duration": pipeline_data.get("duration", 0)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_pipeline_jobs(pipeline_id):
    """
    從 GitLab API 查詢 pipeline 的 jobs

    Args:
        pipeline_id (str): Pipeline ID

    Returns:
        dict: Jobs 資訊
    """
    gitlab_url = "http://172.26.1.176:8080"
    project_id = "15"

    headers = {
        "PRIVATE-TOKEN": "glpat-L8_6TMifNGL6uby92h_f"
    }

    try:
        response = requests.get(
            f"{gitlab_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()

        jobs_data = response.json()

        jobs = []
        for job in jobs_data:
            jobs.append({
                "job_id": str(job.get("id", "")),
                "name": job.get("name", ""),
                "stage": job.get("stage", ""),
                "status": job.get("status", ""),
                "web_url": job.get("web_url", ""),
                "created_at": job.get("created_at", ""),
                "started_at": job.get("started_at", ""),
                "finished_at": job.get("finished_at", ""),
                "duration": job.get("duration", 0)
            })

        return {
            "success": True,
            "jobs": jobs
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "jobs": []
        }


# def get_pipeline_status_with_db_info(pipeline_id):
#     """
#     查詢 pipeline 狀態，結合 GitLab API 和資料庫資訊

#     Args:
#         pipeline_id (str): Pipeline ID

#     Returns:
#         dict: 完整的 pipeline 資訊（包含資料庫中的環境變數等）
#     """
#     try:
#         db_conn = get_db_connection()
#         cursor = db_conn.cursor()

#         # 從資料庫查詢 pipeline 相關資訊
#         cursor.execute("""
#             SELECT
#                 gp.workflow_id,
#                 gp.pipeline_id,
#                 gp.job_id,
#                 gp.project_name,
#                 gp.branch,
#                 gp.commit_sha,
#                 gp.status as db_status,
#                 gp.triggered_by,
#                 gp.created_at,
#                 gp.environment,
#                 gp.resource,
#                 gp.os_type,
#                 gp.vsphere_datacenter,
#                 gp.vsphere_cluster,
#                 gp.vsphere_network,
#                 gp.vsphere_template,
#                 gp.vsphere_datastore,
#                 gp.vm_name_prefix,
#                 gp.vm_instance_type,
#                 gp.vm_num_cpus,
#                 gp.vm_memory,
#                 gp.vm_ipv4_gateway,
#                 gp.netbox_prefix,
#                 gp.netbox_tenant,
#                 -- 同時查詢關聯的 Jira ticket 資訊
#                 jt.ticket_id,
#                 jt.summary as jira_summary,
#                 jt.status as jira_status,
#                 jt.url as jira_url
#             FROM gitlab_pipelines gp
#             LEFT JOIN jira_tickets jt ON gp.workflow_id = jt.workflow_id
#             WHERE gp.pipeline_id = %s
#         """, (pipeline_id,))

#         db_result = cursor.fetchone()
#         cursor.close()
#         db_conn.close()

#         if not db_result:
#             return {
#                 "success": False,
#                 "error": f"Pipeline {pipeline_id} not found in database"
#             }

#         # 整理資料庫查詢結果
#         db_columns = [
#             'workflow_id', 'pipeline_id', 'job_id', 'project_name', 'branch', 
#             'commit_sha', 'db_status', 'triggered_by', 'created_at',
#             'environment', 'resource', 'os_type', 'vsphere_datacenter',
#             'vsphere_cluster', 'vsphere_network', 'vsphere_template',
#             'vsphere_datastore', 'vm_name_prefix', 'vm_instance_type',
#             'vm_num_cpus', 'vm_memory', 'vm_ipv4_gateway',
#             'netbox_prefix', 'netbox_tenant',
#             'jira_ticket_id', 'jira_summary', 'jira_status', 'jira_url'
#         ]

#         db_info = dict(zip(db_columns, db_result))

#         # 從 GitLab API 查詢最新狀態
#         gitlab_status = get_pipeline_status_from_gitlab(pipeline_id)

#         # 查詢 jobs 資訊
#         jobs_info = get_pipeline_jobs(pipeline_id)

#         # 合併所有資訊
#         result = {
#             "success": True,
#             "pipeline_info": {
#                 # 資料庫中的基本資訊
#                 "workflow_id": db_info['workflow_id'],
#                 "pipeline_id": db_info['pipeline_id'],
#                 "job_id": db_info['job_id'],
#                 "project_name": db_info['project_name'],
#                 "branch": db_info['branch'],
#                 "commit_sha": db_info['commit_sha'],
#                 "triggered_by": db_info['triggered_by'],
#                 "created_at": db_info['created_at'],

#                 # GitLab API 的最新狀態
#                 "current_status": gitlab_status.get("status", db_info['db_status']) if gitlab_status["success"] else db_info['db_status'],
#                 "web_url": gitlab_status.get("web_url", "") if gitlab_status["success"] else "",
#                 "updated_at": gitlab_status.get("updated_at", "") if gitlab_status["success"] else "",
#                 "finished_at": gitlab_status.get("finished_at", "") if gitlab_status["success"] else "",
#                 "duration": gitlab_status.get("duration", 0) if gitlab_status["success"] else 0,

#                 # 環境變數和配置資訊
#                 "vm_config": {
#                     "environment": db_info['environment'],
#                     "resource": db_info['resource'],
#                     "os_type": db_info['os_type'],
#                     "vsphere_datacenter": db_info['vsphere_datacenter'],
#                     "vsphere_cluster": db_info['vsphere_cluster'],
#                     "vsphere_network": db_info['vsphere_network'],
#                     "vsphere_template": db_info['vsphere_template'],
#                     "vsphere_datastore": db_info['vsphere_datastore'],
#                     "vm_name_prefix": db_info['vm_name_prefix'],
#                     "vm_instance_type": db_info['vm_instance_type'],
#                     "vm_num_cpus": db_info['vm_num_cpus'],
#                     "vm_memory": db_info['vm_memory'],
#                     "vm_ipv4_gateway": db_info['vm_ipv4_gateway'],
#                     "netbox_prefix": db_info['netbox_prefix'],
#                     "netbox_tenant": db_info['netbox_tenant']
#                 },

#                 # 關聯的 Jira ticket 資訊
#                 "jira_info": {
#                     "ticket_id": db_info['jira_ticket_id'],
#                     "summary": db_info['jira_summary'],
#                     "status": db_info['jira_status'],
#                     "url": db_info['jira_url']
#                 },

#                 # Jobs 資訊
#                 "jobs": jobs_info.get("jobs", []) if jobs_info["success"] else []
#             }
#         }

#         # 如果 GitLab API 查詢失敗，添加錯誤資訊
#         if not gitlab_status["success"]:
#             result["gitlab_api_error"] = gitlab_status.get("error", "")

#         if not jobs_info["success"]:
#             result["jobs_api_error"] = jobs_info.get("error", "")

#         return result

#     except Exception as e:
#         return {
#             "success": False,
#             "error": f"Database query error: {str(e)}"
#         }


# def get_pipeline_status_by_workflow_id(workflow_id):
#     """
#     根據 workflow_id 查詢 pipeline 狀態

#     Args:
#         workflow_id (int): Workflow ID

#     Returns:
#         dict: Pipeline 狀態資訊
#     """
#     try:
#         db_conn = get_db_connection()
#         cursor = db_conn.cursor()

#         cursor.execute("""
#             SELECT pipeline_id FROM gitlab_pipelines WHERE workflow_id = %s
#         """, (workflow_id,))

#         result = cursor.fetchone()
#         cursor.close()
#         db_conn.close()

#         if result:
#             pipeline_id = result[0]
#             return get_pipeline_status_with_db_info(pipeline_id)
#         else:
#             return {
#                 "success": False,
#                 "error": f"No pipeline found for workflow_id: {workflow_id}"
#             }

#     except Exception as e:
#         return {
#             "success": False,
#             "error": f"Database error: {str(e)}"
#         }


# Example usage for testing
from app.mysql.db import get_db_connection
from app.vsphere.vm.gitlab_api.get_pipeline_status_from_gitlab import get_pipeline_status_from_gitlab
from app.vsphere.vm.db.update_gitlab_pipeline_details import update_gitlab_pipeline_details

if __name__ == "__main__":
    # 測試 pipeline_id（請替換成實際 pipeline ID）
    pipeline_id = "1433"

    print("=== 測試 GitLab API 查詢 ===")
    gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)

    if gitlab_result["success"]:
        print("\n🛠️ GitLab API 回傳資料：")
        print(f"   Pipeline ID  : {gitlab_result['pipeline_id']}")
        print(f"   Status       : {gitlab_result['status']}")
        print(f"   Web URL      : {gitlab_result['web_url']}")
        print(f"   Ref          : {gitlab_result['ref']}")
        print(f"   SHA          : {gitlab_result['sha']}")
        print(f"   Created At   : {gitlab_result['created_at']}")
        print(f"   Updated At   : {gitlab_result['updated_at']}")
        print(f"   Finished At  : {gitlab_result['finished_at']}")
        print(f"   Duration     : {gitlab_result['duration']} seconds")
    else:
        print(f"❌ GitLab API 查詢失敗：{gitlab_result['error']}")

    if gitlab_result["success"]:
        try:
            print("\n=== 嘗試更新資料庫 ===")
            db_conn = get_db_connection()
            update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)
            db_conn.close()
            print("✅ 資料庫更新完成")
        except Exception as e:
            print(f"❌ 測試失敗：{e}")
    else:
        print(f"❌ 無法從 GitLab API 取得資料：{gitlab_result['error']}")


    print("\n=== 測試 Jobs 查詢 ===")
    jobs_result = get_pipeline_jobs(pipeline_id)

    if jobs_result["success"]:
        print("Jobs result:")
        for job in jobs_result["jobs"]:
            print(f"\n🛠️ Job ID        : {job['job_id']}")
            print(f"   Name         : {job['name']}")
            print(f"   Stage        : {job['stage']}")
            print(f"   Status       : {job['status']}")
            print(f"   Web URL      : {job['web_url']}")
            print(f"   Created At   : {job['created_at']}")
            print(f"   Started At   : {job['started_at']}")
            print(f"   Finished At  : {job['finished_at']}")
            print(f"   Duration     : {job['duration']} seconds")
    else:
        print(f"❌ Failed to fetch jobs: {jobs_result['error']}")

    # print("\n=== 測試完整狀態查詢 ===")
    # full_result = get_pipeline_status_with_db_info(pipeline_id)
    # print(f"Full result: {full_result}")

    # print("\n=== 測試根據 workflow_id 查詢 ===")
    # workflow_result = get_pipeline_status_by_workflow_id(32)
    # print(f"Workflow result: {workflow_result}")