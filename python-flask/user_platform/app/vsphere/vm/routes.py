from flask import render_template, request, redirect, url_for, session, flash, jsonify, current_app
import traceback
import json
import logging

from . import vm_bp

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- DB 函式 ---
from .db.get_vm_configurations import get_environment, get_vms_by_environment, get_vm_config
from .db.get_jira_tickets_and_stats import get_jira_tickets_and_stats, get_jira_ticket_by_workflow_id
from .db.get_gitlab_pipeline_detail_and_stats import get_gitlab_pipeline_detail_and_stats, get_pipeline_details_by_workflow_id
from .db.insert_jira_info_to_db import insert_jira_info_to_db
from .db.insert_gitlab_pipeline_info_to_db import insert_gitlab_pipeline_info_to_db
from .db.workflow_manager import get_all_workflow_runs, save_or_update_draft, get_workflow_by_id, update_request_status, apply_request_to_db, delete_draft_by_workflow_id, return_request
from .db.update_jira_ticket_status import update_jira_ticket_status
from .db.vsphere_connections_manager import (
    get_all_vsphere_connections,
    get_active_vsphere_connections,
    add_or_update_vsphere_connection,
    update_connection_password,
    delete_vsphere_connection_by_id,
    toggle_connection_status,
    get_vsphere_connection_by_id,
    get_hosts_by_environment,
    get_vsphere_connection_by_host,
)

# --- API 函式 ---
from .vsphere_api.get_vsphere_objects import get_vsphere_objects
from .vsphere_api.test_connection import test_vsphere_connection
from .jira_api.create_jira_ticket import create_jira_ticket
from .jira_api.get_jira_issue_detail import get_jira_issue_detail
from .jira_api.issue_updates import jira_return_issue
from .jira_api.create_jira_ticket import _generate_create_summary
from .gitlab_api.trigger_gitlab_pipeline import trigger_gitlab_pipeline
from .gitlab_api.run_manual_job import run_manual_job
from .gitlab_api.cancel_manual_jobs import cancel_manual_jobs

# --- 抓取目前登入 User ---
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

# --- overview 與 API (Views & APIs) ---
@vm_bp.route("/vsphere/vm")
def vm_index():
    """
    Render the main VM management page.
    """
    environments = []
    try:
        active_connections = get_active_vsphere_connections()

        raw_envs = [
            (c.get("environment") or "").strip()
            for c in active_connections
            if c.get("environment")
        ]
        seen = set()
        for e in raw_envs:
            key = e.lower()
            if key not in seen:
                seen.add(key)
                environments.append(e)

    except Exception as e:
        logging.error(f"Error in vm_index: {e}")
        environments = []

    return render_template(
        "vm_index.html",
        environment=environments,
        datacenters=[],
        clusters=[],
        esxi_hosts=[],
        templates=[],
        networks=[],
        datastores=[],
        vm_name=[],
    )

