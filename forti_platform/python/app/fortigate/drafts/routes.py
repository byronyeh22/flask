# app/fortigate/drafts/routes.py

import json
from flask import request, jsonify, session, flash, get_flashed_messages
from . import forti_drafts_bp
from ..workflow import (
    DraftStatus, TaskStatus,
    can_transition_draft, normalize_draft_status, is_submit_allowed_status,
)
from .db.drafts_handler import (
    create_draft, get_draft, update_draft_status,
    record_approval, delete_draft, update_draft_content,
    mark_submitted,
    mark_approved,
    get_plan_results_for_draft,
    refresh_check_report_by_id,
    _policy_exists, _validate_action_types,
)
from ..tasks.db.tasks_handler import (
    create_task_for_draft, get_task_by_draft_id, set_task_status, update_task_gitlab_info
)
from ..pipeline.gitlab_client import kickoff_pipeline, cancel_pipeline, play_apply_job_for_pipeline
from app.decorators.decorators import login_required


# ---- 共用：把 flash 打包進 JSON（維持原本欄位 & HTTP 狀態碼）
def json_with_flashes(payload: dict, ok=True, status=200):
    msgs = get_flashed_messages(with_categories=True)
    out = {"ok": ok, **(payload or {})}
    if msgs:
        out["flash"] = [{"category": c, "message": m} for c, m in msgs]
    return jsonify(out), status


def _require_user_id():
    uid = session.get("user_id")
    if not uid:
        flash("Unauthorized. Please sign in again.", "danger")
        return None, json_with_flashes({"error": "unauthorized"}, ok=False, status=401)
    return uid, None


# 建立/更新 Draft（可接受空 body；有 id 即視為更新）
@forti_drafts_bp.route("/drafts", methods=["POST"])
@login_required
def api_create_draft():
    uid, err = _require_user_id()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    draft_id_in = data.get("id") or data.get("draft_id")
    title = data.get("title")
    draft_action = data.get("draft_action")
    reset_flag = bool(data.get("reset_check_report", False))

    # ---- Update path ----
    if draft_id_in:
        try:
            draft_id_int = int(draft_id_in)
        except Exception:
            flash("Invalid draft id.", "danger")
            return json_with_flashes({"error": "invalid draft id"}, ok=False, status=400)

        if draft_action is not None:
            ok, violations = _validate_action_types(draft_action)
            if not ok:
                for v in violations:
                    if v.get("message"):
                        flash(v["message"], "danger")
                return json_with_flashes(
                        {"error": "invalid action_type for non-existing policy", "violations": violations},
                        ok=False, status=400
                )
        affected = update_draft_content(
            draft_id_int,
            title=title,
            draft_action=draft_action,
            reset_check_report=reset_flag
        )
        if affected <= 0:
            flash("Update not allowed or no change.", "warning")
            return json_with_flashes({"error": "update not allowed or no change"}, ok=False, status=400)

        report = None
        try:
            # 只有在有帶 draft_action 或要求 reset 時才重算
            if draft_action is not None or reset_flag:
                report = refresh_check_report_by_id(draft_id_int)
        except Exception:
            flash("Draft saved, but re-computing check report failed.", "warning")

        flash(f"Request #{draft_id_int} updated.", "success")
        return json_with_flashes({"id": draft_id_int, "updated": True, "check_report": report})

    # ---- Create path ----
    draft_action_obj = draft_action or {}
    ok, violations = _validate_action_types(draft_action_obj)
    if not ok:
        for v in violations:
            if v.get("message"):
                flash(v["message"], "danger")
        return json_with_flashes(
                {"error": "invalid action_type for non-existing policy", "violations": violations},
                ok=False, status=400
        )
    try:
        draft_id = create_draft(created_by=uid, draft_action=draft_action_obj, title=title)
    except Exception as e:
        flash("Create failed.", "danger")
        return json_with_flashes({"error": str(e)}, ok=False, status=500)

    report = None
    try:
        report = refresh_check_report_by_id(draft_id)
    except Exception:
        # 若第一次計算失敗，再嘗試一次（維持你原本的容錯邏輯）
        try:
            report = refresh_check_report_by_id(draft_id)
        except Exception:
            flash("Request created, but computing check report failed.", "warning")

    flash(f"Request created: #{draft_id}.", "success")
    return json_with_flashes({"id": draft_id, "created": True, "check_report": report})

