# app/vsphere/vm/db/insert_jira_info_to_db.py
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
        if ts_val.tzinfo is not None:
            ts_val = ts_val.astimezone(timezone.utc).replace(tzinfo=None)
        return ts_val
    if isinstance(ts_val, str):
        try:
            # 盡量吃 Jira 的 ISO 格式，像 2025-08-26T15:59:25.506+0800 / ...+00:00 / ...Z
            dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    return datetime.strptime(ts_val, fmt)
                except Exception:
                    pass
    return None


def insert_jira_info_to_db(db_conn, workflow_id, ticket_data):
    """
    將 Jira Ticket 資訊寫入資料庫。
    會嘗試將 ticket_data['created_at'] 轉成 MySQL 可接受的 datetime；
    若無法解析則由 DB 端以 NOW() 帶入。
    """
    cursor = None

    sql = """
        INSERT INTO jira_tickets (
            workflow_id, ticket_id, project_key, summary,
            description, status, url, created_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            COALESCE(%s, NOW())
        )
    """

    try:
        cursor = db_conn.cursor()

        # 讓 created_at 支援多種輸入格式；不能解析就交給 COALESCE 用 NOW()
        created_dt = _parse_ts(ticket_data.get("created_at"))

        params = (
            workflow_id,
            ticket_data.get("ticket_id"),
            ticket_data.get("project_key"),
            ticket_data.get("summary", ""),
            ticket_data.get("description", ""),
            ticket_data.get("status", ""),
            ticket_data.get("url", ""),
            created_dt,  # -> COALESCE(%s, NOW())
        )

        cursor.execute(sql, params)
        db_conn.commit()

        logging.info(
            "✅ Inserted Jira ticket: wf=%s, ticket=%s, created_at=%s",
            workflow_id,
            ticket_data.get("ticket_id"),
            created_dt.isoformat(sep=' ') if created_dt else "NOW()"
        )

    except Error as e:
        logging.error("❌ DB error in insert_jira_info_to_db (wf=%s): %s", workflow_id, e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in insert_jira_info_to_db: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()