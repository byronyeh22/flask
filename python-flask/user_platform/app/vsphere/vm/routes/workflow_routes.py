from .. import vm_bp
from flask import render_template, request, redirect, url_for, session, flash
import traceback
import json
import logging

# --- DB 函式 ---
from ..db.workflow_manager import (
    get_all_workflow_runs,
    get_workflow_by_id,
    save_or_update_draft,
    update_request_status,
    # apply_request_to_db,
    delete_draft_by_workflow_id,
    return_request
)
from ..db.vm_provisioning_manager import apply_request_to_db

from ..db.get_vm_configurations import get_vms_by_environment, get_vm_config
from ..db.get_gitlab_pipeline_detail_and_stats import get_pipeline_details_by_workflow_id
from ..db.get_jira_tickets_and_stats import get_jira_ticket_by_workflow_id
from ..db.vsphere_connections_manager import get_active_vsphere_connections
from ..db.insert_gitlab_pipeline_info_to_db import insert_gitlab_pipeline_info_to_db

# --- API 函式 ---
from ..jira_api.create_jira_ticket import _generate_create_summary
from ..gitlab_api.trigger_gitlab_pipeline import trigger_gitlab_pipeline
from ..gitlab_api.run_manual_job import run_manual_job
from ..gitlab_api.cancel_manual_jobs import cancel_manual_jobs

# --- 輔助函數 ---
def _current_username():
    # 1) Flask-Login（若你有用）
    try:
        from flask_login import current_user
        if current_user and getattr(current_user, "is_authenticated", False):
            return (
                getattr(current_user, "username", None)
                or getattr(current_user, "email", None)
                or (str(current_user.get_id()) if hasattr(current_user, "get_id") else None)
            )
    except Exception:
        pass

    # 2) 常見的 session key 順序嘗試
    for k in ("user", "username", "account", "email", "uid"):
        v = session.get(k)
        if v:
            return v

    # 3) 最後退回預設
    return "webform_user"

# --- Draft 相關路由 ---
@vm_bp.route("/workflow/draft/<int:workflow_id>/edit", methods=["GET"])
def workflow_draft_edit(workflow_id: int):
    """編輯草稿"""
    try:
        # 1) Load environments for the dropdown (active connections only)
        active_connections = get_active_vsphere_connections()
        raw_envs = [
            (c.get("environment") or "").strip()
            for c in active_connections
            if c.get("environment")
        ]
        seen = set()
        environments = []
        for e in raw_envs:
            k = e.lower()
            if k not in seen:
                seen.add(k)
                environments.append(e)

        # 2) Load draft row
        row = get_workflow_by_id(workflow_id)

        if not row:
            flash(f"Draft #{workflow_id} not found.", "warning")
            return redirect(url_for('vm.overview_index'))

        # Normalize status
        status = (row.get("status") or "").strip().upper()
        if status not in ("DRAFT", "RETURNED"):
            flash(f"Workflow #{workflow_id} is not editable (status={status}).", "warning")
            return redirect(url_for('vm.overview_index'))

        # 3) Parse payload safely
        payload_raw = row.get("request_payload") or "{}"
        try:
            draft_data = json.loads(payload_raw)
            if not isinstance(draft_data, dict):
                draft_data = {}
                flash("Draft payload format is invalid; some values may not be prefilled.", "warning")
        except Exception:
            logging.exception(f"[workflow_draft_edit] JSON parse error for workflow_id={workflow_id}")
            draft_data = {}
            flash("Failed to parse draft payload; some values may not be prefilled.", "danger")

        # 4) Determine action/resource (default: create/vm)
        resource = (draft_data.get("resource") or "vm").strip().lower()
        action_type = (draft_data.get("action_type") or "create").strip().lower()

        # 5) vSphere lists：保持空清單（前端選 Host 後再載入）
        vsphere_data = {
            "datacenters": [],
            "clusters": [],
            "esxi_hosts": [],
            "templates": [],
            "networks": [],
            "datastores": [],
            "vm_name": [],
        }

        if action_type == "create":
            # Render create page (你原本的 create tab)
            return render_template(
                "vm_index.html",
                datacenters=vsphere_data.get("datacenters", []),
                clusters=vsphere_data.get("clusters", []),
                esxi_hosts=vsphere_data.get("esxi_hosts", []),
                templates=vsphere_data.get("templates", []),
                networks=vsphere_data.get("networks", []),
                datastores=vsphere_data.get("datastores", []),
                vm_name=vsphere_data.get("vm_name", []),
                environment=environments,
                draft_data=draft_data,
                workflow_id=workflow_id,
                active_tab="create",
            )

        elif action_type == "update":
            return render_template(
                "update/index.html",
                environment=environments,
                draft_data=draft_data,
                workflow_id=workflow_id,
            )

        elif action_type == "delete":
            return render_template(
                "delete/index.html",
                environment=environments,
                draft_data=draft_data,
                workflow_id=workflow_id,
            )

        else:
            flash(f"Unknown action_type '{action_type}' for workflow #{workflow_id}.", "warning")
            return redirect(url_for('vm.overview_index'))

    except Exception as e:
        logging.exception(f"[workflow_draft_edit] unexpected error: {e}")
        flash(f"Failed to open draft: {e}", "danger")
        return redirect(url_for('vm.overview_index'))

