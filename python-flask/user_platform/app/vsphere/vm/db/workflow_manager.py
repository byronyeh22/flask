# app/vsphere/vm/db/workflow_manager.py
import json
from mysql.connector import Error
import logging
from flask import session
from datetime import datetime

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Helper Functions ---
class _Helpers:
    @staticmethod
    def _first_scalar(value, default=None):
        if isinstance(value, list): return value[0] if value else default
        return value if value is not None else default

    @staticmethod
    def _as_list(value):
        if value is None: return []
        if isinstance(value, list): return value
        return [value]

    @staticmethod
    def _to_int(value, default=0):
        scalar_value = _Helpers._first_scalar(value)
        try:
            return int(scalar_value)
        except (TypeError, ValueError):
            return default

# --- Workflow Management Functions ---

def record_pending_request(db_conn, form_data):
    """
    第一階段：在使用者提交請求時呼叫，建立一個待審批的工作流。

    Returns:
        int: 新建立的 workflow_id
    """
    cursor = None

    # 假設使用者名稱儲存在 session 中
    created_by = session.get("username", "system")

    payload_json = json.dumps(form_data, ensure_ascii=False)

    sql = """
        INSERT INTO workflow_runs (created_by, status, request_payload)
        VALUES (%s, %s, %s)
    """
    try:
        cursor = db_conn.cursor()
        # 將新請求的狀態預設為 'DRAFT'
        cursor.execute(sql, (created_by, 'DRAFT', payload_json))
        workflow_id = cursor.lastrowid
        db_conn.commit()
        logging.info(f"✅ Successfully recorded pending request for workflow_id: {workflow_id}")
        return workflow_id
    except Error as e:
        logging.error(f"❌ Database error in record_pending_request: {e}")
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        raise
    finally:
        if cursor: cursor.close()

