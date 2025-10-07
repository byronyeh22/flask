from flask import request, jsonify
from app.decorators.decorators import login_or_callback_required

from . import forti_pipeline_bp
from .schema import parse_callback_payload, PayloadError
from ..tasks.db.tasks_handler import get_task_by_draft_id
from ..drafts.db.drafts_handler import get_draft
from ..policy.db.policy_sync import sync_policies_from_fortigate
from .db.pipeline_handler import (
    handle_validate_callback,
    handle_apply_start_callback,
    handle_apply_results_callback,
    append_action_results,
    handle_canceled_callback,
    verify_callback_auth,
)

# ----------------------------------------------------
# Helpers（統一權杖取得與核對流程）
# ----------------------------------------------------
def _extract_token():
    """僅接受 Header: X-Auth-Token。"""
    return request.headers.get("X-Auth-Token")

# ----------------------------------------------------
# Validate
# ----------------------------------------------------
@forti_pipeline_bp.route("/callbacks/validate/<int:task_id>/<int:draft_id>", methods=["POST"])
def callback_validate(task_id: int, draft_id: int):
    try:
        data = parse_callback_payload("validate", request.get_json(force=True))
    except PayloadError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    token = _extract_token()
    _t, err = verify_callback_auth(
        task_id=task_id,
        draft_id=draft_id,
        token=token,
        payload_task_id=data.get("task_id"),
        payload_draft_id=data.get("draft_id"),
    )
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]

    handle_validate_callback(
        task_id=data["task_id"],
        draft_id=data["draft_id"],
        result=data["result"],
        report=data["report"],
    )
    return jsonify({"ok": True})


# ----------------------------------------------------
# Apply Start
# ----------------------------------------------------
@forti_pipeline_bp.route("/callbacks/apply/start/<int:task_id>/<int:draft_id>", methods=["POST"])
def callback_apply_start(task_id: int, draft_id: int):
    try:
        data = parse_callback_payload("apply_start", request.get_json(force=True))
    except PayloadError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    token = _extract_token()
    _t, err = verify_callback_auth(
        task_id=task_id,
        draft_id=draft_id,
        token=token,
        payload_task_id=data.get("task_id"),
        payload_draft_id=data.get("draft_id"),
    )
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]

    handle_apply_start_callback(task_id=data["task_id"], draft_id=data["draft_id"])
    return jsonify({"ok": True})


# ----------------------------------------------------
# Apply Results（支援 chunk）
# ----------------------------------------------------
@forti_pipeline_bp.route("/callbacks/pipeline/apply/<int:task_id>/<int:draft_id>", methods=["POST"])
def cb_pipeline_apply(task_id: int, draft_id: int):
    try:
        data = parse_callback_payload("apply", request.get_json(force=True))
    except PayloadError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    token = _extract_token()
    _t, err = verify_callback_auth(
        task_id=task_id,
        draft_id=draft_id,
        token=token,
        payload_task_id=data.get("task_id"),
        payload_draft_id=data.get("draft_id"),
    )
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]

    # chunk：partial 只累積結果，不變更狀態
    if str(data.get("result", "")).lower() == "partial" or (
        data.get("chunk_total") and data.get("chunk_index", 0) < data.get("chunk_total")
    ):
        append_action_results(task_id=data["task_id"], results=data.get("results") or [])
        return jsonify({"ok": True, "partial": True})

    # 最終片：寫入全部結果 + 更新狀態
    handle_apply_results_callback(
        task_id=data["task_id"],
        draft_id=data["draft_id"],
        result=data.get("result"),
        summary=data.get("summary"),
        results=data.get("results") or [],
    )

    # 成功後觸發同步（以該草稿的 device/vdom 去同步政策）
    if str(data.get("result", "")).lower() == "success":
        try:
            d = get_draft(draft_id)
            da = d.get("draft_action") if d else {}
            if isinstance(da, str):
                import json as _json2
                da = _json2.loads(da) if da else {}
            pairs = set()
            for a in (da.get("action_plan") or []):
                device_id = a.get("device_id")
                vdom = a.get("vdom")
                if device_id and vdom:
                    pairs.add((int(device_id), str(vdom)))
            for device_id, vdom in pairs:
                try:
                    sync_policies_from_fortigate(device_id, vdom)
                except Exception:
                    pass
        except Exception:
            pass

    return jsonify({"ok": True})


# ----------------------------------------------------
# Canceled
# ----------------------------------------------------
@forti_pipeline_bp.route("/callbacks/canceled/<int:task_id>/<int:draft_id>", methods=["POST"])
def callback_canceled(task_id: int, draft_id: int):
    try:
        data = parse_callback_payload("canceled", request.get_json(force=True))
    except PayloadError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    token = _extract_token()
    _t, err = verify_callback_auth(
        task_id=task_id,
        draft_id=draft_id,
        token=token,
        payload_task_id=data.get("task_id"),
        payload_draft_id=data.get("draft_id"),
    )
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]

    handle_canceled_callback(
        task_id=data["task_id"],
        draft_id=data["draft_id"],
        reason=data.get("reason"),
    )
    return jsonify({"ok": True})


# ----------------------------------------------------
# Kickoff (GitLab 回填 pipeline id / job id / url / sha)
# ----------------------------------------------------
@forti_pipeline_bp.route("/callbacks/pipeline/kickoff/<int:task_id>/<int:draft_id>", methods=["POST"])
def cb_pipeline_kickoff(task_id: int, draft_id: int):
    data = request.get_json(silent=True) or {}

    # kickoff 沒有 payload 的 task/draft，比對 path + token 即可
    token = _extract_token()
    _t, err = verify_callback_auth(
        task_id=task_id,
        draft_id=draft_id,
        token=token,
        # payload_* 省略
    )
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]

    from ..tasks.db.tasks_handler import update_task_gitlab_info
    update_task_gitlab_info(
        task_id,
        data.get("gitlab_pipeline_id"),
        data.get("gitlab_pipeline_url"),
        data.get("gitlab_job_id"),
        data.get("gitlab_job_url"),
        data.get("git_commit_sha"),
    )
    return jsonify({"ok": True})
