# app/vsphere/vm/db/workflow_manager.py
import json
from mysql.connector import Error
import logging
from flask import session
from datetime import datetime
from app.mysql.db import get_db_connection

# 純 Workflow CRUD 操作
def get_all_workflow_runs():
    """取得所有 workflow_runs（包含 DRAFT）"""
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
    """儲存或更新草稿"""
    try:
        db_conn = get_db_connection()

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

        # 插入新的 DRAFT
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
    """取得單一 workflow"""
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

def get_workflow_status(workflow_id):
    """
    查詢 workflow 當前狀態
    Args:
        workflow_id (int): Workflow ID
    Returns:
        dict: 包含 success 和 status 的字典
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT status FROM workflow_runs WHERE workflow_id = %s",
                (workflow_id,)
            )
            row = cur.fetchone()
            if row:
                return {
                    "success": True,
                    "status": row.get("status", "")
                }
            else:
                return {
                    "success": False,
                    "error": f"Workflow {workflow_id} not found"
                }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        if db_conn:
            db_conn.close()

def update_request_status(workflow_id, new_status, approver=None, failed_message=None):
    """更新工作流狀態"""
    try:
        db_conn = get_db_connection()
        with db_conn.cursor() as cursor:
            sql = "UPDATE workflow_runs SET status = %s"
            params = [new_status]

            if approver:
                sql += ", approved_by = %s, approved_at = NOW()"
                params.append(approver)

            if failed_message:
                sql += ", failed_message = %s"
                params.append(failed_message)

            if new_status.upper() == "IN_PROGRESS":
                sql += ", submitted_at = COALESCE(submitted_at, NOW())"

            sql += ", updated_at = NOW() WHERE workflow_id = %s"
            params.append(workflow_id)

            cursor.execute(sql, tuple(params))
            db_conn.commit()

            logging.info(f"✅ Successfully updated workflow {workflow_id} status to {new_status}.")
            return cursor.rowcount

    except Error as e:
        logging.error(f"❌ Database error in update_request_status for workflow_id {workflow_id}: {e}")
        if db_conn:
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error(f"❌ Unexpected error in update_request_status for workflow_id {workflow_id}: {e}")
        if db_conn:
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()

def delete_draft_by_workflow_id(workflow_id):
    """刪除指定 workflow_id 的 DRAFT"""
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

def return_request(workflow_id, reason, returned_by, target_status="RETURNED"):
    """將 workflow_runs 設為 RETURNED 或 CANCELED"""
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            # 使用傳入的 target_status
            cursor.execute("""
                UPDATE workflow_runs
                SET status = %s,
                    returned_by = %s,
                    returned_reason = %s,
                    updated_at = NOW()
                WHERE workflow_id = %s
            """, (target_status, returned_by, reason, workflow_id))
        db_conn.commit()
        logging.info(f"✅ Workflow {workflow_id} set to {target_status} by {returned_by}, reason={reason}")
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