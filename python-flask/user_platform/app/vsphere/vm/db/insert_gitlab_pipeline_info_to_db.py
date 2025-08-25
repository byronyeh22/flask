from mysql.connector import Error
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _parse_ts(ts_val):
    """
    將多種格式的時間值轉成「UTC naive datetime」。
    - 支援 Python datetime（aware/naive）、ISO8601 字串（含/不含時區）。
    - 無法解析則回傳 None，交給 SQL 的 COALESCE 用 NOW()。
    """
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        # 轉成 UTC 再去掉 tzinfo（naive）
        if ts_val.tzinfo is not None:
            ts_val = ts_val.astimezone(timezone.utc).replace(tzinfo=None)
        return ts_val
    if isinstance(ts_val, str):
        try:
            # Python 3.11+ 支援 fromisoformat 大多數 ISO 8601
            dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            # 最後再嘗試常見格式
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(ts_val, fmt)
                except Exception:
                    pass
    return None

def insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_data):
    """
    將觸發 GitLab Pipeline 當下的快照資訊寫入資料庫。
    - started_at 優先取 pipeline_data['started_at']，沒有就用 pipeline_data['created_at']。
    - 寫入時若為 None，DB 端以 NOW() 帶入目前時間（COALESCE）。
    """
    cursor = None

    sql = """
        INSERT INTO gitlab_pipelines (
            workflow_id, pipeline_id, project_name, branch,
            commit_sha, status, started_at, finished_at, duration, web_url
        ) VALUES (
            %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s, %s
        )
    """

    try:
        # 1) 取回應中的時間並正規化
        started_raw  = pipeline_data.get("started_at") or pipeline_data.get("created_at")
        finished_raw = pipeline_data.get("finished_at")

        started_at_dt  = _parse_ts(started_raw)    # -> datetime or None
        finished_at_dt = _parse_ts(finished_raw)   # -> datetime or None

        # 2) 組參數
        params = (
            workflow_id,
            pipeline_data.get("id"),
            f"project-{pipeline_data.get('project_id')}",
            pipeline_data.get("ref"),
            pipeline_data.get("sha"),
            pipeline_data.get("status"),
            started_at_dt,                 # -> COALESCE(%s, NOW())
            finished_at_dt,
            pipeline_data.get("duration"),
            pipeline_data.get("web_url"),
        )

        # 3) 寫入
        cursor = db_conn.cursor()
        cursor.execute(sql, params)
        db_conn.commit()

        logging.info(
            "✅ Inserted GitLab pipeline: wf=%s, pipeline=%s, started_at=%s",
            workflow_id, pipeline_data.get("id"),
            started_at_dt.isoformat(sep=' ') if started_at_dt else "NOW()"
        )

    except Error as e:
        logging.error("❌ DB error in insert_gitlab_pipeline_info_to_db (wf=%s): %s", workflow_id, e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in insert_gitlab_pipeline_info_to_db: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()