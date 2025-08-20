def get_jira_tickets_and_stats(db_conn):

    cursor = db_conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT workflow_id, ticket_id, summary, description, status, url
        FROM jira_tickets
        ORDER BY created_at DESC
    """)
    jira_tickets = cursor.fetchall()

    cursor.close()
    db_conn.close()
    return jira_tickets