@forti_drafts_bp.route("/drafts/<int:draft_id>/submit", methods=["POST"])
@login_required
def api_submit_draft(draft_id: int):
    uid, err = _require_user_id()
    if err:
        return err

    d = get_draft(draft_id)
    if not d:
        flash("Request not found.", "danger")
        return json_with_flashes({"error": "draft not found"}, ok=False, status=404)

    raw_status = d.get("status")
    if not (is_submit_allowed_status(raw_status) and
            can_transition_draft(raw_status, DraftStatus.Preparing_Deploy.value)):
        flash("Invalid status for submit.", "warning")
        return json_with_flashes({"error": "invalid status for submit", "status": raw_status}, ok=False, status=400)

    # === 先刷新 check_report（由後端統一計算與儲存）===
    try:
        report = refresh_check_report_by_id(draft_id)
    except Exception:
        report = None
        flash("Submit ok, but recomputing check report failed.", "warning")

    # 檢查是否已有活躍任務
    t_existing = get_task_by_draft_id(draft_id)
    if t_existing and t_existing.get("status") in ("pending", "queued", "running"):
        flash("This request already has an active task.", "info")
        return json_with_flashes({
            "error": "request already has an active task",
            "task_id": t_existing["id"],
            "task_status": t_existing["status"]
        }, ok=False, status=409)

    # 切換草稿狀態 → Preparing_Deploy
    update_draft_status(draft_id, DraftStatus.Preparing_Deploy.value)
    
    # 新增：記錄提交時間
    mark_submitted(draft_id)

    payload = request.get_json(silent=True) or {}
    options = payload.get("options") or {}

    # 建立 forti_tasks 與 callback_token
    task_id, token = create_task_for_draft(draft_id=draft_id, created_by=uid, options=options)

    # 觸發 GitLab pipeline（validate）
    base = request.url_root.rstrip("/")
    draft_action_json = d["draft_action"] if isinstance(d["draft_action"], str) else json.dumps(d["draft_action"] or {})

    try:
        git_meta = kickoff_pipeline(
            task_id=task_id,
            draft_id=draft_id,
            callback_token=token,
            draft_action_json=draft_action_json,
            callback_url_validate=f"{base}/fortigate/callbacks/validate/{task_id}/{draft_id}",
            callback_url_apply_start=f"{base}/fortigate/callbacks/apply/start/{task_id}/{draft_id}",
            callback_url_apply=f"{base}/fortigate/callbacks/apply/{task_id}/{draft_id}",
            callback_url_canceled=f"{base}/fortigate/callbacks/canceled/{task_id}/{draft_id}",
        ) or {}
    except Exception as e:
        # 回滾狀態與任務狀態，並回傳 JSON（讓前端 Toast）
        update_draft_status(draft_id, DraftStatus.Pending_Submit.value)
        set_task_status(task_id, TaskStatus.failed.value)
        flash(str(e), "danger")
        return json_with_flashes({"error": str(e)}, ok=False, status=400)

    update_task_gitlab_info(
        task_id,
        git_meta.get("gitlab_pipeline_id"),
        git_meta.get("gitlab_pipeline_url"),
        git_meta.get("gitlab_job_id"),
        git_meta.get("gitlab_job_url"),
        git_meta.get("git_commit_sha"),
    )
    try: mark_submitted(draft_id)
    except Exception: pass

    flash(f"Submitted request #{draft_id}. Task #{task_id} started (validate).", "success")
    return json_with_flashes({"task_id": task_id, "check_report": report})

