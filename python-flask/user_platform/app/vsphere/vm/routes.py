from flask import render_template, request, redirect, url_for, session, flash, jsonify
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

# === [新增] 匯入 summary 產生器：用於 DRAFT 顯示 ===
from .jira_api.create_jira_ticket import _generate_create_summary


# --- 主視圖與 API (Views & APIs) ---

@vm_bp.route("/vsphere/vm")
def vm_index():
    """
    Render the main VM management page.
    """
    VCENTER_HOST = "172.26.1.60"
    VCENTER_USER = "administrator@vsphere.local"
    VCENTER_PASSWORD = "Gict@1688+"
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

    [修改說明]
    - 原本只顯示 pipeline_data。現在會把 workflow_runs 的 DRAFT 也合併成列，一樣走同一個 table。
    - DRAFT 列的欄位對齊 pipeline：
        workflow_id: 來自 workflow_runs
        status: 'draft'
        created_at: workflow_runs.created_at
        finished_at: None
        duration: None
        pipeline_id: None
    - 同時「造一個類 Jira 物件」，只有 summary，讓原本模板的 summary 欄位能顯示。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()

        # 既有資料
        jira_tickets = get_jira_tickets_and_stats(db_conn)
        pipeline_data = get_gitlab_pipeline_detail_and_stats(db_conn)

        # === 新增：抓取 DRAFT 工作流 ===
        cur = db_conn.cursor(dictionary=True)
        cur.execute("""
            SELECT workflow_id, created_at, request_payload
            FROM workflow_runs
            WHERE status = 'DRAFT'
            ORDER BY created_at DESC
        """)
        drafts = cur.fetchall()
        cur.close()

        # 將 DRAFT 轉成「pipeline-like」資料，方便沿用同一張表渲染
        for d in drafts:
            # 預設 pipeline 欄位
            draft_row = {
                "workflow_id": d["workflow_id"],
                "pipeline_id": None,
                "status": "draft",            # 讓前端 badge 顯示 Draft
                "created_at": d["created_at"],
                "finished_at": None,
                "duration": None,
                # 其他欄位若模板未使用，可忽略
            }
            pipeline_data.insert(0, draft_row)  # 草稿放在最上面較直覺

            # 產生「類 Jira 物件」只為了顯示 Summary
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


# --- Review and Cancel Routes ---

@vm_bp.route("/vsphere/vm/create/review", methods=["POST"])
def vsphere_create_vm_review():
    """
    Handles the review step for VM creation.
    [註] 目前被 Save to Draft 使用：寫入 workflow_runs.DRAFT
    """
    form_data = request.form.to_dict(flat=False)
    processed_form_data = {}
    for key, value in form_data.items():
        if key.endswith('[]'):
            processed_form_data[key] = value
        else:
            processed_form_data[key] = value[0] if isinstance(value, list) and value else value

    # 保留原本 session 行為（若你已全面改 DB，可移除此段）
    session["form_scope"] = "create"
    session["create_vm_form_data"] = processed_form_data
    session.pop("vm_update_form_data", None)

    # === 新增：直接記成 DRAFT ===
    db_conn = None
    try:
        db_conn = get_db_connection()
        # 這裡 created_by 你可以改成實際登入使用者名稱（目前示意）
        created_by = session.get("user", "webform_user")
        # 建 DRAFT：你如果已用新的 record_draft_request()，可替換掉
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


# --- [CORE REFACTOR] New Submit & Approval Workflow ---

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
    [新] 統一的 Submit 路由，處理 Create 和 Update 請求的第一階段。
    職責：建立待審批的工作流 (workflow)、Jira Ticket 和一個等待手動批准的 GitLab Pipeline。
    """
    form_data = _get_form_data_from_session()

    if not form_data:
        flash("Your session has expired or the form is empty. Please fill out the form again.", "warning")
        return redirect(url_for('vm.vm_index'))

    db_conn = None
    workflow_id = None
    try:
        db_conn = get_db_connection()
        
        # 1. 將請求意圖寫入 DB，建立一個 PENDING_APPROVAL 的工作流
        workflow_id = record_pending_request(db_conn, triggered_by="webform_submit", form_data=form_data)

        # 2. 建立 Jira Ticket
        jira_key = create_jira_ticket(form_data)
        ticket_data = get_jira_issue_detail(jira_key)
        insert_jira_info_to_db(db_conn, workflow_id, ticket_data)
        flash(f"Jira ticket {jira_key} created successfully.", "success")

        # 3. 觸發 GitLab Pipeline (此 Pipeline 會有一個 manual job 等待批准)
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
        # [TODO] 之後在此處增加呼叫 update_workflow_to_failed 的邏輯
        return redirect(url_for('vm.vm_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


@vm_bp.route('/workflow/approve/<int:workflow_id>', methods=['GET'])
def workflow_approve_page(workflow_id):
    """
    [新] 顯示審批頁面，預覽即將執行的變更。
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
        request_details = json.loads(workflow['request_payload'])
        
        # 註解: create/approve.html 模板需要被修改，以更好地展示來自 request_details 的內容。
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
    [新] 處理 "Approve & Execute" 按鈕的點擊。
    職責：將待辦事項正式寫入 DB，並解鎖 GitLab Pipeline 的 manual job。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        
        # 1. 將變更請求正式應用到 DB (狀態變更為 PENDING_*)
        apply_request_to_db(db_conn, workflow_id)
        
        # 2. 找到對應的 pipeline_id
        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        if not pipeline or not pipeline.get('pipeline_id'):
            raise Exception("Could not find the associated pipeline to execute.")
            
        # 3. 解鎖 GitLab Pipeline 的 manual job
        result = run_manual_job(pipeline['pipeline_id'])
        if not result.get("success"):
            raise Exception(f"Failed to trigger manual job: {result.get('error')}")

        flash(f"Request {workflow_id} has been approved. The pipeline is now running.", "success")
        
    except Exception as e:
        logging.error(f"Failed to execute workflow {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to approve request: {e}", "danger")
        # [TODO] 之後在此處增加呼叫 update_workflow_to_failed 的邏輯
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))