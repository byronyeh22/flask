# app/vsphere/vm/db/get_gitlab_pipeline_detail_and_stats.py
from mysql.connector import Error
import logging
from app.mysql.db import get_db_connection

def get_gitlab_pipeline_detail_and_stats():
    """獲取所有 pipeline 資料（用於 overview 頁面）"""
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT
                    workflow_id,
                    pipeline_id,
                    job_id,
                    project_name,
                    branch,
                    commit_sha,
                    status,
                    started_at,
                    finished_at,
                    duration,
                    web_url
                FROM gitlab_pipelines
                ORDER BY started_at DESC
            """)
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"[get_gitlab_pipeline_detail_and_stats] DB error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()

def get_pipeline_details_by_workflow_id(workflow_id):
    """
    根據 workflow_id 獲取單一的 GitLab pipeline 紀錄。找不到回傳 None
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT
                    workflow_id,
                    pipeline_id,
                    job_id,
                    project_name,
                    branch,
                    commit_sha,
                    status,
                    started_at,
                    finished_at,
                    duration,
                    web_url
                FROM gitlab_pipelines
                WHERE workflow_id = %s
                LIMIT 1
            """, (workflow_id,))
            pipeline_data = cursor.fetchone()
            return pipeline_data
    except Exception as e:
        logging.error(f"[get_pipeline_details_by_workflow_id] DB error: {e}")
        return None
    finally:
        if db_conn:
            db_conn.close()

# def get_pipeline_details_by_id(pipeline_id):
#     """
#     根據 pipeline_id 獲取特定 pipeline 的完整資訊。
#     - 找不到時回傳 None
#     """
#     db_conn = get_db_connection()
#     try:
#         with db_conn.cursor(dictionary=True) as cursor:
#             cursor.execute(
#                 """
#                 SELECT workflow_id, pipeline_id, job_id, project_name,
#                        branch, commit_sha, status, started_at,
#                        finished_at, duration, web_url
#                 FROM gitlab_pipelines
#                 WHERE pipeline_id = %s
#                 """,
#                 (pipeline_id,)
#             )
#             return cursor.fetchone()

#     except Error as e:
#         logging.error(f"[get_pipeline_details_by_id] DB error: {e}")
#         if db_conn and db_conn.is_connected():
#             db_conn.rollback()
#         return None
#     except Exception as e:
#         logging.error(f"[get_pipeline_details_by_id] Unexpected error: {e}")
#         if db_conn and db_conn.is_connected():
#             db_conn.rollback()
#         return None
#     finally:
#         if db_conn:
#             db_conn.close()