# Approve（核准後→排入隊列→立刻 play GitLab 的 apply 手動 job）
@forti_drafts_bp.route("/drafts/<int:draft_id>/approve", methods=["POST"])
@login_required
def api_approve_draft(draft_id: int):
    uid, err = _require_user_id()
    if err:
        return err

    d = get_draft(draft_id)
    if not d:
        flash("Request not found.", "danger")
        return json_with_flashes({"error": "draft not found"}, ok=False, status=404)
    if normalize_draft_status(d["status"]) != DraftStatus.Awaiting_Approval:
        flash("Only Awaiting_Approval can be approved.", "warning")
        return json_with_flashes({"error": "only awaiting_approval can be approved"}, ok=False, status=400)

    # 記錄審批
    record_approval(draft_id=draft_id, approver_id=uid, decision='approved', comment=None)
    # 新增：回寫核准時間
    mark_approved(draft_id)
    update_draft_status(draft_id, DraftStatus.Deploying.value)
    # 找任務
    t = get_task_by_draft_id(draft_id)
    if not t:
        flash("Task not found.", "danger")
        return json_with_flashes({"error": "task not found"}, ok=False, status=404)

    # 任務進入 queued
    set_task_status(t["id"], TaskStatus.queued.value)
    try: mark_approved(draft_id)
    except Exception: pass

    # === 預先寫入 forti_task_action_results（只填指定位）===
    try:
        from ..tasks.db.tasks_handler import preseed_results_after_approval
        preseeded = preseed_results_after_approval(t["id"])
    except Exception:
        preseeded = 0
        flash("Approved, but pre-seeding action results failed.", "warning")

    # 立刻觸發 apply job（若 pipeline 需要人工 play）
    started = False
    try:
        meta = play_apply_job_for_pipeline(task_id=t["id"], draft_id=draft_id)
        started = bool(meta)
    except Exception:
        started = False

    if started:
        set_task_status(t["id"], TaskStatus.running.value)
        flash(f"Request #{draft_id} approved. Apply job started.", "success")
    else:
        flash(f"Request #{draft_id} approved. Task queued and will start soon.", "success")

    return json_with_flashes({"apply_started": bool(started), "preseeded": preseeded})

# Reject（與前端彈窗對齊：wrong/comment → Rejected；cancel → 用 /cancel）
@forti_drafts_bp.route("/drafts/<int:draft_id>/reject", methods=["POST"])
@login_required
def api_reject_draft(draft_id: int):
    uid, err = _require_user_id()
    if err:
        return err
    d = get_draft(draft_id)
    if not d:
        flash("Request not found.", "danger")
        return json_with_flashes({"error": "draft not found"}, ok=False, status=404)
    if normalize_draft_status(d["status"]) != DraftStatus.Awaiting_Approval:
        flash("Only Awaiting_Approval can be rejected.", "warning")
        return json_with_flashes({"error": "only awaiting_approval can be rejected"}, ok=False, status=400)

    payload = request.get_json(silent=True) or {}
    comment = payload.get("comment") or "Rejected"

    record_approval(draft_id=draft_id, approver_id=uid, decision='rejected', comment=comment)

    t = get_task_by_draft_id(draft_id)
    if t:
        cancel_pipeline(task_id=t["id"], draft_id=draft_id)
        set_task_status(t["id"], TaskStatus.canceled.value)

    update_draft_status(draft_id, DraftStatus.Rejected.value)
    flash(f"Request #{draft_id} rejected. ({comment})", "success")
    return json_with_flashes({})


# Cancel（前端選「Request Cancel」時呼叫）
@forti_drafts_bp.route("/drafts/<int:draft_id>/cancel", methods=["POST"])
@login_required
def api_cancel_draft(draft_id: int):
    uid, err = _require_user_id()
    if err:
        return err
    d = get_draft(draft_id)
    if not d:
        flash("Request not found.", "danger")
        return json_with_flashes({"error": "draft not found"}, ok=False, status=404)

    st = normalize_draft_status(d["status"])
    if st not in (DraftStatus.Awaiting_Approval, DraftStatus.Preparing_Deploy, DraftStatus.Deploying):
        flash("Current status cannot cancel.", "warning")
        return json_with_flashes({"error": "current status cannot cancel"}, ok=False, status=400)

    record_approval(draft_id=draft_id, approver_id=uid, decision='canceled', comment='Request Cancel')

    t = get_task_by_draft_id(draft_id)
    if t:
        cancel_pipeline(task_id=t["id"], draft_id=draft_id)
        set_task_status(t["id"], TaskStatus.canceled.value)

    update_draft_status(draft_id, DraftStatus.Canceled.value)
    flash(f"Request #{draft_id} canceled.", "success")
    return json_with_flashes({})


