# app/utils/utils.py
from __future__ import annotations
import json
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Optional, Tuple, Union

from flask import request, session, g, current_app
from app.db.mysql import get_db_connection
from zoneinfo import ZoneInfo  # <<< 新增

__all__ = [
    "log_operation",
    "log_forti_audit",
    "audit_event",
]

# 固定資料庫時間時區（UTC+8）
DB_TZ = ZoneInfo("Asia/Taipei")


# =========================
# 共用小工具
# =========================
def _client_ip() -> str:
    """取得真實客戶端 IP（支援 Nginx Proxy）"""
    ip = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For")
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    return ip or request.remote_addr or "0.0.0.0"


def _to_json(details: Any) -> Optional[str]:
    """將 details 轉成 JSON 字串；保留非 ASCII；必要時包裝為 _raw"""
    if details is None:
        return None
    if isinstance(details, (dict, list)):
        return json.dumps(details, ensure_ascii=False, default=str)
    try:
        return json.dumps(details, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"_raw": str(details)}, ensure_ascii=False)


def _get_actor_id() -> Optional[int]:
    """優先從 g.user，其次 session 取得 actor id"""
    if hasattr(g, "user") and getattr(g.user, "id", None):
        return g.user.id
    return session.get("user_id")


def _get_db() -> Tuple[Any, bool]:
    """
    取得 DB 連線。
    回傳 (conn, is_temp)：
      - 若讀到 app.extensions["mysql_conn"]（共用連線/池），回傳 is_temp=False（呼叫端不要 close）
      - 否則以 get_db_connection() 取得臨時連線，回傳 is_temp=True（呼叫端要 close）
    """
    try:
        conn = current_app.extensions.get("mysql_conn")
        if conn:
            return conn, False
    except Exception:
        pass
    return get_db_connection(), True


def _now_db_tz_naive() -> datetime:
    """
    回傳「Asia/Taipei 的現在時間」，且移除 tzinfo（MySQL DATETIME 常用 naive）。
    注意：前端/報表要知道 DB 為 UTC+8。
    """
    return datetime.now(DB_TZ).replace(tzinfo=None)


# =========================
# UI / 系統層操作 → operation_logs
# =========================
def log_operation(user: Optional[str], action: str, detail: Any = None) -> None:
    """
    寫入 operation_logs（UI/系統操作事件）
    - user: 使用者名稱
    - action: 動作（login / click_button / open_page ...）
    - detail: dict/任意，會被 JSON 化並加入 ip/ua/path/method
    """
    conn, is_temp = _get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO operation_logs (username, action, detail, timestamp)
            VALUES (%s, %s, %s, %s)
            """,
            (
                user or "anonymous",
                action,
                _to_json({
                    "detail": detail,
                    "ip": _client_ip(),
                    "ua": request.headers.get("User-Agent"),
                    "path": request.path,
                    "method": request.method,
                }),
                _now_db_tz_naive(),  # <<< 以 UTC+8 寫入
            )
        )
        conn.commit()
    finally:
        try:
            cur.close()
        finally:
            if is_temp:
                conn.close()


# =========================
# 策略 / 設備層事件 → forti_audit_logs
# =========================
def log_forti_audit(
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Any = None
) -> int:
    """
    寫入 forti_audit_logs（策略/設備級事件：草稿、審批、同步、發佈等）
    - action: 'create_draft' / 'approve' / 'publish' / 'sync' ...
    - entity_type: 'policy_draft' / 'device' / 'task' ...
    - entity_id: 關聯資源 ID
    - details: dict/任意，會被 JSON 化並加上 meta(ip/ua/path/method)
    回傳：新建的 audit id
    """
    conn, is_temp = _get_db()
    cur = conn.cursor()
    try:
        payload = {
            "meta": {
                "ip": _client_ip(),
                "ua": request.headers.get("User-Agent"),
                "path": request.path,
                "method": request.method,
            },
            "data": details,
        }
        cur.execute(
            """
            INSERT INTO forti_audit_logs (actor_id, action, entity_type, entity_id, details, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (_get_actor_id(), action, entity_type, entity_id, _to_json(payload), _now_db_tz_naive())  # <<< 以 UTC+8 寫入
        )
        audit_id = cur.lastrowid
        conn.commit()
        return audit_id
    finally:
        try:
            cur.close()
        finally:
            if is_temp:
                conn.close()


# =========================
# 審計 Decorator → 自動寫 forti_audit_logs
# =========================
Extractor = Union[str, int, None, Callable[[Any], Any]]

def _extract(value: Extractor, result: Any, kwargs: dict) -> Any:
    """
    從多種來源抽取 entity_id / details：
      - 直接值（int/str/None）
      - key 名稱（從 kwargs 讀）
      - lambda(res)（以 route 回傳結果為輸入）
    """
    if callable(value):
        return value(result)
    if isinstance(value, str):
        return kwargs.get(value)
    return value  # int / None


def audit_event(
    action: str,
    entity_type: str,
    entity_id: Extractor = None,
    details: Extractor = None,
    actor_id_getter: Optional[Callable[[], Optional[int]]] = None,
):
    """
    在視圖成功執行後自動寫入 forti_audit_logs
      - action: 'create_draft' / 'approve' / 'publish' / 'sync' ...
      - entity_type: 'policy_draft' / 'device' / 'task' ...
      - entity_id: 直接值、'kwargs鍵名'、或 lambda(res) -> id
      - details: 直接值、'kwargs鍵名'、或 lambda(res) -> dict
      - actor_id_getter: 若需自訂 actor 取得方式可傳入（預設使用 g.user.id 或 session['user_id']）
    """
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)

            if actor_id_getter:
                try:
                    _ = actor_id_getter()
                except Exception:
                    current_app.logger.exception("actor_id_getter failed")

            ent_id = _extract(entity_id, result, kwargs)
            det = _extract(details, result, kwargs)

            try:
                log_forti_audit(
                    action=action,
                    entity_type=entity_type,
                    entity_id=ent_id,
                    details=det
                )
            except Exception:
                current_app.logger.exception("audit_event failed")

            return result
        return wrapper
    return decorator

