from flask import render_template, request, redirect, url_for, session, flash, jsonify, current_app
import traceback
import json
import logging

from . import vm_bp
from app.mysql.db import get_db_connection

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- DB 操作模組 ---
from .db.get_vm_configurations import get_environment, get_vms_by_environment, get_vm_config
from .db.get_jira_tickets_and_stats import get_jira_tickets_and_stats, get_jira_ticket_by_workflow_id
from .db.get_gitlab_pipeline_detail_and_stats import get_gitlab_pipeline_detail_and_stats, get_pipeline_details_by_workflow_id
from .db.insert_jira_info_to_db import insert_jira_info_to_db
from .db.insert_gitlab_pipeline_info_to_db import insert_gitlab_pipeline_info_to_db
from .db.workflow_manager import record_pending_request, update_request_status, cancel_request, apply_request_to_db

# --- API 函式 ---
from .vsphere_api.get_vsphere_objects import get_vsphere_objects
from .jira_api.create_jira_ticket import create_jira_ticket
from .jira_api.get_jira_issue_detail import get_jira_issue_detail
from .gitlab_api.trigger_gitlab_pipeline import trigger_gitlab_pipeline
from .gitlab_api.run_manual_job import run_manual_job

# === 匯入 summary 產生器：用於 DRAFT 顯示 ===
from .jira_api.create_jira_ticket import _generate_create_summary


# --- 主視圖與 API (Views & APIs) ---

@vm_bp.route("/vsphere/vm")
def vm_index():
    """
    Render the main VM management page.
    """
    VCENTER_HOST = current_app.config['VSPHERE_HOST']
    VCENTER_USER = current_app.config['VSPHERE_USER']
    VCENTER_PASSWORD = current_app.config['VSPHERE_PASSWORD']
    vsphere_data = get_vsphere_objects(VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD)

    db_conn = None
    try:
        db_conn = get_db_connection()
        environment = get_environment(db_conn)
    except Exception as e:
        logging.error(f"Database connection error in vm_index: {e}")
        environment = []
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return render_template(
        "vm_index.html",
        datacenters=vsphere_data["datacenters"], clusters=vsphere_data["clusters"],
        templates=vsphere_data["templates"], networks=vsphere_data["networks"],
        datastores=vsphere_data["datastores"], vm_name=vsphere_data["vm_name"],
        environment=environment,
        session_data=session.get('create_vm_form_data', {})
    )


