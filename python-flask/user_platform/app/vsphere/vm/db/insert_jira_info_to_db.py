# app/vsphere/vm/db/insert_jira_info_to_db.py
from mysql.connector import Error
import logging
from app.mysql.db import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def insert_jira_info_to_db(workflow_id, ticket_data):
    """
    將 Jira Ticket 資訊寫入資料庫。
    直接使用 get_jira_issue_detail 傳來的、已是 UTC+8 的時間字串。
    """
    sql = """
        INSERT INTO jira_tickets (
            workflow_id, ticket_id, project_key, summary,
            description, status, url, created_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            COALESCE(%s, NOW())
        )
    """

    try:
        db_conn = get_db_connection()
        with db_conn.cursor() as cursor:
            # 直接使用傳入的時間字串，不再進行任何解析
            created_dt = ticket_data.get("created_at")

            params = (
                workflow_id,
                ticket_data.get("ticket_id"),
                ticket_data.get("project_key"),
                ticket_data.get("summary", ""),
                ticket_data.get("description", ""),
                ticket_data.get("status", ""),
                ticket_data.get("url", ""),
                created_dt,  # -> COALESCE(%s, NOW())
            )

            cursor.execute(sql, params)
            db_conn.commit()

            # 【修正】日誌記錄：直接印出 created_dt 字串，不再呼叫 .isoformat()
            logging.info(
                "✅ Inserted Jira ticket: wf=%s, ticket=%s, created_at=%s",
                workflow_id,
                ticket_data.get("ticket_id"),
                created_dt if created_dt else "NOW()"
            )

    except Error as e:
        logging.error("❌ DB error in insert_jira_info_to_db (wf=%s): %s", workflow_id, e)
        if db_conn:
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in insert_jira_info_to_db: %s", e)
        if db_conn:
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()