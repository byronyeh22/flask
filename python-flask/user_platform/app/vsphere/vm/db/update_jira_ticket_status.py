from mysql.connector import Error
import logging
from app.mysql.db import get_db_connection

def update_jira_ticket_status(ticket_id: str, new_status: str) -> bool:
    """
    更新 jira_tickets 表中指定 ticket 的狀態。
    內部自行建立/關閉 DB 連線。

    Args:
        ticket_id (str): Jira 工單編號 (例如 "SJT-123")
        new_status (str): 要更新的狀態 (例如 "Done", "RETURNED")

    Returns:
        bool: True 表示成功, False 表示找不到或失敗
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            sql = """
                UPDATE jira_tickets
                SET status = %s, updated_at = NOW()
                WHERE ticket_id = %s
            """
            cursor.execute(sql, (new_status, ticket_id))
            db_conn.commit()

            if cursor.rowcount == 0:
                logging.warning(f"[update_jira_ticket_status] Ticket {ticket_id} not found in DB.")
                return False

            logging.info(f"[update_jira_ticket_status] Ticket {ticket_id} updated to status={new_status}")
            return True

    except Error as e:
        logging.error(f"[update_jira_ticket_status] DB error: {e}")
        if db_conn:
            db_conn.rollback()
        return False

    except Exception as e:
        logging.error(f"[update_jira_ticket_status] Unexpected error: {e}")
        if db_conn:
            db_conn.rollback()
        return False

    finally:
        if db_conn:
            db_conn.close()