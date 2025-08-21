# app/vsphere/vm/db/get_jira_tickets_and_stats.py

def get_jira_tickets_and_stats(db_conn):
    """
    獲取所有 jira 資料（用於 overview 頁面）
    """
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT *
            FROM jira_tickets
            ORDER BY created_at DESC
        """)
        jira_tickets = cursor.fetchall()
        return jira_tickets
    finally:
        # [修正] 確保 cursor 在函式結束時被關閉
        if cursor:
            cursor.close()

def get_jira_ticket_by_workflow_id(db_conn, workflow_id):
    """
    根據 workflow_id 獲取單一的 Jira ticket 資訊。
    """
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT *
            FROM jira_tickets
            WHERE workflow_id = %s
            LIMIT 1
        """, (workflow_id,))
        jira_ticket = cursor.fetchone()
        return jira_ticket
    finally:
        # [修正] 確保 cursor 在函式結束時被關閉
        if cursor:
            cursor.close()

def get_jira_ticket_by_pipeline_id(db_conn, pipeline_id):
    """根據 pipeline_id 獲取對應的 Jira ticket 資訊"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT jt.*
            FROM jira_tickets jt
            JOIN gitlab_pipelines gp ON jt.workflow_id = gp.workflow_id
            WHERE gp.pipeline_id = %s
        """, (pipeline_id,))
        jira_ticket = cursor.fetchone()
        return jira_ticket
    finally:
        # [修正] 確保 cursor 在函式結束時被關閉
        if cursor:
            cursor.close()