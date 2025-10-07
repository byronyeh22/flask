# app/fortigate/task_list/db/task_list_handler.py
from typing import Optional, Tuple, List
from app.db.mysql import get_db_connection
import json

def _dc(conn):
    return conn.cursor(dictionary=True)

def list_tasks_for_page(
    *,
    status: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    draft_id: Optional[int] = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
) -> Tuple[List[dict], int]:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        where = []
        params: list = []

        # 是否視為「搜尋」（有任一條件即是）
        is_search = bool(date_from or date_to or q or status or (draft_id is not None))
        if is_search:
            # 搜尋時排除 canceled
            where.append("t.status <> 'canceled'")

        # 1) 狀態過濾（逗號多選；搜尋時一律忽略 canceled）
        if status:
            sts = [s.strip().lower() for s in status.split(",") if s.strip()]
            if is_search:
                sts = [s for s in sts if s != "canceled"]
            if sts:
                where.append("t.status IN (" + ",".join(["%s"] * len(sts)) + ")")
                params.extend(sts)

        # 2) 指定某個 draft
        if draft_id is not None:
            where.append("t.draft_id = %s")
            params.append(int(draft_id))

        # 3) 關鍵字（task/draft/user/pipeline/job/sha）
        if q:
            like = f"%{q}%"
            where.append("("
                         "CAST(t.id AS CHAR) LIKE %s OR "
                         "CAST(d.id AS CHAR) LIKE %s OR "
                         "d.title LIKE %s OR "
                         "u.username LIKE %s OR "
                         "CAST(t.gitlab_pipeline_id AS CHAR) LIKE %s OR "
                         "CAST(t.gitlab_job_id AS CHAR) LIKE %s OR "
                         "t.git_commit_sha LIKE %s"
                         ")")
            params.extend([like, like, like, like, like, like, like])

        # 4) 時間區間（只看 action 的 started_at / finished_at）
        #    條件：「任一 action 的 started_at 或 finished_at 落在區間」
        if date_from or date_to:
            if date_from and date_to:
                where.append("""
                    EXISTS (
                      SELECT 1 FROM forti_task_action_results ar2
                      WHERE ar2.task_id = t.id
                        AND (
                             (ar2.started_at  BETWEEN %s AND %s) OR
                             (ar2.finished_at BETWEEN %s AND %s)
                        )
                    )
                """)
                params.extend([date_from, date_to, date_from, date_to])
            elif date_from:
                where.append("""
                    EXISTS (
                      SELECT 1 FROM forti_task_action_results ar2
                      WHERE ar2.task_id = t.id
                        AND (
                             ar2.started_at  >= %s OR
                             ar2.finished_at >= %s
                        )
                    )
                """)
                params.extend([date_from, date_from])
            else:  # 只有上限
                where.append("""
                    EXISTS (
                      SELECT 1 FROM forti_task_action_results ar2
                      WHERE ar2.task_id = t.id
                        AND (
                             ar2.started_at  <= %s OR
                             ar2.finished_at <= %s
                        )
                    )
                """)
                params.extend([date_to, date_to])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        # ----- total（與主查詢相同 WHERE）-----
        sql_total = f"""
            SELECT COUNT(DISTINCT t.id) AS c
            FROM forti_tasks t
            LEFT JOIN forti_drafts d ON d.id = t.draft_id
            LEFT JOIN users u ON u.id = d.created_by
            {where_sql}
        """
        cur.execute(sql_total, tuple(params))
        total = int(cur.fetchone()["c"])

        # ----- 排序白名單 -----
        sort_by_whitelist = {
            "draft_id": "d.id",
            "task_id": "t.id",
            "task_status": "t.status",
            "first_started_at": "MIN(ar.started_at)",
            "last_finished_at": "MAX(ar.finished_at)",
        }
        sort_col = sort_by_whitelist.get((sort_by or "").strip().lower(), "t.id")
        sort_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

        # ----- 主查詢 -----
        sql = f"""
        SELECT
            t.id AS task_id,
            t.status AS task_status,
            t.created_at,
            t.updated_at,
            t.gitlab_pipeline_id,
            t.gitlab_pipeline_url,

            d.id    AS draft_id,
            d.title AS draft_title,
            d.status AS draft_status,
            u.username AS created_by_name,

            COALESCE(SUM(ar.status='ok'), 0)      AS ok_cnt,
            COALESCE(SUM(ar.status='error'), 0)   AS err_cnt,
            COALESCE(SUM(ar.status='skipped'), 0) AS skipped_cnt,
            COALESCE(COUNT(ar.id), 0)             AS total_cnt,

            MIN(ar.started_at)  AS first_started_at,
            MAX(ar.finished_at) AS last_finished_at,

            COALESCE(JSON_LENGTH(JSON_EXTRACT(d.draft_action, '$.action_plan')), 0) AS action_count,
            TIMESTAMPDIFF(SECOND, MIN(ar.started_at), MAX(ar.finished_at)) AS duration_sec
        FROM forti_tasks t
        LEFT JOIN forti_drafts d ON d.id = t.draft_id
        LEFT JOIN users u ON u.id = d.created_by
        LEFT JOIN forti_task_action_results ar ON ar.task_id = t.id
        {where_sql}
        GROUP BY t.id
        ORDER BY {sort_col} {sort_dir}
        LIMIT %s OFFSET %s
        """
        cur.execute(sql, (*params, limit, offset))
        rows = cur.fetchall() or []

        return rows, total
    finally:
        cur.close(); conn.close()

def get_task_actions(task_id: int) -> List[dict]:
    """
    取得 forti_task_action_results
    - 別名 deploy_message -> deploy_meta、rollback -> rollback_meta
    - JOIN forti_devices 取得 device_name
    - 以 action_id 升冪排序（同 action_id 再以 id 升冪）
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("""
            SELECT
              ar.action_id,
              ar.kind,
              ar.action_type,
              ar.device_id,
              fd.name AS device_name,
              ar.vdom,
              ar.resource_id,
              ar.status,
              ar.started_at,
              ar.finished_at,
              ar.deploy_message AS deploy_meta,
              ar.rollback      AS rollback_meta
            FROM forti_task_action_results ar
            LEFT JOIN forti_devices fd ON fd.id = ar.device_id
            WHERE ar.task_id=%s
            ORDER BY ar.action_id ASC, ar.id ASC
        """, (task_id,))
        rows = cur.fetchall() or []

        for r in rows:
            for k in ("deploy_meta", "rollback_meta"):
                v = r.get(k, None)
                if isinstance(v, (dict, list)):
                    r[k] = json.dumps(v, ensure_ascii=False)
                elif v is None:
                    r[k] = ""
                else:
                    r[k] = str(v)
        return rows
    finally:
        cur.close(); conn.close()