@vm_bp.route("/vsphere/overview")
def overview_index():
    def _to_iso(v):
        try:
            return v.isoformat()
        except Exception:
            return str(v) if v else "1970-01-01T00:00:00Z"

    try:
        # 1) 原資料
        jira_tickets = get_jira_tickets_and_stats() or []
        pipeline_data = get_gitlab_pipeline_detail_and_stats() or []
        pipeline_data = [
            {**dict(p), "created_at": _to_iso(p.get("created_at") or p.get("started_at"))}
            for p in pipeline_data
        ]

        # 2) 全部 workflow（包含 DRAFT）
        workflows = get_all_workflow_runs()
        wf_status_map = {w["workflow_id"]: w["status"] for w in workflows}
        wf_created_by_map = {w["workflow_id"]: w.get("created_by") for w in workflows}

        # A) 先把 JIRA tickets 收成 map
        jira_map = {t["workflow_id"]: dict(t) for t in jira_tickets if t.get("workflow_id")}

        # B) 已存在的 pipeline workflow_ids
        existing_wids = {row.get("workflow_id") for row in pipeline_data if row.get("workflow_id")}

        # C) 處理 DRAFT workflow
        for w in workflows:
            if (w.get("status") or "").upper() != "DRAFT":
                continue
            wid = w["workflow_id"]
            try:
                payload = json.loads(w.get("request_payload") or "{}")
                summary = _generate_create_summary(payload) if payload else "-"
            except Exception:
                summary = "-"

            jira_map[wid] = {
                "workflow_id": wid,
                "ticket_id": None,
                "project_key": None,
                "summary": summary,
                "description": None,
                "status": None,
                "url": None,
                "created_at": _to_iso(w.get("created_at")),
            }

            if wid not in existing_wids:
                pipeline_data.insert(0, {
                    "workflow_id": wid,
                    "pipeline_id": None,
                    "status": "DRAFT",
                    "created_at": _to_iso(w.get("created_at")),
                    "finished_at": None,
                    "duration": None,
                    "created_by": w.get("created_by"),
                })
                existing_wids.add(wid)

        # D) 用 workflow 覆蓋 pipeline_data 的顯示欄位
        for row in pipeline_data:
            wid = row.get("workflow_id")
            if wid in wf_status_map:
                row["status"] = wf_status_map[wid]
            row["created_by"] = wf_created_by_map.get(wid, row.get("created_by"))

        jira_tickets = list(jira_map.values())

    except Exception as e:
        logging.error(f"overview_index error: {e}")
        jira_tickets, pipeline_data = [], []

    return render_template("overview_index.html", jira_tickets=jira_tickets, pipeline_data=pipeline_data)

@vm_bp.route('/api/get_vms_by_environment/<string:environment>')
def get_vms_by_environment_api(environment):
    """
    API endpoint to fetch VMs for a given environment.
    給 create/form.html 檢查相同環境下有沒有重複的 VM Name
    """
    try:
        vms = get_vms_by_environment(environment)
        return jsonify(vms)
    except Exception as e:
        logging.error(f"Error in get_vms_by_environment_api: {e}")
        return jsonify({"error": "Internal server error"}), 500

@vm_bp.route('/api/get_vm_config/<string:environment>/<string:vm_name_prefix>')
def get_vm_config_api(environment, vm_name_prefix):
    """
    API endpoint to fetch a specific VM's configuration.
    再 update/form.html 選了某個 VM 後，需要把現有設定抓回來顯示
    """
    try:
        config = get_vm_config(environment, vm_name_prefix)
        if not config:
            return jsonify({"error": "Configuration not found"}), 404
        return jsonify(config)
    except Exception as e:
        logging.error(f"Error in get_vm_config_api: {e}")
        return jsonify({"error": "Internal server error"}), 500

# --- Save/Update Draft (Create VM configuration) ---
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

# --- Save/Update Draft (Update VM configuration) ---
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
        new_workflow_id = insert_workflow_draft(created_by, payload)

        flash(f"New update draft #{new_workflow_id} for {prefix} has been created.", "success")

    except Exception as e:
        current_app.logger.exception(f"[vsphere_update_vm_review] save draft failed: {e}")
        flash(f"Failed to save update draft: {e}", "danger")

    return redirect(url_for('vm.overview_index'))

@vm_bp.route("/vsphere/vm/cancel")
def vsphere_cancel_vm_form():
    """Redirects to the overview page, as session is no longer used for forms."""
    return redirect(url_for('vm.overview_index'))

