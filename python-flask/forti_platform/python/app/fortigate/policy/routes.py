# app/fortigate/policy/routes.py
from flask import request, jsonify, render_template, session
from . import forti_policy_bp
from app.decorators.decorators import login_required, permission_required
from ..drafts.db.drafts_handler import list_drafts_for_request_page
from .db.policy_sync import (
    sync_policies_from_fortigate,
    query_policies_current,
    get_forti_objects,
    get_forti_services_meta,
    get_forti_addresses_meta,
    get_forti_interfaces_meta,
)

@forti_policy_bp.route("/policy/api/requests", methods=["GET"], endpoint="api_policy_request_list")
@login_required
def api_policy_request_list():
    """
    提供 request_list.html 前端使用的清單接口
    支援參數：status, q, page, page_size
    """
    status = request.args.get("status")
    q = request.args.get("q")
    page = int(request.args.get("page", "1"))
    page_size = int(request.args.get("page_size", "20"))
    offset = (page - 1) * page_size

    rows, total = list_drafts_for_request_page(status=status, q=q, limit=page_size, offset=offset)
    return jsonify({"ok": True, "data": rows, "total": total, "page": page, "page_size": page_size})

# 1) 列出已同步（forti_policies_current）的現況清單，支援篩選
@forti_policy_bp.get("/policy/api/list")
@login_required
def api_policy_list():
    q = request.args
    device_id = q.get("device_id", type=int)
    vdom = q.get("vdom", type=str)
    if not device_id or not vdom:
        return jsonify({"ok": False, "error": "device_id and vdom are required"}), 400

    items = query_policies_current(
        device_id=device_id,
        vdom=vdom,
        action=(q.get("action") or "").strip(),
        status=(q.get("status") or "").strip(),
        seq_min=q.get("seq_min", type=int),
        seq_max=q.get("seq_max", type=int),
        name=(q.get("name") or "").strip(),
    )
    return jsonify({"ok": True, "items": items})

# 2) 取得 Forti 物件下拉（verbose=1 時回傳 typed meta）
@forti_policy_bp.get("/policy/api/objects")
@login_required
def api_policy_objects():
    device_id = request.args.get("device_id", type=int)
    vdom = request.args.get("vdom", type=str)
    verbose = request.args.get("verbose", default="0")
    if not device_id or not vdom:
        return jsonify({"ok": False, "error": "device_id and vdom are required"}), 400

    data = get_forti_objects(device_id, vdom)
    if str(verbose) in {"1", "true", "yes"}:
        data["services_meta"] = get_forti_services_meta(device_id, vdom).get("typed", [])
        data["addresses_meta"] = get_forti_addresses_meta(device_id, vdom).get("typed", [])
        data["interfaces_meta"] = get_forti_interfaces_meta(device_id, vdom).get("typed", [])
    return jsonify({"ok": True, **data})

# 3) 觸發「從 FortiGate 同步」：把現況塞到 forti_policies_current
@forti_policy_bp.post("/policy/api/sync")
@login_required
def api_sync_from_device():
    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id"); vdom = data.get("vdom")
    if not device_id or not vdom:
        return jsonify({"ok": False, "error": "device_id and vdom are required"}), 400

    count = sync_policies_from_fortigate(int(device_id), str(vdom))

    # 若你專案有 log_operation 可呼叫（沒有就註解掉）
    try:
        from app.utils.log import log_operation
        log_operation(session.get("username"), "policy_sync",
                      {"device_id": device_id, "vdom": vdom, "count": count})
    except Exception:
        pass

    return jsonify({"ok": True, "count": count})

