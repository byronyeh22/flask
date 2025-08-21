# app/vsphere/vm/db/insert_gitlab_pipeline_info_to_db.py
from mysql.connector import Error
import logging

# 建議使用 logging 模組，可以提供更豐富的日誌級別和格式
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_data):
    """
    將觸發 GitLab Pipeline 當下的快照資訊寫入資料庫。
    此函式現在的職責是儲存 GitLab Pipeline 的執行資訊。

    Args:
        db_conn: 資料庫連線物件。
        workflow_id (int): 對應的 workflow_runs ID。
        pipeline_data (dict): 從 GitLab trigger API 回傳的資料。
    """
    cursor = None

    sql = """
        INSERT INTO gitlab_pipelines (
            workflow_id, pipeline_id, project_name, branch,
            commit_sha, status, started_at, finished_at, duration, web_url
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """

    try:
        params = (
            workflow_id,
            pipeline_data.get("id"),
            f"project-{pipeline_data.get('project_id')}",
            pipeline_data.get("ref"),
            pipeline_data.get("sha"),
            pipeline_data.get("status"),
            pipeline_data.get("created_at"),
            pipeline_data.get("finished_at"),
            pipeline_data.get("duration"),
            pipeline_data.get("web_url"),
        )

        cursor = db_conn.cursor()
        cursor.execute(sql, params)
        db_conn.commit()

        logging.info(f"✅ Successfully inserted GitLab pipeline info for workflow_id: {workflow_id}, pipeline_id: {pipeline_data.get('id')}")

    except Error as e:
        # 當資料庫操作出錯時，提供詳細的錯誤回饋
        logging.error(f"❌ Database error in insert_gitlab_pipeline_info_to_db for workflow_id: {workflow_id}")
        logging.error(f"   - MySQL Error: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise

    except Exception as e:
        # 捕捉其他非資料庫的預期外錯誤
        logging.error(f"❌ An unexpected error occurred in insert_gitlab_pipeline_info_to_db: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise

    finally:
        if cursor:
            cursor.close()