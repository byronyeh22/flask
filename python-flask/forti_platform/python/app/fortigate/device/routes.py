# app/fortigate/device/routes.py
from __future__ import annotations
from typing import List
from flask import request, jsonify, session

import time
import requests
import urllib3

from . import forti_device_bp
from app.utils.utils import log_operation, audit_event
from .db.device_handler import (
    list_devices, get_device, create_device, update_device, delete_device,
    list_device_vdoms, upsert_device_vdoms
)
from mysql.connector import errors as db_errors

# 如需靜音 verify_ssl=False 的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---- Devices ----
@forti_device_bp.get("/devices")
def api_device_list():
    items = list_devices()
    log_operation(session.get("username"), "device_list", None)
    resp = jsonify({"ok": True, "items": items})
    resp.status_code = 200
    return resp


@forti_device_bp.post("/devices")
@audit_event(
    action="device_create",
    entity_type="device",
    # 確保不是 tuple；且非 JSON 時不會噴錯
    entity_id=lambda res: (res.get_json(silent=True) or {}).get("id"),
    details=lambda res: request.json
)
def api_device_create():
    data = request.get_json(silent=True) or {}
    for k in ("name", "host", "api_token"):
        if k not in data:
            resp = jsonify({"ok": False, "error": f"missing key: {k}"})
            resp.status_code = 400
            return resp
    try:
        port = int(data.get("port", 443))
    except Exception:
        resp = jsonify({"ok": False, "error": "invalid port"})
        resp.status_code = 400
        return resp

    try:
        new_id = create_device(
            name=str(data["name"]).strip(),
            host=str(data["host"]).strip(),
            port=port,
            api_token=str(data["api_token"]),
            is_active=1 if data.get("is_active", 1) else 0,
            verify_ssl=1 if data.get("verify_ssl", 1) else 0,
        )
    except db_errors.DatabaseError as e:
        # 1205: lock wait timeout, 1213: deadlock
        if getattr(e, "errno", None) in (1205, 1213):
            resp = jsonify({"ok": False, "error": f"database busy (errno {e.errno})"})
            resp.status_code = 409
            return resp
        resp = jsonify({"ok": False, "error": f"db error (errno {getattr(e,'errno',None)})"})
        resp.status_code = 500
        return resp

    log_operation(session.get("username"), "device_create", {"id": new_id})
    resp = jsonify({"ok": True, "id": new_id})
    resp.status_code = 201
    return resp


@forti_device_bp.put("/devices/<int:device_id>")
@audit_event(action="device_update", entity_type="device", entity_id="device_id",
             details=lambda res: request.json)
def api_devices_update(device_id: int):
    data = request.get_json(silent=True) or {}
    dev = get_device(device_id)
    if not dev:
        resp = jsonify({"ok": False, "error": "device not found"})
        resp.status_code = 404
        return resp

    name = str(data.get("name", dev["name"])).strip()
    host = str(data.get("host", dev["host"])).strip()
    port = int(data.get("port", dev["port"]))
    is_active = 1 if data.get("is_active", dev["is_active"]) else 0
    verify_ssl = 1 if data.get("verify_ssl", dev["verify_ssl"]) else 0
    api_token = data.get("api_token")  # 留白/缺省則不更新

    update_device(device_id, name, host, port, is_active, verify_ssl, api_token=api_token)
    log_operation(session.get("username"), "device_update", {"id": device_id})
    resp = jsonify({"ok": True, "id": device_id})
    resp.status_code = 200
    return resp


@forti_device_bp.delete("/devices/<int:device_id>")
@audit_event(action="device_delete", entity_type="device", entity_id="device_id")
def api_devices_delete(device_id: int):
    if not get_device(device_id):
        resp = jsonify({"ok": False, "error": "device not found"})
        resp.status_code = 404
        return resp
    delete_device(device_id)
    log_operation(session.get("username"), "device_delete", {"id": device_id})
    resp = jsonify({"ok": True})
    resp.status_code = 200
    return resp


# ---- VDOMs ----
@forti_device_bp.get("/devices/<int:device_id>/vdoms")
def api_device_vdoms(device_id: int):
    if not get_device(device_id):
        resp = jsonify({"ok": False, "error": "device not found"})
        resp.status_code = 404
        return resp
    vdoms = list_device_vdoms(device_id)
    log_operation(session.get("username"), "device_vdoms_list", {"device_id": device_id})
    resp = jsonify({"ok": True, "vdoms": vdoms})
    resp.status_code = 200
    return resp


@forti_device_bp.post("/devices/<int:device_id>/vdoms")
@audit_event(action="device_vdoms_upsert", entity_type="device", entity_id="device_id",
             details=lambda res: request.json)
def api_device_vdoms_upsert(device_id: int):
    data = request.get_json(silent=True) or {}
    vdoms: List[str] = data.get("vdoms") or []
    if not get_device(device_id):
        resp = jsonify({"ok": False, "error": "device not found"})
        resp.status_code = 404
        return resp
    upsert_device_vdoms(device_id, vdoms)
    log_operation(session.get("username"), "device_vdoms_upsert", {"device_id": device_id, "vdoms": vdoms})
    resp = jsonify({"ok": True, "device_id": device_id, "vdoms": vdoms})
    resp.status_code = 200
    return resp


# ---- Probe ----
@forti_device_bp.get("/devices/<int:device_id>/probe")
def api_device_probe(device_id: int):
    """
    嘗試以 API Token 打 FortiGate 常見健康端點，回傳延遲(ms) 與使用的端點。
    前端用 /devices/<id>/probe 來測試連線與權限。
    """
    dev = get_device(device_id)
    if not dev:
        resp = jsonify({"ok": False, "error": "device not found"})
        resp.status_code = 404
        return resp

    host = dev["host"]
    port = int(dev.get("port") or 443)
    base = f"https://{host}:{port}"
    token = str(dev.get("api_token") or "")
    verify_ssl = bool(dev.get("verify_ssl", 1))

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # 依序嘗試兩個端點，任一成功即算 OK
    candidates = [
        "/api/v2/monitor/system/status",   # 回版本/序號等（常用）
        "/api/v2/cmdb/system/interface",   # 權限不足時可能 403，但設備通也算有回應
    ]

    last_error = None
    tried = []
    for path in candidates:
        url = f"{base}{path}"
        tried.append(path)
        t0 = time.perf_counter()
        try:
            r = requests.get(url, headers=headers, timeout=5, verify=verify_ssl)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            if 200 <= r.status_code < 300:
                # 成功
                log_operation(
                    session.get("username"),
                    "device_probe",
                    {"device_id": device_id, "endpoint": path, "ms": latency_ms, "status": r.status_code}
                )
                resp = jsonify({"ok": True, "latency_ms": latency_ms, "endpoint": path, "http_status": r.status_code})
                resp.status_code = 200
                return resp
            else:
                # 非 2xx 視為失敗，繼續嘗試下一個
                last_error = f"http {r.status_code}"
        except Exception as e:
            last_error = str(e)

    # 全部失敗
    log_operation(
        session.get("username"),
        "device_probe_fail",
        {"device_id": device_id, "error": last_error, "tried": tried}
    )
    resp = jsonify({"ok": False, "error": last_error or "probe failed", "tried": tried})
    resp.status_code = 502
    return resp

