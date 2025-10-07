# app/fortigate/policy/db/policy_handler.py
from typing import List, Tuple, Optional
from app.db.mysql import get_db_connection

LIST_SQL = """
WITH latest_task AS (
  SELECT t.*
  FROM forti_tasks t
  JOIN (
    SELECT draft_id, MAX(id) AS max_id
    FROM forti_tasks
    GROUP BY draft_id
  ) x ON x.draft_id = t.draft_id AND x.max_id = t.id
),
task_result_sum AS (
  SELECT task_id,
         SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)     AS ok_cnt,
         SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)  AS err_cnt,
         SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END)AS skip_cnt
  FROM forti_task_action_results
  GROUP BY task_id
)
SELECT d.id          AS draft_id,
       d.title       AS title,
       d.status      AS draft_status,
       d.created_at  AS created_at,
       d.updated_at  AS updated_at,
       u.username    AS created_by,
       lt.id         AS task_id,
       lt.status     AS task_status,
       COALESCE(tr.ok_cnt,0)   AS ok_cnt,
       COALESCE(tr.err_cnt,0)  AS err_cnt,
       COALESCE(tr.skip_cnt,0) AS skip_cnt
FROM forti_drafts d
JOIN users u ON u.id = d.created_by
LEFT JOIN latest_task lt ON lt.draft_id = d.id
LEFT JOIN task_result_sum tr ON tr.task_id = lt.id
WHERE ( %(status)s IS NULL OR d.status = %(status)s )
  AND ( %(keyword)s IS NULL
        OR d.title LIKE CONCAT('%%', %(keyword)s, '%%')
        OR u.username LIKE CONCAT('%%', %(keyword)s, '%%')
      )
ORDER BY d.created_at DESC
LIMIT %(limit)s OFFSET %(offset)s
"""

COUNT_SQL = """
SELECT COUNT(*)
FROM forti_drafts d
JOIN users u ON u.id = d.created_by
WHERE ( %(status)s IS NULL OR d.status = %(status)s )
  AND ( %(keyword)s IS NULL
        OR d.title LIKE CONCAT('%%', %(keyword)s, '%%')
        OR u.username LIKE CONCAT('%%', %(keyword)s, '%%')
      )
"""

def query_policy_request_list(page: int, per_page: int,
                              status: Optional[str], keyword: Optional[str]) -> Tuple[List[dict], int]:
    offset = (page - 1) * per_page
    params = {
        "status": status if status else None,
        "keyword": keyword if keyword else None,
        "limit": per_page,
        "offset": offset,
    }
    conn = get_db_connection()
    try:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(LIST_SQL, params)
            rows = cur.fetchall()
            cur.execute(COUNT_SQL, params)
            total = cur.fetchone()["COUNT(*)"]
        return rows, total
    finally:
        conn.close()

def verify_runner_token_by_draft(draft_id: int, token: str) -> bool:
    """
    提供給 app.decorators.decorators.login_or_callback_required 使用：
    只要該 draft_id 底下「任一」 forti_tasks 的 callback_token 符合即放行。
    嚴格的 task/draft/token 對應由各 callback route 內再核對。
    """
    if not draft_id or not token:
        return False
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM forti_tasks
                 WHERE draft_id = %s
                   AND callback_token = %s
                 LIMIT 1
                """,
                (int(draft_id), str(token).strip()),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()