# --- Submit & Approval Workflow ---
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

        # === 2. Jira ===
        jira_ticket = get_jira_ticket_by_workflow_id(workflow_id)
        if not jira_ticket:
            logging.info(f"No Jira ticket for workflow #{workflow_id}, creating new one...")
            jira_key = create_jira_ticket(form_data)
            ticket_data = get_jira_issue_detail(jira_key)
            insert_jira_info_to_db(workflow_id, ticket_data)
            flash(f"Jira ticket {jira_key} created successfully.", "success")
            jira_ticket = get_jira_ticket_by_workflow_id(workflow_id)
        else:
            logging.info(f"Using existing Jira ticket {jira_ticket['ticket_id']} for workflow #{workflow_id}.")
            flash(f"Using existing Jira ticket {jira_ticket['ticket_id']}.", "info")

        jira_key = jira_ticket["ticket_id"]

        # === 3. GitLab Pipeline ===
        pipeline = get_pipeline_details_by_workflow_id(workflow_id)
        if not pipeline:
            logging.info(f"No pipeline for workflow #{workflow_id}, triggering new one...")
            pipeline_data = trigger_gitlab_pipeline(jira_key, form_data)
            if pipeline_data.get("success"):
                insert_gitlab_pipeline_info_to_db(workflow_id, pipeline_data)
                flash(f"Pipeline {pipeline_data['pipeline_id']} has been triggered.", "info")
            else:
                raise Exception(f"Pipeline trigger failed: {pipeline_data.get('error', 'Unknown error')}")
        else:
            logging.info(f"Using existing pipeline {pipeline['pipeline_id']} for workflow #{workflow_id}.")
            flash(f"Using existing pipeline {pipeline['pipeline_id']}.", "info")

        # === 4. 更新 workflow 狀態 ===
        update_request_status(workflow_id, "IN_PROGRESS")

    except Exception as e:
        logging.error(f"Submit failed for workflow {workflow_id}: {e}", exc_info=True)
        flash(f"Failed to submit request: {e}", "danger")

    # === 5. 統一導回 overview ===
    redirect_url = url_for("vm.overview_index")
    return f'<script>window.top.location="{redirect_url}"</script>'

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

# --- Draft: Edit / Delete / Review ---
@vm_bp.route("/workflow/draft/<int:workflow_id>/edit", methods=["GET"])
def workflow_draft_edit(workflow_id: int):
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
    """
    刪除 DRAFT。
    """
    try:
        affected = delete_draft_by_workflow_id(workflow_id)

        if affected == 0:
            return jsonify({"success": False, "message": "Draft not found or not deletable."}), 400

        return jsonify({"success": True})

    except Exception as e:
        logging.error(f"[workflow_draft_delete] {e}", exc_info=True)
        return jsonify({"success": False, "message": str(e)}), 500

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

        # 這裡統一補上，避免 'dict object has no attribute workflow_id'
        if 'workflow_id' not in wf:
            wf['workflow_id'] = workflow_id

        # 安全解析 payload
        raw = wf.get('request_payload') or "{}"
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        # 判斷是否為 update 類型（你現在用 'new_config' 作為判斷）
        is_update_action = 'new_config' in payload

        if is_update_action:
            return render_template("update/review.html", data=payload, workflow=wf)
        else:
            return render_template("create/review.html", data=payload, workflow=wf)

    except Exception as e:
        logging.error(f"[workflow_review_page] {e}")
        flash("Failed to load review page.", "danger")
        return redirect(url_for('vm.overview_index'))

