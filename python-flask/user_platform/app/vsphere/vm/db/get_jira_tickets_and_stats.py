# app/vsphere/vm/db/get_jira_tickets_and_stats.py
from mysql.connector import Error
from app.mysql.db import get_db_connection

def get_jira_tickets_and_stats():
    """
    取得 Jira tickets 列表（overview 顯示需要的欄位）
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT
                    workflow_id,
                    ticket_id,
                    project_key,
                    summary,
                    description,
                    status,
                    url,
                    created_at
                FROM jira_tickets
                ORDER BY created_at DESC
            """)
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"[get_jira_tickets_and_stats] DB error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()

def get_jira_ticket_by_workflow_id(workflow_id):
    """
    根據 workflow_id 獲取單一的 Jira ticket 資訊。找不到回傳 None
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT
                    workflow_id,
                    ticket_id,
                    project_key,
                    summary,
                    description,
                    status,
                    url,
                    created_at
                FROM jira_tickets
                WHERE workflow_id = %s
                LIMIT 1
            """, (workflow_id,))
            jira_ticket = cursor.fetchone()
            return jira_ticket
    except Exception as e:
        logging.error(f"[get_jira_ticket_by_workflow_id] DB error: {e}")
        return None
    finally:
        if db_conn:
            db_conn.close()

# def get_jira_ticket_by_pipeline_id(pipeline_id):
#     """
#     根據 pipeline_id 獲取對應的 Jira ticket 資訊。
#     - 找不到則回傳 None
#     """
#     db_conn = get_db_connection()
#     try:
#         with db_conn.cursor(dictionary=True) as cursor:
#             cursor.execute("""
#                 SELECT jt.workflow_id, jt.ticket_id, jt.project_key, jt.summary,
#                        jt.description, jt.status, jt.url, jt.created_at
#                 FROM jira_tickets jt
#                 JOIN gitlab_pipelines gp ON jt.workflow_id = gp.workflow_id
#                 WHERE gp.pipeline_id = %s
#             """, (pipeline_id,))
#             return cursor.fetchone()

#     except Error as e:
#         logging.error(f"[get_jira_ticket_by_pipeline_id] DB error: {e}")
#         return None
#     except Exception as e:
#         logging.error(f"[get_jira_ticket_by_pipeline_id] Unexpected error: {e}")
#         return None
#     finally:
#         if db_conn:
#             db_conn.close()