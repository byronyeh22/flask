# app/vsphere/vm/db/get_gitlab_pipeline_detail_and_stats.py

def get_gitlab_pipeline_detail_and_stats(db_conn):
    """獲取所有 pipeline 資料（用於 overview 頁面）"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT *
            FROM gitlab_pipelines
            ORDER BY created_at DESC
        """)
        pipeline_data = cursor.fetchall()
        return pipeline_data
    finally:
        # [修正] 確保 cursor 在函式結束時被關閉
        if cursor:
            cursor.close()


def get_pipeline_details_by_id(db_conn, pipeline_id):
    """根據 pipeline_id 獲取特定 pipeline 的完整資訊"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT *
            FROM gitlab_pipelines
            WHERE pipeline_id = %s
        """, (pipeline_id,))
        pipeline_data = cursor.fetchone()
        return pipeline_data
    finally:
        # [修正] 確保 cursor 在函式結束時被關閉
        if cursor:
            cursor.close()


def get_pipeline_details_by_workflow_id(db_conn, workflow_id):
    """根據 workflow_id 獲取單一的 GitLab pipeline 紀錄。"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT *
            FROM gitlab_pipelines
            WHERE workflow_id = %s
            LIMIT 1
        """, (workflow_id,))
        pipeline_data = cursor.fetchone()
        return pipeline_data
    finally:
        # [修正] 確保 cursor 在函式結束時被關閉
        if cursor:
            cursor.close()