@vm_bp.route('/workflow/return/<int:workflow_id>', methods=['POST'])
def workflow_return(workflow_id):
    """
    Return (退件) 流程：
    1) workflow_runs → status='RETURNED', returned_reason=reason（抽到 workflow_manager）
    2) GitLab → cancel manual jobs（僅在有 manual 工作的 pipeline）
    3) Jira → 通用函式：加 comment + 轉為 Done（避免被排程打回 Pending_Approval）
    4) 更新本地 DB：jira_tickets.status = 'Done'
    5) Audit log
    + from_modal 支援父頁跳轉：與 vsphere_submit_request() 一致
    """

    # 讓 modal 關閉後回父頁顯示 flash
    from_modal = (request.args.get("from_modal") == "1") or (request.form.get("from_modal") == "1")

    # 同時支援 JSON 與 form 的欄位名稱：returned_reason / reason
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

        # 1) DB：更新 workflow_runs（由 helper 內部處理連線）
        try:
            return_request(workflow_id, reason, returned_by=returned_by)
        except TypeError:
            # 相容舊版 helper（沒有 returned_by 參數）
            return_request(workflow_id, reason)

        # 2) GitLab：取消 manual job（若有 pipeline）
        pipeline = get_pipeline_details_by_workflow_id(workflow_id)
        if pipeline and pipeline.get("pipeline_id"):
            try:
                from .gitlab_api.cancel_manual_jobs import cancel_manual_jobs
                cancel_manual_jobs(pipeline["pipeline_id"])
            except Exception as e:
                current_app.logger.warning(f"[WORKFLOW_RETURN] cancel_manual_jobs failed: {e}")

        # 3) Jira：加 comment + transition 到 RETURNED（或你定義的狀態）
        # 4) 本地 DB：同步更新 jira_tickets.status = 'Done'
        jira_ticket = get_jira_ticket_by_workflow_id(workflow_id)
        if jira_ticket and jira_ticket.get("ticket_id"):
            try:
                jira_return_issue(jira_ticket["ticket_id"], reason, transition_name='RETURNED')
            except Exception as e:
                current_app.logger.warning(f"[WORKFLOW_RETURN] jira_return_issue failed: {e}")

            try:
                update_jira_ticket_status(jira_ticket["ticket_id"], "RETURNED")
            except Exception as e:
                current_app.logger.warning(f"[WORKFLOW_RETURN] update_jira_ticket_status failed: {e}")

        # 5) Audit log
        current_app.logger.info(
            f"[WORKFLOW_RETURN] workflow_id={workflow_id}, reason={reason}, returned_by={returned_by}"
        )

        flash(f"Workflow {workflow_id} has been returned.", "warning")

    except Exception as e:
        current_app.logger.exception(f"[WORKFLOW_RETURN] failed for workflow_id={workflow_id}")
        flash(f"Failed to return workflow: {e}", "danger")

    redirect_url = url_for('vm.overview_index')
    if from_modal:
        return f'<script>try{{window.top.location="{redirect_url}";}}catch(e){{window.parent.location="{redirect_url}";}}</script>'
    return redirect(redirect_url)

