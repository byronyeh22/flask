# app/vsphere/vm/db/insert_jira_info_to_db.py
from mysql.connector import Error
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def insert_jira_info_to_db(db_conn, workflow_id, ticket_data):
    """
    將 Jira Ticket 資訊寫入資料庫。
    
    Args:
        db_conn: 資料庫連線物件。
        workflow_id (int): 對應的 workflow_runs ID。
        ticket_data (dict): 從 Jira API 回傳的資料。
    """
    cursor = None

    sql = """
        INSERT INTO jira_tickets (
            workflow_id, ticket_id, project_key, summary,
            description, status, url, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    try:
        cursor = db_conn.cursor()
        params = (
            workflow_id,
            ticket_data.get("ticket_id"),
            ticket_data.get("project_key"),
            ticket_data.get("summary", ""),
            ticket_data.get("description", ""),
            ticket_data.get("status", ""),
            ticket_data.get("url", ""),
            ticket_data.get("created_at"),
        )
        cursor.execute(sql, params)
        db_conn.commit()

        logging.info(f"✅ Successfully inserted Jira ticket info for workflow_id: {workflow_id}, ticket_id: {ticket_data.get('ticket_id')}")
    
    except Error as e:
        logging.error(f"❌ Database error in insert_jira_info_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error(f"❌ An unexpected error occurred in insert_jira_info_to_db: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()