# app/fortigate/pipeline/db/pipeline_handler.py
import json
from typing import List, Optional
from app.db.mysql import get_db_connection
from ...workflow import DraftStatus, TaskStatus
from ...drafts.db.drafts_handler import mark_executed, mark_completed

def _dc(conn):
    return conn.cursor(dictionary=True)

def _update_task_check_report(task_id: int, report: dict) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute(
            "UPDATE forti_tasks SET pipeline_check_report=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(report or {}), task_id)
        )
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def _update_draft_check_report_if_exists(draft_id: int, report: dict) -> None:
    """
    若 forti_drafts 有 check_report 欄位，就寫入摘要（不保證一定存在）。
    """
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM forti_drafts LIKE 'check_report'")
        if cur.fetchone():
            # 存一份簡化的摘要，前端列表用
            summary = dict(report or {})
            cur2 = conn.cursor()
            try:
                cur2.execute(
                    "UPDATE forti_drafts SET check_report=%s, updated_at=NOW() WHERE id=%s",
                    (json.dumps(summary), draft_id)
                )
                conn.commit()
            finally:
                cur2.close()
    finally:
        cur.close(); conn.close()

def _set_draft_status(draft_id: int, status: str) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET status=%s, updated_at=NOW() WHERE id=%s", (status, draft_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def _set_task_status(task_id: int, status: str) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_tasks SET status=%s, updated_at=NOW() WHERE id=%s", (status, task_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def _insert_action_results(task_id: int, results: List[dict]) -> int:
    if not results:
        return 0
    conn = get_db_connection(); cur = conn.cursor()
    try:
        affected = 0
        for r in results:
            cur.execute("""
                INSERT INTO forti_task_action_results (task_id, action_id, kind, action_type, device_id, vdom, resource_id, status,  action_order, deploy_message, rollback, started_at, finished_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
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
                int(r.get("device_id") or 0),
                str(r.get("vdom")) if r.get("vdom") is not None else None,
                (None if r.get("resource_id") in (None, "") else str(r.get("resource_id"))),
                str(r.get("status")),
                int(r.get("action_order") or 0),
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


def append_action_results(task_id: int, results: list) -> int:
    """Insert results without touching task/draft status (for chunked callbacks)."""
    return _insert_action_results(task_id, results or [])

# -------------------- public handlers --------------------

def handle_validate_callback(*, task_id: int, draft_id: int, result: str, report: dict) -> None:
    """
    驗證回調：
      - 永遠寫 forti_tasks.pipeline_check_report
      - 不覆蓋 forti_drafts.check_report（交由 draft create/update/submit 時的計算結果）
      - 狀態流轉：
          passed  → draft: Awaiting_Approval（task 維持 pending/queued 等待審批）
          failed  → draft: Verify_Failed，task: failed
    """
    # 1) pipeline 驗證報告 → forti_tasks
    _update_task_check_report(task_id, report)

    # 2) 依驗證結果調整狀態
    if (result or "").lower() == "passed":
        _set_draft_status(draft_id, DraftStatus.Awaiting_Approval.value)
        # task 狀態保持（等審批後再進 running/apply）
    else:
        _set_draft_status(draft_id, DraftStatus.Verify_Failed.value)
        _set_task_status(task_id, TaskStatus.failed.value)

def handle_apply_start_callback(*, task_id: int, draft_id: int) -> None:
    # 進入 running
    _set_task_status(task_id, TaskStatus.running.value)
    # 記錄開始執行時間
    try: mark_executed(draft_id)
    except Exception: pass

def handle_apply_results_callback(*, task_id: int, draft_id: int, result: str, summary: Optional[dict], results: List[dict]) -> None:
    _insert_action_results(task_id, results or [])
    # 記錄完成時間
    mark_completed(draft_id)
    if (result or "").lower() == "success":
        _set_task_status(task_id, TaskStatus.success.value)
        _set_draft_status(draft_id, DraftStatus.Deploy_Succeeded.value)
    else:
        _set_task_status(task_id, TaskStatus.failed.value)
        _set_draft_status(draft_id, DraftStatus.Partial_Failed.value)
    try: mark_completed(draft_id)
    except Exception: pass

def handle_canceled_callback(*, task_id: int, draft_id: int, reason: Optional[str]) -> None:
    _set_task_status(task_id, TaskStatus.canceled.value)
    _set_draft_status(draft_id, DraftStatus.Canceled.value)
    # 取消視為流程終止，也寫 completed_at
    mark_completed(draft_id)

# ---- Callback auth helper (centralized here) ----
import os
from typing import Tuple, Optional, Dict, Any

# 允許用環境變數切換是否強制驗證（預設開啟）
_CALLBACK_REQUIRED_DEFAULT = str(os.getenv("FG_CALLBACK_REQUIRED", "1")).lower() not in ("0", "false", "no")

def _get_task_row_by_draft_id(draft_id: int) -> Optional[dict]:
    """
    取該 draft 對應的 forti_tasks（若一對多，取最新一筆）
    """
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, draft_id, callback_token
            FROM forti_tasks
            WHERE draft_id=%s
            ORDER BY id DESC
            LIMIT 1
        """, (draft_id,))
        row = cur.fetchone()
        return row
    finally:
        cur.close(); conn.close()

def verify_callback_auth(
    *,
    task_id: int,
    draft_id: int,
    token: Optional[str],
    payload_task_id: Optional[int] = None,
    payload_draft_id: Optional[int] = None,
    required: Optional[bool] = None,
) -> Tuple[Optional[dict], Optional[Tuple[str, int]]]:
    """
    統一做回調驗證：
      1) （可選）比對 payload 與 path 的 task_id/draft_id
      2) 查 DB forti_tasks 與 draft 對應是否正確
      3) 比對 forti_tasks.callback_token == token

    回傳：
      (task_row, None) 表示 OK；
      (None, ("錯誤訊息", HTTP 狀態碼)) 表示失敗
    """
    if required is None:
        required = _CALLBACK_REQUIRED_DEFAULT

    # 缺 token（若強制）
    if required and not token:
        return None, ("missing callback_token", 401)

    # path / payload 一致性（若提供 payload 值才檢查）
    try:
        if payload_task_id is not None and int(payload_task_id) != int(task_id):
            return None, ("path/payload task_id mismatch", 400)
        if payload_draft_id is not None and int(payload_draft_id) != int(draft_id):
            return None, ("path/payload draft_id mismatch", 400)
    except Exception:
        return None, ("invalid path/payload id(s)", 400)

    # DB 對應檢查
    t = _get_task_row_by_draft_id(int(draft_id))
    if not t or int(t["id"]) != int(task_id):
        return None, ("task/draft mismatch", 404)

    # token 比對（若強制）
    if required and str(t.get("callback_token") or "") != str(token or ""):
        return None, ("invalid callback_token", 403)

    return t, None