# --- vSphere Connection Management Routes ---
@vm_bp.route("/vsphere/connections", methods=['GET', 'POST'])
def manage_vsphere_connections():
    try:
        if request.method == 'POST':
            add_or_update_vsphere_connection(
                env=request.form['environment'],
                host=request.form['host'],
                user=request.form['user'],
                password_plain=request.form['password']
            )
            flash(f"Connection for environment '{request.form['environment']}' saved successfully.", "success")
            return redirect(url_for('vm.manage_vsphere_connections'))

        connections = get_all_vsphere_connections()
        return render_template("manage_vsphere_connection.html", connections=connections)

    except Exception as e:
        flash(f"An error occurred: {e}", "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

@vm_bp.route("/vsphere/connections/change_password/<int:conn_id>", methods=['POST'])
def change_vsphere_password(conn_id):
    """
    Supports both:
    - Normal form submit (flash + redirect)
    - AJAX (X-Requested-With: XMLHttpRequest) -> JSON {success, message, errors?}
    Expected fields: current_password, new_password, confirm_new_password
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    try:
        current_password = request.form.get('current_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_new_password = request.form.get('confirm_new_password', '').strip()

        # Basic validation
        errors = {}
        if not current_password:
            errors["current_password"] = "Current password is required."
        if not new_password:
            errors["new_password"] = "New password is required."
        if confirm_new_password != new_password:
            errors["confirm_new_password"] = "New passwords do not match."

        if errors:
            if is_ajax:
                return jsonify({"success": False, "message": "Validation failed.", "errors": errors}), 400
            for _, msg in errors.items():
                flash(msg, "danger")
            return redirect(url_for('vm.manage_vsphere_connections'))

        success, message = update_connection_password(conn_id, current_password, new_password)

        if is_ajax:
            return jsonify({"success": success, "message": message}), (200 if success else 400)

        flash(message, "success" if success else "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": f"Internal error: {e}"}), 500
        flash(f"Error updating password: {e}", "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

@vm_bp.route("/vsphere/connections/delete/<int:conn_id>", methods=['POST'])
def delete_vsphere_connection(conn_id):
    """
    刪除指定的 vSphere 連線。
    """
    try:
        affected = delete_vsphere_connection_by_id(conn_id)
        if affected == 0:
            flash(f"Connection {conn_id} not found.", "warning")
        else:
            flash("Connection deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting connection: {e}", "danger")

    return redirect(url_for('vm.manage_vsphere_connections'))

@vm_bp.route("/vsphere/connections/toggle/<int:conn_id>", methods=['POST'])
def toggle_vsphere_connection(conn_id):
    """
    切換 vsphere 連線的啟用狀態。
    """
    try:
        affected = toggle_connection_status(conn_id)
        if affected > 0:
            flash("Connection status updated.", "info")
        else:
            flash("No connection updated (maybe not found).", "warning")
    except Exception as e:
        logging.error(f"[toggle_vsphere_connection] {e}")
        flash(f"Error updating status: {e}", "danger")

    return redirect(url_for('vm.manage_vsphere_connections'))

# --- API Routes ---
# (1) 依 Environment 取得「Active」主機清單（供前端在 Environment 選定後載入 Host 下拉選單）
@vm_bp.route('/api/vsphere/hosts/<string:environment>')
def get_hosts_by_environment_api(environment):
    """
    API: 依 environment 取得可用的 vSphere hosts
    """
    try:
        hosts = get_hosts_by_environment(environment, active_only=True) or []
        return jsonify({"environment": environment, "hosts": hosts})
    except Exception as e:
        logging.error(f"[get_hosts_by_environment_api] {environment} -> {e}")
        return jsonify({"error": "An internal server error occurred"}), 500

# (2) 依 vSphere Host 取得 vSphere 物件（供前端在 Host 選定後載入 Placement/Template/Datastore/Network 等）
@vm_bp.route('/api/vsphere_objects/by_host/<path:host>')
def get_vsphere_objects_by_host_api(host):
    """
    取得指定 host 的 vSphere 物件清單。
    """
    try:
        conn_info = get_vsphere_connection_by_host(host, require_active=True)
        if not conn_info:
            return jsonify({"error": f"Active vSphere connection not found for host: {host}"}), 404

        # 解密失敗時，沿用你原本的回傳內容與狀態碼
        if conn_info.get('password') is None:
            error_msg = conn_info.get('decrypt_error', 'Password decryption failed.')
            return jsonify({"success": False, "message": error_msg}), 400

        # 呼叫 vSphere API 取得物件
        vsphere_data = get_vsphere_objects(
            host=conn_info['host'],
            user=conn_info['user'],
            password=conn_info['password']
        ) or {}

        return jsonify(vsphere_data)

    except Exception as e:
        logging.error(f"Error in get_vsphere_objects_by_host_api for host {host}: {e}")
        traceback.print_exc()
        return jsonify({"error": "An internal server error occurred"}), 500

# (3) 保留：VM 連線測試（管理頁使用）
@vm_bp.route("/api/vsphere/connections/test/<int:conn_id>", methods=['POST'])
def test_vsphere_connection_api(conn_id):
    """
    連線測試
    （route 不處理 DB，改由 helper 自行建/關連線）
    """
    try:
        # 改：helper 自行處理 DB 連線/關閉
        conn_info = get_vsphere_connection_by_id(conn_id)

        if not conn_info:
            return jsonify({"success": False, "message": "Connection not found or is inactive."}), 404

        if conn_info.get('password') is None:
            return jsonify({
                "success": False,
                "message": conn_info.get('decrypt_error', 'Password decrypt failed.')
            }), 400

        is_ok, message = test_vsphere_connection(
            host=conn_info['host'],
            user=conn_info['user'],
            password=conn_info['password']
        )
        return jsonify({"success": is_ok, "message": message})

    except Exception as e:
        logging.error(f"Error in test_vsphere_connection_api for conn_id {conn_id}: {e}")
        return jsonify({"success": False, "message": "An internal server error occurred."}), 500
