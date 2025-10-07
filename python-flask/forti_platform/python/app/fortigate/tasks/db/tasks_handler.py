# app/fortigate/tasks/db/tasks_handler.py
import json, secrets
from typing import Optional, List, Tuple
from app.db.mysql import get_db_connection
from ...workflow import TaskStatus

def _dc(conn):
    return conn.cursor(dictionary=True)

def _new_callback_token() -> str:
    return secrets.token_urlsafe(32)

def create_task_for_draft(draft_id: int, created_by: int, options: Optional[dict]=None) -> Tuple[int, str]:
    """
    Upsert：保持 draft_id 唯一。
      - 若已存在此 draft 的 task → 沿用同一 task_id，清除舊 results，重置欄位與狀態為 pending。
      - 若不存在 → 新增一筆。
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        token = _new_callback_token()
        # 鎖住此 draft 的舊 task（若有）
        cur.execute("SELECT id FROM forti_tasks WHERE draft_id=%s FOR UPDATE", (draft_id,))
        old = cur.fetchone()

        if old:
            task_id = old["id"]
            # 清掉舊 results，避免殘留
            cur.execute("DELETE FROM forti_task_action_results WHERE task_id=%s", (task_id,))
            # 重置任務資料（覆蓋模式）
            cur.execute("""
                UPDATE forti_tasks
                   SET status=%s,
                       callback_token=%s,
                       options=%s,
                       gitlab_pipeline_id=NULL,
                       gitlab_pipeline_url=NULL,
                       gitlab_job_id=NULL,
                       gitlab_job_url=NULL,
                       git_commit_sha=NULL,
                       pipeline_check_report=NULL,
                       updated_at=NOW()
                 WHERE id=%s
            """, (TaskStatus.pending.value, token, json.dumps(options or {}), task_id))
            conn.commit()
            return task_id, token

        # 沒有舊任務 → 新增
        cur.execute("""
            INSERT INTO forti_tasks (draft_id, status, callback_token, options, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
        """, (draft_id, TaskStatus.pending.value, token, json.dumps(options or {})))
        conn.commit()
        return cur.lastrowid, token
    finally:
        cur.close(); conn.close()

def get_task_by_id(task_id: int) -> Optional[dict]:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("SELECT * FROM forti_tasks WHERE id=%s", (task_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()

def get_task_by_draft_id(draft_id: int) -> Optional[dict]:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("SELECT * FROM forti_tasks WHERE draft_id=%s ORDER BY id DESC LIMIT 1", (draft_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()

def set_task_status(task_id: int, status: str) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_tasks SET status=%s, updated_at=NOW() WHERE id=%s", (status, task_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def update_task_gitlab_info(task_id: int,
                            pipeline_id: int|None,
                            pipeline_url: str|None,
                            job_id: int|None,
                            job_url: str|None,
                            commit_sha: str|None) -> int:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE forti_tasks
               SET gitlab_pipeline_id = %s,
                   gitlab_pipeline_url = %s,
                   gitlab_job_id       = %s,
                   gitlab_job_url      = %s,
                   git_commit_sha      = %s,
                   updated_at          = NOW()
             WHERE id = %s
        """, (pipeline_id, pipeline_url, job_id, job_url, commit_sha, task_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def upsert_action_results(task_id: int, results: List[dict]) -> int:
    if not results:
        return 0
    conn = get_db_connection(); cur = _dc(conn)
    try:
        affected = 0
        for r in results:
            cur.execute("""
                INSERT INTO forti_task_action_results
                  (task_id, action_id, kind, action_type, device_id, vdom, resource_id, status,
                   action_order, deploy_message, rollback, started_at, finished_at)
                VALUES
                  (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  resource_id=VALUES(resource_id),
                  status=VALUES(status),
                  action_order=VALUES(action_order),
                  deploy_message=VALUES(deploy_message),
                  rollback=VALUES(rollback),
                  started_at=VALUES(started_at),
                  finished_at=VALUES(finished_at),
                  updated_at=NOW()
            """, (
                task_id,
                str(r.get("action_id")),
                str(r.get("kind")),
                str(r.get("action_type")),
                int(r.get("device_id")),
                str(r.get("vdom")),
                (None if r.get("resource_id") in (None, "") else str(r.get("resource_id"))),
                str(r.get("status")),
                int(r.get("action_order", 0)),
                json.dumps(r.get("deploy_message")),
                json.dumps(r.get("rollback")),
                r.get("started_at"),
                r.get("finished_at"),
            ))
            affected += cur.rowcount
        conn.commit()
        return affected
    finally:
        cur.close(); conn.close()

def list_action_results_by_task(task_id: int) -> List[dict]:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("""
            SELECT * FROM forti_task_action_results
            WHERE task_id=%s ORDER BY action_order ASC, action_id ASC
        """, (task_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()

def preseed_action_results(task_id: int, draft_action: dict) -> int:
    """
    審批通過後，先把 action_plan 逐筆寫進 forti_task_action_results。
    只填：task_id, action_id, kind, action_type, device_id, vdom,
          resource_id(非 create 才填), action_order, deploy_message
    其他欄位（status/rollback/started_at/finished_at...）交由 pipeline apply 回調補齊。
    """
    plan = (draft_action or {}).get("action_plan") or []
    if not plan:
        return 0

    sql = """
    INSERT INTO forti_task_action_results
      (task_id, action_id, kind, action_type, device_id, vdom, resource_id, action_order, deploy_message)
    VALUES
      (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      kind=VALUES(kind),
      action_type=VALUES(action_type),
      device_id=VALUES(device_id),
      vdom=VALUES(vdom),
      -- 僅在現值為 NULL 時才回填 resource_id，避免覆蓋 pipeline 回寫
      resource_id=COALESCE(forti_task_action_results.resource_id, VALUES(resource_id)),
      action_order=VALUES(action_order),
      deploy_message=VALUES(deploy_message),
      updated_at=NOW()
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        rows = []
        for a in sorted(plan, key=lambda x: x.get("action_order", 0)):
            res_id = None if str(a.get("action_type","")).lower() == "create" else a.get("resource_id")
            rows.append((
                task_id,
                str(a.get("action_id")),
                str(a.get("kind")),
                str(a.get("action_type")),
                int(a.get("device_id")),
                str(a.get("vdom")),
                (None if res_id in (None, "") else str(res_id)),
                int(a.get("action_order", 0)),
                json.dumps(a.get("deploy_message") or {}, ensure_ascii=False),
            ))
        cur.executemany(sql, rows)
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()


def preseed_results_after_approval(task_id: int) -> int:
    """
    由 task_id 追到 draft_action，呼叫 preseed_action_results。
    要求 forti_tasks(draft_id) 與 forti_drafts(draft_action) 存在。
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("SELECT draft_id FROM forti_tasks WHERE id=%s", (task_id,))
        t = cur.fetchone()
        if not t:
            return 0

        cur.execute("SELECT draft_action FROM forti_drafts WHERE id=%s", (t["draft_id"],))
        d = cur.fetchone() or {}
        da = d.get("draft_action")

        try:
            if isinstance(da, str):
                da = json.loads(da) if da else {}
        except Exception:
            da = {}

        return preseed_action_results(task_id, da or {})
    finally:
        cur.close(); conn.close()
