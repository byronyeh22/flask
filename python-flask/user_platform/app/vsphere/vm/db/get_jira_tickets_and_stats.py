def get_jira_tickets_and_stats(db_conn):
    """獲取所有 jira 資料（用於 overview 頁面）"""
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT *
        FROM jira_tickets
        ORDER BY created_at DESC
    """)
    jira_tickets = cursor.fetchall()

    cursor.close()
    # db_conn.close()
    return jira_tickets

def get_jira_ticket_by_pipeline_id(db_conn, pipeline_id):
    """根據 pipeline_id 獲取對應的 Jira ticket 資訊"""
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT jira_tickets.*
        FROM jira_tickets
        JOIN gitlab_pipelines ON jira_tickets.workflow_id = gitlab_pipelines.workflow_id
        WHERE gitlab_pipelines.pipeline_id = %s
    """, (pipeline_id,))
    jira_ticket = cursor.fetchone()

    cursor.close()
    return jira_ticket