def update_request_status(db_conn, workflow_id, new_status, approver=None, failed_message=None):
    """
    更新工作流狀態，並可選填入審批者與失敗訊息。
    """
    cursor = None
    try:
        cursor = db_conn.cursor()

        sql = "UPDATE workflow_runs SET status = %s"
        params = [new_status]

        if approver:
            sql += ", approved_by = %s, approved_at = %s"
            params.extend([approver, datetime.now()])

        if failed_message:
            sql += ", failed_message = %s"
            params.append(failed_message)

        sql += " WHERE workflow_id = %s"
        params.append(workflow_id)

        cursor.execute(sql, tuple(params))
        db_conn.commit()
        logging.info(f"✅ Successfully updated workflow {workflow_id} status to {new_status}.")
    except Error as e:
        logging.error(f"❌ Database error in update_request_status for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        raise
    finally:
        if cursor: cursor.close()

def cancel_request(db_conn, workflow_id):
    """
    執行取消操作，將 cancelled_by 與 cancelled_at 欄位填入。
    """
    cursor = None
    try:
        cancelled_by = session.get("username", "system")
        cursor = db_conn.cursor()
        sql = "UPDATE workflow_runs SET status = 'CANCELLED', cancelled_by = %s, cancelled_at = %s WHERE workflow_id = %s"
        cursor.execute(sql, (cancelled_by, datetime.now(), workflow_id))
        db_conn.commit()
        logging.info(f"✅ Successfully cancelled workflow_id: {workflow_id}.")
    except Error as e:
        logging.error(f"❌ Database error in cancel_request for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        raise
    finally:
        if cursor: cursor.close()

def apply_request_to_db(db_conn, workflow_id):
    """
    第二階段：在請求被批准後呼叫。
    讀取 request_payload，並將變更正式應用到 vm_configurations 和 vm_disks 表。
    """
    cursor = None
    try:
        # 1. 從資料庫讀取 request_payload
        cursor = db_conn.cursor(dictionary=True)
        cursor.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        workflow = cursor.fetchone()
        if not workflow or not workflow['request_payload']:
            raise ValueError(f"Workflow {workflow_id} not found or has no payload.")

        # 從 JSON 欄位中解析資料
        form_data = json.loads(workflow['request_payload'])
        action_type = _Helpers._first_scalar(form_data.get('action_type'))

        # 2. 根據請求類型，分派給對應的處理函式
        if action_type == 'create':
            _apply_create_action(db_conn, form_data)
        elif action_type == 'update':
            _apply_update_action(db_conn, form_data)
        else:
            raise ValueError(f"Unsupported action_type: {action_type}")

        # 3. 更新 workflow 狀態為 IN_PROGRESS
        update_request_status(db_conn, workflow_id, 'IN_PROGRESS')
        
        db_conn.commit()
        logging.info(f"✅ Successfully applied changes for workflow_id: {workflow_id} to DB.")

    except Error as e:
        logging.error(f"❌ Database error in apply_request_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        raise
    except Exception as e:
        logging.error(f"❌ Unexpected error in apply_request_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        raise
    finally:
        if cursor: cursor.close()

# --- Private Helper functions for apply_request_to_db ---

def _apply_create_action(db_conn, form_data):
    """私有函式：處理 Create 請求的資料庫寫入"""
    cursor = db_conn.cursor()

    # 1. 插入主表
    sql_vm_config = """
        INSERT INTO vm_configurations (
            environment, resource, os_type, vsphere_datacenter, vsphere_cluster,
            vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
            vm_instance_type, vm_num_cpus, vm_memory, vm_scsi_controller_count,
            vm_ipv4_gateway, netbox_prefix, netbox_tenant
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    params_vm_config = (
        _Helpers._first_scalar(form_data.get('environment')),
        _Helpers._first_scalar(form_data.get('resource')),
        _Helpers._first_scalar(form_data.get('os_type')),
        _Helpers._first_scalar(form_data.get('vsphere_datacenter')),
        _Helpers._first_scalar(form_data.get('vsphere_cluster')),
        _Helpers._first_scalar(form_data.get('vsphere_network')),
        _Helpers._first_scalar(form_data.get('vsphere_template')),
        _Helpers._first_scalar(form_data.get('vsphere_datastore')),
        _Helpers._first_scalar(form_data.get('vm_name_prefix')),
        _Helpers._first_scalar(form_data.get('vm_instance_type')),
        _Helpers._to_int(form_data.get('vm_num_cpus'), 2),
        _Helpers._to_int(form_data.get('vm_memory'), 2048),
        _Helpers._to_int(form_data.get('create_vm_scsi_controller_count'), 1),
        _Helpers._first_scalar(form_data.get('vm_ipv4_gateway')),
        _Helpers._first_scalar(form_data.get('netbox_prefix')),
        _Helpers._first_scalar(form_data.get('netbox_tenant')),
    )
    cursor.execute(sql_vm_config, params_vm_config)
    vm_config_id = cursor.lastrowid
    logging.info(f"   -> Applied CREATE action for vm_config_id: {vm_config_id}")

    # 2. 插入附屬表
    disk_sizes = _Helpers._as_list(form_data.get('create_vm_disk_size[]'))
    if disk_sizes:
        sql_vm_disk = """
            INSERT INTO vm_disks (
                vm_configuration_id, scsi_controller, unit_number, size, 
                disk_provisioning, status, ui_disk_number
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        disk_provs = _Helpers._as_list(form_data.get('create_vm_disk_provisioning[]'))
        disk_scsis = _Helpers._as_list(form_data.get('create_vm_disk_scsi_controller[]'))
        disk_units = _Helpers._as_list(form_data.get('create_vm_disk_unit_number[]'))

        disks_to_insert = []
        for i, size in enumerate(disk_sizes):
            disks_to_insert.append((
                vm_config_id,
                disk_scsis[i] if i < len(disk_scsis) else 0,
                disk_units[i] if i < len(disk_units) else (i + 1),
                size,
                disk_provs[i] if i < len(disk_provs) else 'thin',
                'PENDING_CREATION',
                i + 2
            ))
        cursor.executemany(sql_vm_disk, disks_to_insert)
        logging.info(f"   -> Marked {len(disks_to_insert)} disks as PENDING_CREATION for vm_config_id: {vm_config_id}")

    cursor.close()

def _apply_update_action(db_conn, form_data):
    """私有函式：處理 Update 請求的資料庫寫入"""
    cursor = db_conn.cursor(dictionary=True)

    env = _Helpers._first_scalar(form_data.get('environment'))
    prefix = _Helpers._first_scalar(form_data.get('vm_name_prefix'))

    # 1. 查詢 ID 並更新主表
    cursor.execute("SELECT id FROM vm_configurations WHERE environment = %s AND vm_name_prefix = %s", (env, prefix))
    vm_config = cursor.fetchone()
    if not vm_config:
        raise ValueError(f"Cannot apply update: VM '{prefix}' in '{env}' not found.")
    vm_config_id = vm_config['id']

    sql_update_vm = "UPDATE vm_configurations SET vm_num_cpus = %s, vm_memory = %s WHERE id = %s"
    params_update_vm = (
        _Helpers._to_int(form_data.get('vm_num_cpus')),
        _Helpers._to_int(form_data.get('vm_memory')),
        vm_config_id
    )
    update_cursor = db_conn.cursor()
    update_cursor.execute(sql_update_vm, params_update_vm)
    update_cursor.close()
    logging.info(f"   -> Applied UPDATE action for vm_config_id: {vm_config_id}")

    # 2. 同步 vm_disks (此處僅為範例，實際邏輯會更複雜)
    # ... (此處省略比對硬碟並標記 PENDING_UPDATE / PENDING_DELETION 的邏輯)

    cursor.close()