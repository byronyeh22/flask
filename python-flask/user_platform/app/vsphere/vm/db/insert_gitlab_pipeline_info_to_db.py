# app/vsphere/vm/db/insert_gitlab_pipeline_info_to_db.py
import json
from mysql.connector import Error
import logging

# 建議使用 logging 模組，可以提供更豐富的日誌級別和格式
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_data, form_data):
    """
    將觸發 GitLab Pipeline 當下的快照資訊寫入資料庫。

    這個函式現在的職責是作為一個歷史紀錄器，儲存觸發 Pipeline 時
    傳遞的所有 CI/CD 變數，以便未來追蹤與審計。

    Args:
        db_conn: 資料庫連線物件。
        workflow_id (int): 對應的 workflow_runs ID。
        pipeline_data (dict): 從 GitLab trigger API 回傳的資料。
        form_data (dict): 包含所有 VM 規格的表單資料，也就是 CI/CD 變數的來源。
    """
    cursor = None

    # 準備要插入的 SQL 語句 (所有欄位都保留，用於歷史紀錄)
    sql = """
        INSERT INTO gitlab_pipelines (
            workflow_id, pipeline_id, job_id, project_name, branch,
            commit_sha, status, triggered_by, web_url, action_type,
            environment, resource, os_type, vsphere_datacenter, vsphere_cluster,
            vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
            vm_instance_type, vm_num_cpus, vm_memory, vm_additional_disks_json,
            vm_ipv4_gateway, netbox_prefix, netbox_tenant
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
    """

    try:
        # 從表單數據中直接提取所有需要的參數
        # 對於 additional_disks, 我們信賴上游 (routes.py) 傳來的 JSON 字串
        # 在新的流程中，這個 JSON 可能是空的 (如果硬碟操作由 pyVmomi 處理)
        # 或包含完整內容 (如果 Create VM 時一併定義)
        # 無論如何，我們都忠實記錄
        vm_additional_disks_json = form_data.get('VM_ADDITIONAL_DISKS_JSON', '[]')

        # 將所有參數打包成一個元組 (tuple)，順序必須與 SQL 語句中的欄位順序完全一致
        params = (
            workflow_id,
            pipeline_data.get("pipeline_id"),
            pipeline_data.get("job_id"),
            f"project-{pipeline_data.get('project_id')}",
            pipeline_data.get("ref"),
            pipeline_data.get("sha"),
            pipeline_data.get("status"),
            "webform",
            pipeline_data.get("web_url"),
            form_data.get('action_type'),
            form_data.get('environment'),
            form_data.get('resource'),
            form_data.get('os_type'),
            form_data.get('vsphere_datacenter'),
            form_data.get('vsphere_cluster'),
            form_data.get('vsphere_network'),
            form_data.get('vsphere_template'),
            form_data.get('vsphere_datastore'),
            form_data.get('vm_name_prefix'),
            form_data.get('vm_instance_type'),
            form_data.get('vm_num_cpus'),
            form_data.get('vm_memory'),
            vm_additional_disks_json,
            form_data.get('vm_ipv4_gateway'),
            form_data.get('netbox_prefix'),
            form_data.get('netbox_tenant')
        )

        cursor = db_conn.cursor()
        cursor.execute(sql, params)
        db_conn.commit()

        logging.info(f"✅ Successfully inserted GitLab pipeline info for workflow_id: {workflow_id}, pipeline_id: {pipeline_data.get('pipeline_id')}")

    except Error as e:
        # 當資料庫操作出錯時，提供詳細的錯誤回饋
        logging.error(f"❌ Database error in insert_gitlab_pipeline_info_to_db for workflow_id: {workflow_id}")
        logging.error(f"   - MySQL Error: {e}")
        # 為了偵錯，可以選擇性地印出 SQL 和參數
        # logging.debug(f"   - Failing SQL: {sql}")
        # logging.debug(f"   - Parameters: {params}")

        # 回滾交易，確保資料庫狀態的一致性
        if db_conn.is_connected():
            db_conn.rollback()

        # 將例外重新拋出，讓上層呼叫者知道操作失敗，以便顯示 flash message
        raise

    except Exception as e:
        # 捕捉其他非資料庫的預期外錯誤
        logging.error(f"❌ An unexpected error occurred in insert_gitlab_pipeline_info_to_db: {e}")
        if db_conn.is_connected():
            db_conn.rollback()
        raise

    finally:
        if cursor:
            cursor.close()