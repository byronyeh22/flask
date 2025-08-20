from datetime import datetime, timedelta

def parse_gitlab_datetime(dt_str):
    """
    將 GitLab API 的 ISO 8601 時間字串轉換成 MySQL 能接受的 datetime（轉為 UTC+8）
    """
    if not dt_str:
        return None
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
    # 轉換為 UTC+8
    return dt + timedelta(hours=8)



def update_gitlab_pipeline_details(db_conn, pipeline_id, pipeline_data):
    """
    更新 GitLab pipeline 的詳細資訊（status、web_url、finished_at、duration）

    Args:
        db_conn: 資料庫連線物件
        pipeline_id (str): Pipeline ID
        pipeline_data (dict): GitLab API 回傳的資料
    """
    cursor = db_conn.cursor()

    try:
        cursor.execute("""
            UPDATE gitlab_pipelines
            SET
                status = %s,
                web_url = %s,
                finished_at = %s,
                duration = %s
            WHERE pipeline_id = %s
        """, (
            pipeline_data.get("status", "unknown"),
            pipeline_data.get("web_url", "unknown"),
            parse_gitlab_datetime(pipeline_data.get("finished_at")),
            pipeline_data.get("duration", None),
            pipeline_id
        ))

        db_conn.commit()
        print(f"✅ Updated pipeline {pipeline_id} details successfully.")

    except Exception as e:
        db_conn.rollback()
        print(f"❌ Error updating pipeline details: {str(e)}")
        raise
    finally:
        cursor.close()
