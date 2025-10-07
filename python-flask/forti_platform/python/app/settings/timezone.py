# app/settings/timezone.py
from __future__ import annotations
from flask import Blueprint, request, session, jsonify
from zoneinfo import ZoneInfo, available_timezones

tz_bp = Blueprint("tz_bp", __name__)

@tz_bp.get("/settings/timezone")
def get_timezone():
    tz = session.get("tz") or "UTC"
    return jsonify({"ok": True, "timezone": tz})

@tz_bp.post("/settings/timezone")
def set_timezone():
    data = request.get_json(silent=True) or {}
    tz = str(data.get("timezone") or data.get("tz") or "").strip()
    if not tz:
        return jsonify({"ok": False, "error": "timezone is required"}), 400
    try:
        ZoneInfo(tz)  # 驗證 IANA 時區
    except Exception:
        return jsonify({"ok": False, "error": "invalid timezone"}), 400
    session["tz"] = tz
    return jsonify({"ok": True, "timezone": tz})