@vm_bp.route("/vsphere/overview")
def overview_index():
    """
    Render the overview page with all requests and their statuses.
    - 合併 workflow_runs 的 DRAFT 到同一張表。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()

        # 既有資料
        jira_tickets = get_jira_tickets_and_stats(db_conn)
        pipeline_data = get_gitlab_pipeline_detail_and_stats(db_conn)

        # 取 DRAFT
        cur = db_conn.cursor(dictionary=True)
        cur.execute("""
            SELECT workflow_id, created_at, request_payload
            FROM workflow_runs
            WHERE status = 'DRAFT'
            ORDER BY created_at DESC
        """)
        drafts = cur.fetchall()
        cur.close()

        # 轉為 pipeline-like 並補 Summary
        for d in drafts:
            draft_row = {
                "workflow_id": d["workflow_id"],
                "pipeline_id": None,
                "status": "draft",
                "created_at": d["created_at"],
                "finished_at": None,
                "duration": None,
            }
            pipeline_data.insert(0, draft_row)

            summary = "-"
            try:
                payload = json.loads(d["request_payload"] or "{}")
                summary = _generate_create_summary(payload) if payload else "-"
            except Exception:
                pass

            jira_tickets.append({
                "workflow_id": d["workflow_id"],
                "ticket_id": None,
                "project_key": None,
                "summary": summary,
                "description": None,
                "status": None,
                "url": None,
                "created_at": d["created_at"],
            })

    except Exception as e:
        logging.error(f"Database error in overview_index: {e}")
        jira_tickets = []
        pipeline_data = []
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return render_template("overview_index.html", jira_tickets=jira_tickets, pipeline_data=pipeline_data)


@vm_bp.route('/api/get_vms_by_environment/<string:environment>')
def get_vms_by_environment_api(environment):
    """API endpoint to fetch VMs for a given environment."""
    db_conn = None
    try:
        db_conn = get_db_connection()
        vms = get_vms_by_environment(db_conn, environment)
        return jsonify(vms)
    except Exception as e:
        logging.error(f"Error in get_vms_by_environment_api: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route('/api/get_vm_config/<string:environment>/<string:vm_name_prefix>')
def get_vm_config_api(environment, vm_name_prefix):
    """API endpoint to fetch a specific VM's configuration."""
    db_conn = None
    try:
        db_conn = get_db_connection()
        config = get_vm_config(db_conn, environment, vm_name_prefix)
        if not config:
            return jsonify({"error": "Configuration not found"}), 404
        return jsonify(config)
    except Exception as e:
        logging.error(f"Error in get_vm_config_api: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


# --- Review / Cancel（沿用） ---

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

    # 保留 session（你目前 submit 流程還需要）
    session["form_scope"] = "create"
    session["create_vm_form_data"] = processed_form_data
    session.pop("vm_update_form_data", None)

    db_conn = None
    try:
        db_conn = get_db_connection()
        created_by = session.get("user", "webform_user")

        wf_id = processed_form_data.get("workflow_id")
        if wf_id:
            cur = db_conn.cursor()
            cur.execute("SELECT status FROM workflow_runs WHERE workflow_id=%s", (wf_id,))
            row = cur.fetchone()
            if row and (row[0] == 'DRAFT'):
                cur.execute(
                    "UPDATE workflow_runs SET request_payload=%s, updated_at=NOW() WHERE workflow_id=%s",
                    (json.dumps(processed_form_data), wf_id)
                )
                db_conn.commit()
                cur.close()
                return redirect(url_for('vm.overview_index'))

        # 新增 DRAFT
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO workflow_runs (created_by, status, request_payload) VALUES (%s, 'DRAFT', %s)",
            (created_by, json.dumps(processed_form_data))
        )
        db_conn.commit()
        cur.close()

    except Exception as e:
        logging.error(f"Failed to save draft: {e}")
        flash(f"Failed to save draft: {e}", "danger")
        return redirect(url_for('vm.vm_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


@vm_bp.route("/vsphere/vm/update/review", methods=["POST"])
def vsphere_update_vm_review():
    """
    Handles the review step for VM updates.
    """
    new_config = request.form.to_dict(flat=False)
    processed_new_config = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in new_config.items()}
    
    env = processed_new_config.get('environment')
    prefix = processed_new_config.get('vm_name_prefix')
    
    db_conn = None
    original_config = {}
    try:
        db_conn = get_db_connection()
        original_config = get_vm_config(db_conn, env, prefix) or {}
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()
    
    session['form_scope'] = 'update'
    session['vm_update_form_data'] = {
        'original_config': original_config,
        'new_config': processed_new_config
    }
    session.pop('create_vm_form_data', None)
    return render_template("update/review.html", data=session["vm_update_form_data"])


@vm_bp.route("/vsphere/vm/cancel")
def vsphere_cancel_vm_form():
    """Clears form data from session."""
    session.pop("create_vm_form_data", None)
    session.pop("vm_update_form_data", None)
    session.pop("form_scope", None)
    return redirect(url_for('vm.overview_index'))


# --- Submit & Approval Workflow（原樣保留） ---

def _get_form_data_from_session():
    """輔助函式：從 session 中獲取對應的表單資料並清理 session。"""
    form_scope = session.pop("form_scope", None)
    if form_scope == "create":
        return session.pop("create_vm_form_data", None)
    elif form_scope == "update":
        return session.pop("vm_update_form_data", {}).get("new_config")
    return None


@vm_bp.route("/vsphere/vm/submit", methods=["POST"])
def vsphere_submit_request():
    """
    統一 Submit 路由。
    """
    form_data = _get_form_data_from_session()

    if not form_data:
        flash("Your session has expired or the form is empty. Please fill out the form again.", "warning")
        return redirect(url_for('vm.vm_index'))

    db_conn = None
    workflow_id = None
    try:
        db_conn = get_db_connection()
        
        # 1) 建立 PENDING_APPROVAL 工作流
        workflow_id = record_pending_request(db_conn, triggered_by="webform_submit", form_data=form_data)

        # 2) Jira
        jira_key = create_jira_ticket(form_data)
        ticket_data = get_jira_issue_detail(jira_key)
        insert_jira_info_to_db(db_conn, workflow_id, ticket_data)
        flash(f"Jira ticket {jira_key} created successfully.", "success")

        # 3) GitLab Pipeline（等待手動批准）
        pipeline_result = trigger_gitlab_pipeline(jira_key, form_data)
        if pipeline_result.get("success"):
            insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_result, form_data)
            flash(f"Pipeline {pipeline_result['pipeline_id']} has been triggered and is now awaiting approval.", "info")
        else:
            raise Exception(f"Failed to trigger GitLab Pipeline: {pipeline_result.get('error', 'Unknown error')}")

    except Exception as e:
        logging.error(f"An error occurred during the submit process for workflow_id {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to submit request: {e}", "danger")
        return redirect(url_for('vm.vm_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


@vm_bp.route('/workflow/approve/<int:workflow_id>', methods=['GET'])
def workflow_approve_page(workflow_id):
    """
    顯示審批頁面。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        workflow = cursor.fetchone()
        
        if not workflow:
            flash(f"Workflow ID {workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        jira_ticket = get_jira_ticket_by_workflow_id(db_conn, workflow_id)
        request_details = json.loads(workflow['request_payload']) if workflow.get('request_payload') else {}
        
        return render_template("create/approve.html", 
                               workflow=workflow,
                               pipeline=pipeline, 
                               jira_ticket=jira_ticket,
                               request_details=request_details)
    except Exception as e:
        flash(f"Error loading approval page: {e}", "danger")
        return redirect(url_for('vm.overview_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route('/workflow/execute/<int:workflow_id>', methods=['POST'])
def workflow_execute(workflow_id):
    """
    Approve & Execute。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        apply_request_to_db(db_conn, workflow_id)
        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        if not pipeline or not pipeline.get('pipeline_id'):
            raise Exception("Could not find the associated pipeline to execute.")
        result = run_manual_job(pipeline['pipeline_id'])
        if not result.get("success"):
            raise Exception(f"Failed to trigger manual job: {result.get('error')}")
        flash(f"Request {workflow_id} has been approved. The pipeline is now running.", "success")
    except Exception as e:
        logging.error(f"Failed to execute workflow {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to approve request: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


# === Draft: Edit / Delete / Review ===

@vm_bp.route("/workflow/draft/<int:workflow_id>/edit", methods=["GET"])
def workflow_draft_edit(workflow_id: int):
    """
    打開原本的 create/form.html，並以 workflow_runs.request_payload 回填欄位。
    """
    VCENTER_HOST = current_app.config['VSPHERE_HOST']
    VCENTER_USER = current_app.config['VSPHERE_USER']
    VCENTER_PASSWORD = current_app.config['VSPHERE_PASSWORD']
    vsphere_data = get_vsphere_objects(VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD)

    db_conn = None
    draft_data = {}
    environments = []
    try:
        db_conn = get_db_connection()
        environments = get_environment(db_conn)

        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT status, request_payload FROM workflow_runs WHERE workflow_id=%s", (workflow_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            flash(f"Draft #{workflow_id} not found.", "warning")
            return redirect(url_for('vm.overview_index'))

        if row["status"] != "DRAFT":
            flash(f"Workflow #{workflow_id} is not editable (status={row['status']}).", "warning")
            return redirect(url_for('vm.overview_index'))

        draft_data = json.loads(row["request_payload"] or "{}")

    except Exception as e:
        logging.error(f"[workflow_draft_edit] {e}")
        flash(f"Failed to open draft: {e}", "danger")
        return redirect(url_for('vm.overview_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return render_template(
        "create/form.html",
        datacenters=vsphere_data["datacenters"], clusters=vsphere_data["clusters"],
        templates=vsphere_data["templates"], networks=vsphere_data["networks"],
        datastores=vsphere_data["datastores"], vm_name=vsphere_data["vm_name"],
        environment=environments,
        draft_data=draft_data,
        workflow_id=workflow_id
    )


@vm_bp.route("/workflow/draft/<int:workflow_id>/delete", methods=["POST"])
def workflow_draft_delete(workflow_id: int):
    """
    刪除 DRAFT。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        cur = db_conn.cursor()
        cur.execute("DELETE FROM workflow_runs WHERE workflow_id=%s AND status='DRAFT'", (workflow_id,))
        affected = cur.rowcount
        db_conn.commit()
        cur.close()
        if affected == 0:
            return jsonify({"success": False, "message": "Draft not found or not deletable."}), 400
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"[workflow_draft_delete] {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route('/workflow/review/<int:workflow_id>', methods=['GET'])
def workflow_review_page(workflow_id: int):
    """
    以現有 create/review.html 呈現草稿或待審資料（供 Modal iframe 使用）
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        wf = cur.fetchone()
        cur.close()

        if not wf:
            flash(f"Workflow ID {workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        payload = {}
        try:
            payload = json.loads(wf.get('request_payload') or "{}")
        except Exception:
            payload = {}

        return render_template("create/review.html", data=payload, workflow=wf)

    except Exception as e:
        logging.error(f"Error in workflow_review_page: {e}")
        flash("Failed to load review page.", "danger")
        return redirect(url_for('vm.overview_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()