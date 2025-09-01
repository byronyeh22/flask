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
from .db.workflow_manager import record_pending_request, update_request_status, apply_request_to_db, return_request
from .db.update_jira_ticket_status import update_jira_ticket_status

# --- API 函式 ---
from .vsphere_api.get_vsphere_objects import get_vsphere_objects
from .vsphere_api.test_connection import test_vsphere_connection
from .jira_api.create_jira_ticket import create_jira_ticket
from .jira_api.get_jira_issue_detail import get_jira_issue_detail
from .jira_api.issue_updates import jira_return_issue  # <== 新增：Jira 通用退件
from .gitlab_api.trigger_gitlab_pipeline import trigger_gitlab_pipeline
from .gitlab_api.run_manual_job import run_manual_job
from .gitlab_api.cancel_manual_jobs import cancel_manual_jobs

# --- vSphere Connection Manager ---
from .db.vsphere_connections_manager import (
    get_all_vsphere_connections,
    get_active_vsphere_connections,
    # get_vsphere_connection_by_env,  # ← 已改為以 host 為主，移除不用
    add_or_update_vsphere_connection,
    update_connection_password,
    delete_vsphere_connection_by_id,
    toggle_connection_status,
    get_vsphere_connection_by_id,
    # 新增：依環境取主機、依主機取連線
    get_hosts_by_environment,
    get_vsphere_connection_by_host,
)

# --- 匯入 summary 產生器：用於 DRAFT 顯示 ---
from .jira_api.create_jira_ticket import _generate_create_summary

# --- hleper ---

# 抓取目前登入 User
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





# --- 主視圖與 API (Views & APIs) ---

