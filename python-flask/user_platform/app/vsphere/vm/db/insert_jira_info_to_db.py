def insert_jira_info_to_db(db_conn, workflow_id, ticket_data):
    cursor = db_conn.cursor()

    cursor.execute("""
        INSERT INTO jira_tickets (
            workflow_id,
            ticket_id,
            project_key,
            summary,
            description,
            status,
            url
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        workflow_id,
        ticket_data["ticket_id"],
        ticket_data["project_key"],
        ticket_data.get("summary", ""),
        ticket_data.get("description", ""),
        ticket_data.get("status", ""),
        ticket_data.get("url", ""),
    ))

    db_conn.commit()
    cursor.close()