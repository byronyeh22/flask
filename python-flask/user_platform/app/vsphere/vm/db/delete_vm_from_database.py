# app/vsphere/vm/db/delete_vm_from_database.py
from mysql.connector import Error
import logging
import json
from app.mysql.db import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def delete_vm_from_database(workflow_id):
    """
    刪除 vm_configurations 表中的 VM 記錄
    由於有 ON DELETE CASCADE，相關的 vm_disks 也會自動刪除

    Args:
        workflow_id: workflow ID

    Returns:
        bool: 成功刪除返回 True，失敗或找不到記錄返回 False
    """
    db_conn = None
    try:
        db_conn = get_db_connection()

        # 從 workflow 取得 VM 識別資訊
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
            row = cursor.fetchone()

            if not row or not row.get("request_payload"):
                logging.warning(f"Cannot delete VM: No request_payload for workflow {workflow_id}")
                return False

            payload = json.loads(row["request_payload"])
            original_config = payload.get("original_config", {})

            environment = (original_config.get("environment") or "").strip()
            vm_name_prefix = (original_config.get("vm_name_prefix") or "").strip()

            if not environment or not vm_name_prefix:
                logging.warning(f"Cannot delete VM: Missing environment or vm_name_prefix for workflow {workflow_id}")
                return False

        # 刪除 vm_configurations 記錄（vm_disks 會因為 CASCADE 自動刪除）
        with db_conn.cursor() as delete_cursor:
            delete_cursor.execute(
                """
                DELETE FROM vm_configurations
                WHERE environment = %s AND vm_name_prefix = %s
                """,
                (environment, vm_name_prefix)
            )
            deleted_count = delete_cursor.rowcount
            db_conn.commit()

            if deleted_count > 0:
                logging.info(
                    "Successfully deleted VM record: env=%s, vm=%s (workflow %s)",
                    environment, vm_name_prefix, workflow_id
                )
                return True
            else:
                logging.warning(
                    "No VM record found to delete: env=%s, vm=%s (workflow %s)",
                    environment, vm_name_prefix, workflow_id
                )
                return False

    except Error as e:
        logging.error("DB error in delete_vm_from_database (wf=%s): %s", workflow_id, e)
        if db_conn:
            db_conn.rollback()
        return False
    except Exception as e:
        logging.error("Unexpected error in delete_vm_from_database (wf=%s): %s", workflow_id, e)
        if db_conn:
            db_conn.rollback()
        return False
    finally:
        if db_conn:
            db_conn.close()