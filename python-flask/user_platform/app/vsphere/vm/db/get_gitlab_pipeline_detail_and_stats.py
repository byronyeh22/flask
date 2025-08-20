def get_gitlab_pipeline_detail_and_stats(db_conn):
    """獲取所有 pipeline 資料（用於 overview 頁面）"""
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT *
        FROM gitlab_pipelines
        ORDER BY created_at DESC
    """)
    pipeline_data = cursor.fetchall()

    cursor.close()
    return pipeline_data


def get_pipeline_details_by_id(db_conn, pipeline_id):
    """根據 pipeline_id 獲取特定 pipeline 的完整資訊（用於 approve 頁面）"""
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT *
        FROM gitlab_pipelines
        WHERE pipeline_id = %s
    """, (pipeline_id,))
    pipeline_data = cursor.fetchone()

    cursor.close()
    return pipeline_data