@vm_bp.route("/vsphere/vm")
def vm_index():
    """
    Render the main VM management page.
    """
    # 移除舊的 vSphere 連線資訊讀取
    # VCENTER_HOST = current_app.config['VSPHERE_HOST']
    # VCENTER_USER = current_app.config['VSPHERE_USER']
    # VCENTER_PASSWORD = current_app.config['VSPHERE_PASSWORD']
    # vsphere_data = get_vsphere_objects(VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD)

    db_conn = None
    environments = []
    try:
        db_conn = get_db_connection()
        # 【修改】從 vsphere_connections 取得所有已啟用的環境
        active_connections = get_active_vsphere_connections(db_conn)
        # 取出所有 environment，做大小寫不敏感且保序的去重
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
    except Exception as e:
        logging.error(f"Database connection error in vm_index: {e}")
        environments = []
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return render_template(
        "vm_index.html",
        # 傳遞 environment 列表到你的模板
        environment=environments,
        # 初始載入時，這些可以為空，或提供一個預設值
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
    def _to_iso(val):
        try:
            return val.isoformat()
        except Exception:
            return str(val) if val else "1970-01-01T00:00:00Z"

    def _ensure_created_at(item: dict):
        if not item.get("created_at"):
            item["created_at"] = item.get("started_at") or "1970-01-01T00:00:00Z"
        item["created_at"] = _to_iso(item["created_at"])
        return item

    db_conn = None
    try:
        db_conn = get_db_connection()

        # 1) 原資料
        jira_tickets = get_jira_tickets_and_stats(db_conn) or []
        pipeline_data = get_gitlab_pipeline_detail_and_stats(db_conn) or []
        pipeline_data = [_ensure_created_at(dict(p)) for p in pipeline_data]

        # 2) 取全部 workflow（包含 DRAFT）
        cur = db_conn.cursor(dictionary=True)
        cur.execute("""
            SELECT workflow_id, status, created_at, request_payload, created_by
            FROM workflow_runs
            ORDER BY created_at DESC
        """)
        workflows = cur.fetchall()
        cur.close()

        wf_status_map = {w["workflow_id"]: w["status"] for w in workflows}
        wf_created_by_map = {w["workflow_id"]: w.get("created_by") for w in workflows}

        # --- A) 先把 JIRA tickets 收成 map，後面針對 Draft 直接覆蓋 ---
        jira_map = {}
        for t in (jira_tickets or []):
            wid = t.get("workflow_id")
            if wid is not None:
                jira_map[wid] = dict(t)

        # --- B) 避免重複插入 Draft 列：蒐集現有 pipeline_data 的 wid ---
        existing_wids = {row.get("workflow_id") for row in pipeline_data if row.get("workflow_id") is not None}

        # --- C) 重新覆蓋 DRAFT 的 summary（用最新 payload 產生）
        for w in workflows:
            wid = w["workflow_id"]
            if (w.get("status") or "").upper() == "DRAFT":
                summary = "-"
                try:
                    payload = json.loads(w.get("request_payload") or "{}")
                    summary = _generate_create_summary(payload) if payload else "-"
                except Exception:
                    pass

                # 覆蓋/寫入 jira_map（確保列表顯示用的 summary 是最新）
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

                # 若 pipeline_data 裡還沒有這個 Draft 的列，補一列（避免重複）
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

        # --- D) 將覆蓋後的 jira_map 轉回 list 給模板使用 ---
        jira_tickets = list(jira_map.values())

        # --- E) 最後：用 workflow 狀態覆蓋 pipeline_data 的顯示欄位 ---
        for row in pipeline_data:
            wid = row.get("workflow_id")
            if wid in wf_status_map:
                row["status"] = wf_status_map[wid]
            if wf_created_by_map.get(wid):
                row["created_by"] = wf_created_by_map[wid]
            else:
                row.setdefault("created_by", None)

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

    db_conn = None
    try:
        db_conn = get_db_connection()
        created_by = _current_username()

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
                flash(f"Draft #{wf_id} updated successfully.", "success")
                return redirect(url_for('vm.overview_index'))

        # 新增 DRAFT
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO workflow_runs (created_by, status, request_payload) VALUES (%s, 'DRAFT', %s)",
            (created_by, json.dumps(processed_form_data))
        )
        new_workflow_id = cur.lastrowid
        db_conn.commit()
        cur.close()
        flash(f"New draft #{new_workflow_id} created successfully.", "success")

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
    Handles saving an update request as a draft.
    """
    new_config = request.form.to_dict(flat=False)
    processed_new_config = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in new_config.items()}
    
    env = processed_new_config.get('environment')
    prefix = processed_new_config.get('vm_name_prefix')
    
    db_conn = None
    try:
        db_conn = get_db_connection()
        original_config = get_vm_config(db_conn, env, prefix) or {}
        
        # Create a payload that includes both original and new configurations
        payload = {
            'original_config': original_config,
            'new_config': processed_new_config,
            # Explicitly add action_type to the top-level for easier parsing in review page
            'action_type': 'update' 
        }
        
        created_by = _current_username()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO workflow_runs (created_by, status, request_payload) VALUES (%s, 'DRAFT', %s)",
            (created_by, json.dumps(payload))
        )
        new_workflow_id = cur.lastrowid
        db_conn.commit()
        cur.close()
        flash(f"New update draft #{new_workflow_id} for {prefix} has been created.", "success")
        
    except Exception as e:
        logging.error(f"Failed to save update draft: {e}")
        flash(f"Failed to save update draft: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()
    
    return redirect(url_for('vm.overview_index'))


@vm_bp.route("/vsphere/vm/cancel")
def vsphere_cancel_vm_form():
    """Redirects to the overview page, as session is no longer used for forms."""
    return redirect(url_for('vm.overview_index'))

# --- Submit & Approval Workflow ---
@vm_bp.route("/vsphere/vm/submit", methods=["POST"])
def vsphere_submit_request():
    from_modal = (request.args.get("from_modal") == "1") or (request.form.get("from_modal") == "1")
    workflow_id = request.form.get("workflow_id")
    if not workflow_id:
        flash("Workflow ID is missing. Cannot submit request.", "danger")
        return redirect(url_for('vm.overview_index'))

    db_conn = None
    try:
        db_conn = get_db_connection()
        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT status, request_payload FROM workflow_runs WHERE workflow_id=%s LIMIT 1", (workflow_id,))
        wf = cur.fetchone()
        cur.close()

        if not wf:
            flash(f"Workflow #{workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        payload = json.loads(wf.get('request_payload') or "{}")
        form_data = payload.get('new_config', payload)

        if not form_data:
            flash("Draft content is empty. Please check the form again.", "danger")
            return redirect(url_for('vm.overview_index'))

        # --- Idempotent Jira Ticket Creation ---
        jira_ticket = get_jira_ticket_by_workflow_id(db_conn, workflow_id)
        if not jira_ticket:
            logging.info(f"No Jira ticket found for workflow #{workflow_id}. Creating a new one.")
            jira_key = create_jira_ticket(form_data)
            ticket_data = get_jira_issue_detail(jira_key)
            insert_jira_info_to_db(db_conn, workflow_id, ticket_data)
            flash(f"Jira ticket {jira_key} created successfully.", "success")
            jira_ticket = get_jira_ticket_by_workflow_id(db_conn, workflow_id) # Re-fetch to get the full record
        else:
            logging.info(f"Using existing Jira ticket {jira_ticket['ticket_id']} for workflow #{workflow_id}.")
            flash(f"Using existing Jira ticket {jira_ticket['ticket_id']}.", "info")

        jira_key = jira_ticket['ticket_id']

        # --- Idempotent GitLab Pipeline Trigger ---
        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        if not pipeline:
            logging.info(f"No GitLab pipeline found for workflow #{workflow_id}. Triggering a new one.")
            pipeline_data = trigger_gitlab_pipeline(jira_key, form_data)
            if pipeline_data.get("success"):
                insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_data)
                flash(f"Pipeline {pipeline_data['pipeline_id']} has been triggered.", "info")
            else:
                raise Exception(f"Failed to trigger GitLab Pipeline: {pipeline_data.get('error', 'Unknown error')}")
        else:
            logging.info(f"Using existing GitLab pipeline {pipeline['pipeline_id']} for workflow #{workflow_id}.")
            flash(f"Using existing GitLab pipeline {pipeline['pipeline_id']}.", "info")

        update_request_status(db_conn, workflow_id, "IN_PROGRESS")

    except Exception as e:
        logging.error(f"An error occurred during the submit process for workflow_id {workflow_id}: {e}")
        traceback.print_exc()
        # The key change is to NOT change the workflow status back to DRAFT here.
        # It remains in its current state, allowing the user to retry.
        flash(f"Failed to submit request: {e}", "danger")
        redirect_url = url_for('vm.overview_index')
        if from_modal:
            return f'<script>try{{window.top.location="{redirect_url}";}}catch(e){{window.parent.location="{redirect_url}";}}</script>'
        return redirect(redirect_url)
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    redirect_url = url_for('vm.overview_index')
    if from_modal:
        return f'<script>try{{window.top.location="{redirect_url}";}}catch(e){{window.parent.location="{redirect_url}";}}</script>'
    return redirect(redirect_url)


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
    db_conn = None
    cur = None
    try:
        db_conn = get_db_connection()

        # 1) Load environments for the dropdown (active connections only)
        active_connections = get_active_vsphere_connections(db_conn) or []
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
        cur = db_conn.cursor(dictionary=True)
        cur.execute(
            "SELECT status, request_payload FROM workflow_runs WHERE workflow_id=%s",
            (workflow_id,),
        )
        row = cur.fetchone()

        if not row:
            flash(f"Draft #{workflow_id} not found.", "warning")
            return redirect(url_for('vm.overview_index'))

        # Normalize status
        status = (row.get("status") or "").strip().upper()
        if status != "DRAFT":
            flash(f"Workflow #{workflow_id} is not editable (status={status}).", "warning")
            return redirect(url_for('vm.overview_index'))

        # 3) Parse payload safely
        payload_raw = row.get("request_payload") or "{}"
        try:
            draft_data = json.loads(payload_raw)
            if not isinstance(draft_data, dict):
                # Defensive: if payload is a list or other type, fallback
                draft_data = {}
                flash("Draft payload format is invalid; some values may not be prefilled.", "warning")
        except Exception as parse_err:
            logging.exception(f"[workflow_draft_edit] JSON parse error for workflow_id={workflow_id}")
            draft_data = {}
            flash("Failed to parse draft payload; some values may not be prefilled.", "danger")

        # 4) Determine action/resource (default: Create/VM)
        resource = (draft_data.get("resource") or "vm").strip().lower()
        action_type = (draft_data.get("action_type") or "Create").strip().lower()

        # 5) Preload vSphere lists ONLY when:
        #    - action_type == create
        #    - draft has environment
        #    Otherwise, send empty lists (front-end will fetch when env changes).
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
            draft_env = (draft_data.get("environment") or "").strip()
            if draft_env:
                try:
                    # 依目前設計：由前端選 Environment -> Host，再以 Host 取得物件
                    # 這裡僅預載 Environment 清單；實際 vSphere 物件於前端選 Host 後再載入
                    pass
                except Exception:
                    pass

            # Render create page (your existing create tab)
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
            # Your existing update entry page/template
            return render_template(
                "update/index.html",
                environment=environments,
                draft_data=draft_data,
                workflow_id=workflow_id,
            )

        elif action_type == "delete":
            # Your existing delete entry page/template
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
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        if db_conn and getattr(db_conn, "is_connected", lambda: False)():
            db_conn.close()

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

        # Determine if this is an update request by checking for 'new_config' key
        is_update_action = 'new_config' in payload

        if is_update_action:
            # For update actions, render the update review template
            return render_template("update/review.html", data=payload, workflow=wf)
        else:
            # For create actions, render the create review template
            return render_template("create/review.html", data=payload, workflow=wf)

    except Exception as e:
        logging.error(f"Error in workflow_review_page: {e}")
        flash("Failed to load review page.", "danger")
        return redirect(url_for('vm.overview_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

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

    db_conn = None
    try:
        db_conn = get_db_connection()
        returned_by = _current_username()

        # 1) DB：更新 workflow_runs（優先帶 returned_by；若尚未支援此參數則 fallback）
        try:
            return_request(db_conn, workflow_id, reason, returned_by=returned_by)
        except TypeError:
            return_request(db_conn, workflow_id, reason)

        # 2) GitLab：取消 manual job（若有 pipeline）
        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        if pipeline and pipeline.get("pipeline_id"):
            try:
                from .gitlab_api.cancel_manual_jobs import cancel_manual_jobs
                cancel_manual_jobs(pipeline["pipeline_id"])
            except Exception as e:
                current_app.logger.warning(f"[WORKFLOW_RETURN] cancel_manual_jobs failed: {e}")

        # 3) Jira：加 comment + transition 到 Done（41）
        # 4) 本地 DB：同步更新 jira_tickets.status = 'Done'
        jira_ticket = get_jira_ticket_by_workflow_id(db_conn, workflow_id)
        if jira_ticket and jira_ticket.get("ticket_id"):
            try:
                # 轉到 Done，避免排程又把它打回 Pending_Approval
                jira_return_issue(jira_ticket["ticket_id"], reason, transition_name='RETURNED')
            except Exception as e:
                current_app.logger.warning(f"[WORKFLOW_RETURN] jira_return_issue failed: {e}")

            # 無論 Jira API 是否成功，嘗試同步更新本地 DB 的狀態為 Done（你希望是 Done）
            try:
                update_jira_ticket_status(db_conn, jira_ticket["ticket_id"], "Done")
            except Exception as e:
                current_app.logger.warning(f"[WORKFLOW_RETURN] update_jira_ticket_status failed: {e}")

        # 5) Audit log
        current_app.logger.info(f"[WORKFLOW_RETURN] workflow_id={workflow_id}, reason={reason}, returned_by={returned_by}")

        flash(f"Workflow {workflow_id} has been returned.", "warning")
    except Exception as e:
        current_app.logger.exception(f"[WORKFLOW_RETURN] failed for workflow_id={workflow_id}")
        flash(f"Failed to return workflow: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    redirect_url = url_for('vm.overview_index')
    if from_modal:
        # 父頁跳轉：讓 flash 顯示在 overview
        return f'<script>try{{window.top.location="{redirect_url}";}}catch(e){{window.parent.location="{redirect_url}";}}</script>'
    return redirect(redirect_url)


# --- vSphere Connection Management Routes ---
@vm_bp.route("/vsphere/connections", methods=['GET', 'POST'])
def manage_vsphere_connections():
    db_conn = None
    try:
        db_conn = get_db_connection()
        if request.method == 'POST':
            add_or_update_vsphere_connection(
                db_conn,
                env=request.form['environment'],
                host=request.form['host'],
                user=request.form['user'],
                password_plain=request.form['password']
            )
            flash(f"Connection for environment '{request.form['environment']}' saved successfully.", "success")
            return redirect(url_for('vm.manage_vsphere_connections'))

        connections = get_all_vsphere_connections(db_conn)
        # 修正：使用您指定的正確範本檔案名稱
        return render_template("manage_vsphere_connection.html", connections=connections)
    except Exception as e:
        flash(f"An error occurred: {e}", "danger")
        # 還原：在發生錯誤時重新導向，以顯示 flash 錯誤訊息
        return redirect(url_for('vm.manage_vsphere_connections'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

@vm_bp.route("/vsphere/connections/change_password/<int:conn_id>", methods=['POST'])
def change_vsphere_password(conn_id):
    """
    Supports both:
    - Normal form submit (flash + redirect)
    - AJAX (X-Requested-With: XMLHttpRequest) -> JSON {success, message, errors?}
    Expected fields: current_password, new_password, confirm_new_password
    """
    db_conn = None
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
            # Non-AJAX: flash first error and redirect
            for _, msg in errors.items():
                flash(msg, "danger")
            return redirect(url_for('vm.manage_vsphere_connections'))

        # Do update via your existing helper
        db_conn = get_db_connection()
        success, message = update_connection_password(db_conn, conn_id, current_password, new_password)

        if is_ajax:
            status = 200 if success else 400
            return jsonify({"success": success, "message": message}), status

        flash(message, "success" if success else "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": f"Internal error: {e}"}), 500
        flash(f"Error updating password: {e}", "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

    finally:
        try:
            if db_conn and db_conn.is_connected():
                db_conn.close()
        except Exception:
            pass

@vm_bp.route("/vsphere/connections/delete/<int:conn_id>", methods=['POST'])
def delete_vsphere_connection(conn_id):
    db_conn = None
    try:
        db_conn = get_db_connection()
        delete_vsphere_connection_by_id(db_conn, conn_id)
        flash("Connection deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting connection: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()
    return redirect(url_for('vm.manage_vsphere_connections'))

@vm_bp.route("/vsphere/connections/toggle/<int:conn_id>", methods=['POST'])
def toggle_vsphere_connection(conn_id):
    db_conn = None
    try:
        db_conn = get_db_connection()
        toggle_connection_status(db_conn, conn_id)
        flash("Connection status updated.", "info")
    except Exception as e:
        flash(f"Error updating status: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()
    return redirect(url_for('vm.manage_vsphere_connections'))


# --- API Routes ---

# (1) 依 Environment 取得「Active」主機清單（供前端在 Environment 選定後載入 Host 下拉選單）
@vm_bp.route('/api/vsphere/hosts/<string:environment>')
def get_hosts_by_environment_api(environment):
    db_conn = None
    try:
        db_conn = get_db_connection()
        hosts = get_hosts_by_environment(db_conn, environment, active_only=True) or []
        return jsonify({"environment": environment, "hosts": hosts})
    except Exception as e:
        logging.error(f"Error in get_hosts_by_environment_api for {environment}: {e}")
        return jsonify({"error": "An internal server error occurred"}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

# (2) 依 Host 取得 vSphere 物件（供前端在 Host 選定後載入 Placement/Template/Datastore/Network 等）
@vm_bp.route('/api/vsphere_objects/by_host/<path:host>')
def get_vsphere_objects_by_host_api(host):
    db_conn = None
    try:
        db_conn = get_db_connection()
        conn_info = get_vsphere_connection_by_host(db_conn, host, require_active=True)
        if not conn_info:
            return jsonify({"error": f"Active vSphere connection not found for host: {host}"}), 404

        if conn_info.get('password') is None:
            error_msg = conn_info.get('decrypt_error', 'Password decryption failed.')
            return jsonify({"success": False, "message": error_msg}), 400

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
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

# (3) 保留：VM 連線測試（管理頁使用）
@vm_bp.route("/api/vsphere/connections/test/<int:conn_id>", methods=['POST'])
def test_vsphere_connection_api(conn_id):
    """
    連線測試
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        conn_info = get_vsphere_connection_by_id(db_conn, conn_id) # 假設您有一個用ID查詢的函式

        if not conn_info:
            return jsonify({"success": False, "message": "Connection not found or is inactive."}), 404

        if conn_info.get('password') is None:
            return jsonify({"success": False, "message": conn_info.get('decrypt_error', 'Password decrypt failed.')}), 400

        is_ok, message = test_vsphere_connection(
            host=conn_info['host'],
            user=conn_info['user'],
            password=conn_info['password']
        )

        return jsonify({"success": is_ok, "message": message})

    except Exception as e:
        logging.error(f"Error in test_vsphere_connection_api for conn_id {conn_id}: {e}")
        return jsonify({"success": False, "message": "An internal server error occurred."}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()