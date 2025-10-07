# app/utils/tz.py
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import session, current_app

# 資料庫時間一律儲存為 UTC+8（naive），所以這裡把 naive 視為 Asia/Taipei
DB_TZ = ZoneInfo("Asia/Taipei")


def _parse_dt(value):
    """
    嘗試解析多種字串格式：
    - ISO 8601（含 Z 或 +HH:MM）
    - RFC 2822 / email utils
    - Python datetime 物件
    回傳 timezone-aware datetime；若原本 naive，會套上 DB_TZ (UTC+8)
    """
    if isinstance(value, datetime):
        dt = value
    else:
        if not value:
            return None
        v = str(value)
        # ISO: 允許 'Z'
        try:
            v_iso = v.replace("Z", "+00:00")
            dt = datetime.fromisoformat(v_iso)
        except Exception:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(v)
            except Exception:
                return None

    if dt.tzinfo is None:
        # 以前：dt = dt.replace(tzinfo=timezone.utc)
        # 現在：DB 保存的是 UTC+8 的牆上時間，所以補上 Asia/Taipei
        dt = dt.replace(tzinfo=DB_TZ)
    return dt


def register_tz_helpers(app):
    @app.template_filter("fmt_dt")
    def fmt_dt(value, fmt: str = "%Y-%m-%d %H:%M:%S %Z%z"):
        """
        將各種輸入時間格式化為指定時區（預設 session['tz'] 或 Asia/Taipei）
        """
        dt = _parse_dt(value)
        if not dt:
            return value or ""
        tzname = session.get("tz") or "Asia/Taipei"   # 預設顯示 Asia/Taipei
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            current_app.logger.warning("Unknown tz in session: %s; fallback to Asia/Taipei", tzname)
            tz = DB_TZ
        return dt.astimezone(tz).strftime(fmt)

    @app.context_processor
    def inject_current_tz():
        return {"CURRENT_TZ": session.get("tz") or "Asia/Taipei"}

