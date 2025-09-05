# app/vsphere/vm/db/workflow_manager.py
import json
from mysql.connector import Error
import logging
from flask import session
from datetime import datetime
from app.mysql.db import get_db_connection

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
def get_all_workflow_runs():
    """
    取得所有 workflow_runs（包含 DRAFT）
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT workflow_id, status, created_at, request_payload, created_by
                FROM workflow_runs
                ORDER BY created_at DESC
            """)
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"[get_all_workflow_runs] DB error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()

def save_or_update_draft(processed_form_data, created_by, workflow_id=None):
    """
    若 workflow_id 存在且狀態為 DRAFT -> UPDATE request_payload
    否則 INSERT 新 DRAFT
    回傳: (workflow_id, "updated" | "created")
    """
    try:
        db_conn = get_db_connection()

        # 有給 workflow_id：檢查是否為 DRAFT，若是則更新 payload
        if workflow_id:
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM workflow_runs WHERE workflow_id=%s",
                    (workflow_id,)
                )
                row = cur.fetchone()
                status = row[0] if row else None

            if status in ("DRAFT", "RETURNED"):
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE workflow_runs SET request_payload=%s, updated_at=NOW() WHERE workflow_id=%s",
                        (json.dumps(processed_form_data), workflow_id)
                    )
                    db_conn.commit()
                return workflow_id, "updated"

        # 其餘情況：插入新的 DRAFT
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workflow_runs (created_by, status, request_payload) VALUES (%s, 'DRAFT', %s)",
                (created_by, json.dumps(processed_form_data))
            )
            new_workflow_id = cur.lastrowid
            db_conn.commit()
        return new_workflow_id, "created"

    except Exception as e:
        logging.error(f"[save_or_update_draft] DB error: {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()

def get_workflow_by_id(workflow_id):
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id=%s LIMIT 1",
                (workflow_id,)
            )
            return cur.fetchone()
    finally:
        if db_conn:
            db_conn.close()

