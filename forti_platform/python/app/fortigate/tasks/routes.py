# app/fortigate/tasks/routes.py
from flask import jsonify
from . import forti_tasks_bp
from ..workflow import TaskStatus
from .db.tasks_handler import (
    get_task_by_id, get_task_by_draft_id, list_action_results_by_task
)
from ..drafts.db.drafts_handler import get_draft
from app.decorators.decorators import login_required, login_or_callback_required

# 供前端輪詢：取得 task 狀態 + results（需要登入）
@forti_tasks_bp.route("/tasks/<int:task_id>/results", methods=["GET"])
@login_required
def api_task_results(task_id: int):
    t = get_task_by_id(task_id)
    if not t:
        return jsonify({"ok": False, "error": "task not found"}), 404
    results = list_action_results_by_task(task_id)
    return jsonify({"ok": True, "task": {"id": t["id"], "status": t["status"]}, "results": results})

# 供 Runner（或 GitLab 任務腳本）拉取 action_plan（允許 token 模式）
@forti_tasks_bp.route("/tasks/<int:task_id>/plan", methods=["GET"])
@login_or_callback_required
def api_task_plan(task_id: int):
    t = get_task_by_id(task_id)
    if not t:
        return jsonify({"ok": False, "error": "task not found"}), 404
    d = get_draft(t["draft_id"])
    if not d:
        return jsonify({"ok": False, "error": "draft not found"}), 404
    return jsonify({"ok": True, "draft_id": d["id"], "draft_action": d["draft_action"]})
