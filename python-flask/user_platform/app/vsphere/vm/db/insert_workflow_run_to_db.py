def insert_workflow_run_to_db(db_conn, triggered_by):
    cursor = db_conn.cursor()

    cursor.execute("""
        INSERT INTO workflow_runs (triggered_by)
        VALUES (%s)
    """, (triggered_by,))

    db_conn.commit()
    workflow_id = cursor.lastrowid
    cursor.close()
    return workflow_id