def update_request_status(workflow_id, new_status, approver=None, failed_message=None):
    """
    更新工作流狀態，並可選填入審批者與失敗訊息。
    - 若狀態更新為 IN_PROGRESS，會同時設定 submitted_at（僅第一次）。
    - 所有時間都由 DB 端 NOW() 產生，避免時區不一致。
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor() as cursor:
            sql = "UPDATE workflow_runs SET status = %s"
            params = [new_status]

            # 審批資訊：approved_by / approved_at
            if approver:
                sql += ", approved_by = %s, approved_at = NOW()"
                params.append(approver)

            # 失敗訊息
            if failed_message:
                sql += ", failed_message = %s"
                params.append(failed_message)

            # 第一次提交：submitted_at 只在為 NULL 時寫入
            if new_status.upper() == "IN_PROGRESS":
                sql += ", submitted_at = COALESCE(submitted_at, NOW())"

            # 無論如何都更新 updated_at
            sql += ", updated_at = NOW() WHERE workflow_id = %s"
            params.append(workflow_id)

            cursor.execute(sql, tuple(params))
            db_conn.commit()

            logging.info(
                "✅ Successfully updated workflow %s status to %s.",
                workflow_id, new_status
            )
            return cursor.rowcount  # 可選：回傳受影響列數

    except Error as e:
        logging.error(
            "❌ Database error in update_request_status for workflow_id %s: %s",
            workflow_id, e
        )
        if db_conn:
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error(
            "❌ Unexpected error in update_request_status for workflow_id %s: %s",
            workflow_id, e
        )
        if db_conn:
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()

def apply_request_to_db(workflow_id):
    """
    第二階段：在請求被批准後呼叫。
    讀取 request_payload，並將變更正式應用到 vm_configurations 和 vm_disks 表。
    - 本函式自行建立/關閉 DB 連線（route 不處理 DB）
    """
    db_conn = get_db_connection()
    try:
        # 1) 讀取 request_payload
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT request_payload FROM workflow_runs WHERE workflow_id = %s",
                (workflow_id,)
            )
            workflow = cursor.fetchone()

        if not workflow or not workflow.get('request_payload'):
            raise ValueError(f"Workflow {workflow_id} not found or has no payload.")

        # 2) 解析 JSON
        form_data = json.loads(workflow['request_payload'])
        action_type = _Helpers._first_scalar(form_data.get('action_type'))

        # 3) 依 action_type 分派；傳入同一個 db_conn
        if action_type == 'create':
            _apply_create_action(db_conn, form_data)
        elif action_type == 'update':
            _apply_update_action(db_conn, form_data)
        else:
            raise ValueError(f"Unsupported action_type: {action_type}")

        # 4) 更新狀態為 IN_PROGRESS
        update_request_status(workflow_id, 'IN_PROGRESS')

    except Error as e:
        logging.error(f"❌ Database error in apply_request_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error(f"❌ Unexpected error in apply_request_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()

def delete_draft_by_workflow_id(workflow_id):
    """
    刪除指定 workflow_id 的 DRAFT。
    - 回傳受影響列數（0 代表沒刪到）
    - 連線在本函式內部建立與關閉
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM workflow_runs WHERE workflow_id=%s AND status='DRAFT'",
                (workflow_id,)
            )
            affected = cursor.rowcount
        db_conn.commit()
        return affected

    except Error as e:
        logging.error(f"[delete_draft_by_id] DB error: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error(f"[delete_draft_by_id] Unexpected error: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()

# --- Private Helper functions for apply_request_to_db ---
def _apply_create_action(db_conn, form_data):
    """私有函式：處理 Create 請求的資料庫寫入"""
    cursor = None
    try:
        cursor = db_conn.cursor()

        # 1) 插入主表
        sql_vm_config = """
            INSERT INTO vm_configurations (
                environment, resource, os_type, vsphere_host, vsphere_datacenter, vsphere_cluster, vsphere_esxi_host,
                vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
                vm_instance_type, vm_num_cpus, vm_memory, vm_scsi_controller_count,
                vm_ipv4_gateway, netbox_prefix, netbox_tenant
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params_vm_config = (
            _Helpers._first_scalar(form_data.get('environment')),
            _Helpers._first_scalar(form_data.get('resource')),
            _Helpers._first_scalar(form_data.get('os_type')),
            _Helpers._first_scalar(form_data.get('vsphere_host')),
            _Helpers._first_scalar(form_data.get('vsphere_datacenter')),
            _Helpers._first_scalar(form_data.get('vsphere_cluster')),
            _Helpers._first_scalar(form_data.get('vsphere_esxi_host')),
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
        logging.info("   -> Applied CREATE action for vm_config_id: %s", vm_config_id)

        # 2) 插入附屬表
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

            rows = []
            for i, size in enumerate(disk_sizes):
                rows.append((
                    vm_config_id,
                    _Helpers._to_int(disk_scsis[i] if i < len(disk_scsis) else 0, 0),
                    _Helpers._to_int(disk_units[i] if i < len(disk_units) else (i + 1), i + 1),
                    _Helpers._to_int(size, 1),
                    (disk_provs[i] if i < len(disk_provs) else 'thin'),
                    'PENDING_CREATION',
                    i + 2
                ))
            cursor.executemany(sql_vm_disk, rows)
            logging.info("   -> Marked %d disks as PENDING_CREATION for vm_config_id: %s", len(rows), vm_config_id)

        # ★ 關鍵：提交
        db_conn.commit()

    except Error as e:
        logging.error("❌ DB error in _apply_create_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in _apply_create_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()

from mysql.connector import Error
import logging

def _apply_update_action(db_conn, form_data):
    """私有函式：處理 Update 請求的資料庫寫入"""
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            # 1) 取關鍵欄位
            env    = _Helpers._first_scalar(form_data.get('environment'))
            prefix = _Helpers._first_scalar(form_data.get('vm_name_prefix'))

            # 2) 查詢目標 vm_config_id
            cursor.execute(
                "SELECT id FROM vm_configurations WHERE environment = %s AND vm_name_prefix = %s",
                (env, prefix)
            )
            vm_config = cursor.fetchone()
            if not vm_config:
                raise ValueError(f"Cannot apply update: VM '{prefix}' in '{env}' not found.")
            vm_config_id = vm_config['id']

            # 3) 更新主表（此處僅示範 CPU / Memory；維持你原本欄位）
            cursor.execute(
                """
                UPDATE vm_configurations
                SET vm_num_cpus = %s,
                    vm_memory   = %s,
                    updated_at  = NOW()
                WHERE id = %s
                """,
                (
                    _Helpers._to_int(form_data.get('vm_num_cpus')),
                    _Helpers._to_int(form_data.get('vm_memory')),
                    vm_config_id
                )
            )

            logging.info("   -> Applied UPDATE action for vm_config_id: %s", vm_config_id)

            # 4) 同步 vm_disks（你註解說後面再做，這裡先保留空位）
            # TODO: diffs = _diff_disks(...)
            # TODO: 標記/新增/刪除對應磁碟（PENDING_UPDATE / PENDING_DELETION ...）

        # 5) 成功就提交
        db_conn.commit()

    except Error as e:
        logging.error("❌ DB error in _apply_update_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in _apply_update_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise

def return_request(workflow_id, reason, returned_by):
    """
    將 workflow_runs 設為 RETURNED，並紀錄 returned_reason。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            cursor.execute("""
                UPDATE workflow_runs
                SET status = 'RETURNED',
                    returned_by = %s,
                    returned_reason = %s,
                    updated_at = NOW()
                WHERE workflow_id = %s
            """, (returned_by, reason, workflow_id))
        db_conn.commit()
        logging.info(f"✅ Workflow {workflow_id} set to RETURNED by {returned_by}, reason={reason}")
    except Error as e:
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        logging.error(f"❌ DB error in return_request for workflow_id={workflow_id}: {e}")
        raise
    except Exception as e:
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        logging.error(f"❌ Unexpected error in return_request for workflow_id={workflow_id}: {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()