# Delete（僅 Pending_Submit）
@forti_drafts_bp.route("/drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def api_delete_draft(draft_id: int):
    d = get_draft(draft_id)
    if not d:
        flash("Request not found.", "danger")
        return json_with_flashes({"error": "draft not found"}, ok=False, status=404)

    st = normalize_draft_status(d["status"])
    if st not in (DraftStatus.Pending_Submit, DraftStatus.Rejected, DraftStatus.Verify_Failed):
        flash("Only pending/verify_failed/rejected can be deleted.", "warning")
        return json_with_flashes({"error": "only pending/verify_failed/rejected can be deleted"}, ok=False, status=400)

    t = get_task_by_draft_id(draft_id)
    if t and str(t.get("status")) in ("pending", "queued", "running"):
        flash("Draft has an active task and cannot be deleted.", "warning")
        return json_with_flashes({"error": "request has an active task and cannot be deleted"}, ok=False, status=409)

    affected = delete_draft(draft_id)
    if affected <= 0:
        flash("Delete failed.", "danger")
        return json_with_flashes({"error": "delete failed"}, ok=False, status=500)

    flash(f"Request #{draft_id} deleted.", "success")
    return json_with_flashes({})


# 更新草稿（僅 Pending_Submit/Rejected 可編輯）——可接受空 body
@forti_drafts_bp.route("/drafts/<int:draft_id>/update", methods=["POST"])
@login_required
def api_draft_update_v1(draft_id: int):
    payload = request.get_json(silent=True) or {}
    title = payload.get("title")
    draft_action = payload.get("draft_action")
    reset = bool(payload.get("reset_check_report"))

    affected = update_draft_content(draft_id, title=title, draft_action=draft_action, reset_check_report=reset)
    if affected <= 0:
        flash("Update not allowed or no change.", "warning")
        return json_with_flashes({"error": "update not allowed or no change"}, ok=False, status=400)

    # 若有內容異動或要求重算，計算一次報告（失敗不阻擋）
    report = None
    try: 
        if draft_action is not None or reset:
            report = refresh_check_report_by_id(draft_id)
    except Exception:
        flash("Request saved, but re-computing check report failed.", "warning")

    flash(f"Request #{draft_id} saved.", "success")
    return json_with_flashes({"id": draft_id, "check_report": report})


@forti_drafts_bp.route("/drafts/<int:draft_id>", methods=["PUT", "POST"])
@login_required
def api_draft_update_fallback(draft_id: int):
    payload = request.get_json(silent=True) or {}
    title = payload.get("title")
    draft_action = payload.get("draft_action")
    reset = bool(payload.get("reset_check_report"))

    affected = update_draft_content(draft_id, title=title, draft_action=draft_action, reset_check_report=reset)
    if affected <= 0:
        flash("Update not allowed or no change.", "warning")
        return json_with_flashes({"error": "update not allowed or no change"}, ok=False, status=400)
    
    report = None
    try:
        if draft_action is not None or reset:
            report = refresh_check_report_by_id(draft_id)
    except Exception:
        flash("Request saved, but re-computing check report failed.", "warning")

    flash(f"Request #{draft_id} saved.", "success")
    return json_with_flashes({"id": draft_id, "check_report": report})


@forti_drafts_bp.route("/drafts/<int:draft_id>", methods=["GET"])
@login_required
def api_get_draft(draft_id: int):
    d = get_draft(draft_id)
    if not d:
        return jsonify({"ok": False, "error": "request not found"}), 404
    return jsonify({"ok": True, "data": d})

@forti_drafts_bp.route("/drafts/<int:draft_id>/plan_results", methods=["GET"])
@login_required
def list_plan_results(draft_id: int):
    items = get_plan_results_for_draft(draft_id)
    return jsonify({"ok": True, "items": items})