@vm_bp.route("/workflow/draft/<int:workflow_id>/delete", methods=["POST"])
def workflow_draft_delete(workflow_id: int):
    """刪除 DRAFT"""
    try:
        affected = delete_draft_by_workflow_id(workflow_id)

        if affected == 0:
            return {"success": False, "message": "Draft not found or not deletable."}, 400

        return {"success": True}

    except Exception as e:
        logging.error(f"[workflow_draft_delete] {e}", exc_info=True)
        return {"success": False, "message": str(e)}, 500

@vm_bp.route('/workflow/review/<int:workflow_id>', methods=['GET'])
def workflow_review_page(workflow_id):
    """
    以現有 create/review.html 呈現草稿或待審資料（供 Modal iframe 使用）
    """
    try:
        wf = get_workflow_by_id(workflow_id)

        if not wf:
            flash(f"Workflow ID {workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        if 'workflow_id' not in wf:
            wf['workflow_id'] = workflow_id

        # 根據 workflow_id 查詢對應的 pipeline 資訊
        pipeline_details = get_pipeline_details_by_workflow_id(workflow_id)

        # 如果找到了 pipeline 資訊，就把 finished_at 的值加入到 wf 物件中
        if pipeline_details and pipeline_details.get('finished_at'):
            wf['finished_at'] = pipeline_details['finished_at']
        else:
            wf['finished_at'] = None # 確保 finished_at 欄位存在，即使為空

        if wf.get('status'):
            wf['status'] = str(wf['status']).strip().upper()

        raw = wf.get('request_payload') or "{}"
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        is_update_action = 'new_config' in payload

        if is_update_action:
            return render_template("update/review.html", data=payload, workflow=wf)
        else:
            return render_template("create/review.html", data=payload, workflow=wf)

    except Exception as e:
        logging.error(f"[workflow_review_page] {e}", exc_info=True)
        flash("Failed to load review page.", "danger")
        return "Failed to load review page.", 500

# --- Submit 相關路由 ---
@vm_bp.route("/vsphere/vm/submit", methods=["POST"])
def vsphere_submit_request():
    """
    Submit workflow:
      - 需要 workflow_id
      - 檢查 workflow_runs 的狀態與 payload
      - Idempotent 建立 Jira / Pipeline
    """
    from_modal = (request.args.get("from_modal") == "1") or (request.form.get("from_modal") == "1")
    workflow_id = request.form.get("workflow_id")
    if not workflow_id:
        flash("Workflow ID is missing. Cannot submit request.", "danger")
        redirect_url = url_for('vm.overview_index')
        return f'<script>window.top.location="{redirect_url}"</script>'

    try:
        # === 1. 抓 workflow_runs 草稿 ===
        wf = get_workflow_by_id(workflow_id)
        if not wf:
            flash(f"Workflow #{workflow_id} not found.", "danger")
            redirect_url = url_for('vm.overview_index')
            return f'<script>window.top.location="{redirect_url}"</script>'

        payload = json.loads(wf.get("request_payload") or "{}")
        form_data = payload.get("new_config", payload)
        if not form_data:
            flash("Draft content is empty. Please check the form again.", "danger")
            redirect_url = url_for('vm.overview_index')
            return f'<script>window.top.location="{redirect_url}"</script>'

        # 3) GitLab Pipeline：每次 submit 一律觸發新的
        logging.info(f"Triggering new pipeline for workflow #{workflow_id}...")
        pipeline_data = trigger_gitlab_pipeline(form_data)
        if pipeline_data.get("success"):
            insert_gitlab_pipeline_info_to_db(workflow_id, pipeline_data)
            flash(f"Pipeline {pipeline_data['pipeline_id']} has been triggered.", "info")
        else:
            raise Exception(f"Pipeline trigger failed: {pipeline_data.get('error', 'Unknown error')}")

        # 4) 更新 workflow 狀態 → IN_PROGRESS
        update_request_status(workflow_id, "IN_PROGRESS")

    except Exception as e:
        logging.error(f"Submit failed for workflow {workflow_id}: {e}", exc_info=True)
        flash(f"Failed to submit request: {e}", "danger")

    # === 5. 統一導回 overview ===
    redirect_url = url_for("vm.overview_index")
    return f'<script>window.top.location="{redirect_url}"</script>'

# --- Save/Update Draft 路由 ---
@vm_bp.route("/vsphere/vm/create/review", methods=["POST"])
def vsphere_create_vm_review():
    """
    Handles the review step for VM creation.
    Save to Draft 會走這裡：
      - 若有 workflow_id 且狀態為 DRAFT -> UPDATE
      - 否則 -> INSERT 新 DRAFT
    """
    form_data = request.form.to_dict(flat=False)
    processed_form_data = {}
    for key, value in form_data.items():
        if key.endswith('[]'):
            processed_form_data[key] = value
        else:
            processed_form_data[key] = value[0] if isinstance(value, list) and value else value

    try:
        created_by = _current_username()
        wf_id = processed_form_data.get("workflow_id")
        logging.info(f"[vsphere_create_vm_review] username={created_by}, wf_id(raw)={wf_id!r}")

        final_wf_id, action = save_or_update_draft(
            processed_form_data=processed_form_data,
            created_by=created_by,
            workflow_id=wf_id
        )
        logging.info(f"[vsphere_create_vm_review] save_or_update_draft returned: wf_id={final_wf_id}, action={action}")

        if action == "updated":
            flash(f"Draft #{final_wf_id} updated successfully.", "success")
        else:
            flash(f"New draft #{final_wf_id} created successfully.", "success")

        redirect_url = url_for('vm.overview_index')
        return f'<script>window.top.location="{redirect_url}"</script>'

    except Exception as e:
        logging.error(f"[vsphere_create_vm_review] Failed to save draft: {e}", exc_info=True)
        flash(f"Failed to save draft: {e}", "danger")
        redirect_url = url_for('vm.overview_index')
        return f'<script>window.top.location="{redirect_url}"</script>'

# --- Update Draft 路由 ---
@vm_bp.route("/vsphere/vm/update/review", methods=["POST"])
def vsphere_update_vm_review():
    """
    Handles saving an update request as a draft.
    （Route 不處理 DB；由 helper 自行建/關連線）
    """
    # 1) 取表單並正規化
    new_config = request.form.to_dict(flat=False)
    processed_new_config = {
        k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
        for k, v in new_config.items()
    }

    env = processed_new_config.get('environment')
    prefix = processed_new_config.get('vm_name_prefix')

    try:
        # 2) 讀原始設定（helper 內部處理連線）
        original_config = get_vm_config(env, prefix) or {}

        # 3) 組 payload（頂層帶 action_type=update，給 review 用）
        payload = {
            'original_config': original_config,
            'new_config': processed_new_config,
            'action_type': 'update',
        }

        # 4) 建立草稿（helper 內部處理連線）
        created_by = _current_username()

        # 使用 save_or_update_draft 來處理
        final_wf_id, action = save_or_update_draft(
            processed_form_data=payload,
            created_by=created_by,
            workflow_id=None  # 新建立的 update draft
        )

        flash(f"New update draft #{final_wf_id} for {prefix} has been created.", "success")

    except Exception as e:
        logging.exception(f"[vsphere_update_vm_review] save draft failed: {e}")
        flash(f"Failed to save update draft: {e}", "danger")

    return redirect(url_for('vm.overview_index'))

@vm_bp.route("/vsphere/vm/cancel")
def vsphere_cancel_vm_form():
    """Redirects to the overview page, as session is no longer used for forms."""
    return redirect(url_for('vm.overview_index'))

# --- approve ---
@vm_bp.route('/workflow/approve/<int:workflow_id>', methods=['GET'])
def workflow_approve_page(workflow_id):
    """
    顯示 Approve page。
    """
    try:
        # 1) 讀 workflow（找不到就回 overview）
        workflow = get_workflow_by_id(workflow_id)
        if not workflow:
            flash(f"Workflow ID {workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        # 2) 讀 pipeline / jira
        pipeline = get_pipeline_details_by_workflow_id(workflow_id)
        jira_ticket = get_jira_ticket_by_workflow_id(workflow_id)

        # 3) request_payload 轉成 dict（容錯）
        request_details = {}
        if workflow.get('request_payload'):
            try:
                request_details = json.loads(workflow['request_payload'])
            except Exception:
                request_details = {}

        # 4) Render approve page
        return render_template(
            "create/approve.html",
            workflow=workflow,
            pipeline=pipeline,
            jira_ticket=jira_ticket,
            request_details=request_details
        )

    except Exception as e:
        flash(f"Error loading approval page: {e}", "danger")
        return redirect(url_for('vm.overview_index'))

@vm_bp.route('/workflow/execute/<int:workflow_id>', methods=['POST'])
def workflow_execute(workflow_id):
    """
    Approve & Execute。
    """
    from_modal = (request.args.get("from_modal") == "1") or (request.form.get("from_modal") == "1")

    try:
        apply_request_to_db(workflow_id)

        pipeline = get_pipeline_details_by_workflow_id(workflow_id)
        if not pipeline or not pipeline.get('pipeline_id'):
            raise Exception("Could not find the associated pipeline to execute.")

        # 觸發手動 job（外部服務呼叫，維持原樣）
        approver = _current_username()
        update_request_status(workflow_id, "DEPLOYING", approver=approver)

        result = run_manual_job(pipeline['pipeline_id'])
        if not result.get("success"):
            raise Exception(f"Failed to trigger manual job: {result.get('error')}")

        flash(f"Request #{workflow_id} has been approved by {approver}. The pipeline is now running.", "success")

    except Exception as e:
        logging.error(f"Failed to execute workflow {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to approve request: {e}", "danger")

    # 與 submit 一致的導頁方式：modal 用 JS，否則一般 redirect
    redirect_url = url_for('vm.overview_index')
    if from_modal:
        return f'<script>try{{window.top.location="{redirect_url}";}}catch(e){{window.parent.location="{redirect_url}";}}</script>'
    return redirect(redirect_url)

@vm_bp.route('/workflow/return/<int:workflow_id>', methods=['POST'])
def workflow_return(workflow_id):
    """
    Return (退件) 流程：
    1. 取消關聯的 GitLab Pipeline
    2. 更新 workflow_runs 狀態為 'RETURNED'
    """
    # [修正] 確保 from_modal 變數被定義
    from_modal = (request.args.get("from_modal") == "1") or (request.form.get("from_modal") == "1")

    reason = ""
    try:
        if request.is_json:
            body = request.get_json(silent=True) or {}
            reason = (body.get("returned_reason") or body.get("reason") or "").strip()
        else:
            reason = (request.form.get("returned_reason") or request.form.get("reason") or "").strip()
    except Exception:
        pass
    if not reason:
        reason = "No reason provided."

    try:
        returned_by = _current_username()

        # 步驟 1: 取消 GitLab Pipeline
        pipeline = get_pipeline_details_by_workflow_id(workflow_id)
        if pipeline and pipeline.get("pipeline_id"):
            pipeline_id_to_cancel = pipeline["pipeline_id"]
            logging.info(f"Attempting to cancel pipeline #{pipeline_id_to_cancel} for returned workflow #{workflow_id}.")

            try:
                cancel_result = cancel_manual_jobs(pipeline_id_to_cancel)
                if cancel_result.get("success"):
                    flash(f"Successfully sent cancellation request for pipeline #{pipeline_id_to_cancel}.", "success")
                else:
                    flash(f"API failed to cancel pipeline #{pipeline_id_to_cancel}: {cancel_result.get('error')}", "danger")
            except Exception as e:
                logging.error(f"[WORKFLOW_RETURN] Exception during cancel_manual_jobs call for pipeline #{pipeline_id_to_cancel}: {e}", exc_info=True)
                flash(f"An unexpected error occurred while canceling pipeline #{pipeline_id_to_cancel}: {str(e)}", "danger")

        # 步驟 2: 更新 workflow 狀態為 RETURNED
        return_request(workflow_id, reason, returned_by=returned_by)

        # 步驟 3: 記錄日誌並給予最終反饋
        logging.info(
            f"[WORKFLOW_RETURN] workflow_id={workflow_id}, reason={reason}, returned_by={returned_by}"
        )
        flash(f"Workflow {workflow_id} has been returned.", "warning")

    except Exception as e:
        logging.exception(f"[WORKFLOW_RETURN] failed for workflow_id={workflow_id}")
        flash(f"Failed to return workflow: {e}", "danger")

    redirect_url = url_for('vm.overview_index')
    if from_modal:
        return f'<script>try{{window.top.location="{redirect_url}";}}catch(e){{window.parent.location="{redirect_url}";}}</script>'
    return redirect(redirect_url)