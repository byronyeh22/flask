# app/fortigate/task_list/routes.py
from flask import request, jsonify
from . import forti_task_list_bp
from app.decorators.decorators import login_required
from .db.task_list_handler import list_tasks_for_page, get_task_actions

@forti_task_list_bp.route("/task_list", methods=["GET"])
@login_required
def api_list_tasks():
    status    = request.args.get("status") or None
    q         = request.args.get("q") or None
    date_from = request.args.get("date_from") or None
    date_to   = request.args.get("date_to") or None
    draft_id  = request.args.get("draft_id", type=int)
    limit     = request.args.get("limit", default=20, type=int)
    offset    = request.args.get("offset", default=0, type=int)

    # 新增：排序參數
    sort_by   = request.args.get("sort_by") or None  # draft_id | task_id | task_status | first_started_at | last_finished_at
    sort_dir  = request.args.get("sort_dir") or "desc"  # asc | desc

    rows, total = list_tasks_for_page(
        status=status,
        q=q,
        date_from=date_from,
        date_to=date_to,
        draft_id=draft_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir
    )

    return jsonify({
        "ok": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows
    })

@forti_task_list_bp.route("/task_list/<int:task_id>/actions", methods=["GET"])
@login_required
def api_task_actions(task_id: int):
    items = get_task_actions(task_id)
    return jsonify({"ok": True, "items": items})

