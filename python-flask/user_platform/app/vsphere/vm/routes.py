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

    )


@vm_bp.route("/vsphere/overview")
def overview_index():
    """
    Overview：合併 pipeline 與 workflow（含 DRAFT），狀態以 workflow_runs.status 為主。
    """
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

        jira_tickets = get_jira_tickets_and_stats(db_conn) or []
        pipeline_data = get_gitlab_pipeline_detail_and_stats(db_conn) or []
        pipeline_data = [_ensure_created_at(dict(p)) for p in pipeline_data]

        # 取全部 workflow（包含 DRAFT）
        cur = db_conn.cursor(dictionary=True)
        cur.execute("""
            SELECT workflow_id, status, created_at, request_payload
            FROM workflow_runs
            ORDER BY created_at DESC
        """)
        workflows = cur.fetchall()
        cur.close()

        wf_status_map = {w["workflow_id"]: w["status"] for w in workflows}

        # 把 DRAFT 以「類 pipeline 列」補到最上方 & 產 summary
        for w in workflows:
            if w["status"] == "DRAFT":
                draft_row = {
                    "workflow_id": w["workflow_id"],
                    "pipeline_id": None,
                    "status": "draft",
                    "created_at": _to_iso(w.get("created_at")),
                    "finished_at": None,
                    "duration": None,
                }
                pipeline_data.insert(0, draft_row)

                summary = "-"
                try:
                    payload = json.loads(w["request_payload"] or "{}")
                    summary = _generate_create_summary(payload) if payload else "-"
                except Exception:
                    pass

                jira_tickets.append({
                    "workflow_id": w["workflow_id"],
                    "ticket_id": None,
                    "project_key": None,
                    "summary": summary,
                    "description": None,
                    "status": None,
                    "url": None,
                    "created_at": _to_iso(w.get("created_at")),
                })

        # 用 workflow 狀態覆蓋 pipeline 列的顯示狀態
        for row in pipeline_data:
            wid = row.get("workflow_id")
            if wid in wf_status_map:
                row["status"] = wf_status_map[wid]

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


# --- Submit & Approval Workflow（原樣保留＋增強） ---

@vm_bp.route("/vsphere/vm/submit", methods=["POST"])
def vsphere_submit_request():
    """
    統一 Submit 路由。
    - [CHANGED] 支援 workflow_id（從 request.form 或 session['review_workflow_id'] 取得）：
      若有 workflow_id → 從 workflow_runs 讀取 request_payload 作為送單內容；
      若無 → 回退使用 session 內的 create/update 表單資料。
    - [NEW] 若偵測到 from_modal=1，成功後回傳一段 JS 讓「父視窗」跳回 overview，
      避免在 iframe 內 redirect 造成 overview 被載到 modal 裡面。
    """
    # 是否由 modal/iframe 內發起（query 或 form 皆可）
    from_modal = (request.args.get("from_modal") == "1") or (request.form.get("from_modal") == "1")

    # 先嘗試取 workflow_id（表單優先）
    workflow_id = request.form.get("workflow_id")
    if not workflow_id:
        flash("Workflow ID is missing. Cannot submit request.", "danger")
        return redirect(url_for('vm.overview_index'))

    db_conn = None
    try:
        db_conn = get_db_connection()

        # 由 workflow 草稿取得 payload
        cur = db_conn.cursor(dictionary=True)
        cur.execute(
            "SELECT status, request_payload FROM workflow_runs WHERE workflow_id=%s LIMIT 1",
            (workflow_id,)
        )
        wf = cur.fetchone()
        cur.close()
        if not wf:
            flash(f"Workflow #{workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))
            
        form_data = {}
        try:
            # The payload might be nested for update requests
            payload = json.loads(wf.get('request_payload') or "{}")
            if 'new_config' in payload:
                form_data = payload['new_config']
            else:
                form_data = payload
        except (json.JSONDecodeError, TypeError):
            flash("Draft content is invalid. Please check the form again.", "danger")
            return redirect(url_for('vm.overview_index'))

        if not form_data:
            flash("Draft content is empty. Please check the form again.", "danger")
            return redirect(url_for('vm.overview_index'))

        # === Jira ===
        jira_key = create_jira_ticket(form_data)
        ticket_data = get_jira_issue_detail(jira_key)
        insert_jira_info_to_db(db_conn, workflow_id, ticket_data)
        flash(f"Jira ticket {jira_key} created successfully.", "success")

        # === GitLab Pipeline（等待手動批准） ===
        pipeline_data = trigger_gitlab_pipeline(jira_key, form_data)
        if pipeline_data.get("success"):
            insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_data)
            flash(f"Pipeline {pipeline_data['pipeline_id']} has been triggered and is now awaiting approval.", "info")
        else:
            raise Exception(f"Failed to trigger GitLab Pipeline: {pipeline_data.get('error', 'Unknown error')}")

        # 既有/新建 workflow 都統一更新為 IN_PROGRESS
        update_request_status(db_conn, workflow_id, "IN_PROGRESS")

    except Exception as e:
        logging.error(f"An error occurred during the submit process for workflow_id {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to submit request: {e}", "danger")
        return redirect(url_for('vm.overview_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    # === 成功之後的導向：若由 modal 而來，用 JS 讓「父頁」跳回 overview，否則一般 redirect ===
    redirect_url = url_for('vm.overview_index')
    if from_modal:
        # 用 top（或 parent 備援）把父視窗導回 overview，這樣 flash 訊息會顯示在父頁
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
    # 1) vSphere 下拉資料（使用你的 config）
    VCENTER_HOST = current_app.config['VSPHERE_HOST']
    VCENTER_USER = current_app.config['VSPHERE_USER']
    VCENTER_PASSWORD = current_app.config['VSPHERE_PASSWORD']
    vsphere_data = get_vsphere_objects(VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD)

    # 2) 抓 DB
    db_conn = None
    draft_data = {}
    environments = []
    try:
        db_conn = get_db_connection()
        environments = get_environment(db_conn)

        cur = db_conn.cursor(dictionary=True)
        cur.execute("""
            SELECT status, request_payload
            FROM workflow_runs
            WHERE workflow_id=%s
        """, (workflow_id,))
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

    # 3) 關鍵：改成渲染 vm_index.html（它 extends base），
    #    裡面會 include create/form.html（partial），所以不會雙 sidebar。
    return render_template(
        "vm_index.html",
        datacenters=vsphere_data["datacenters"], clusters=vsphere_data["clusters"],
        templates=vsphere_data["templates"], networks=vsphere_data["networks"],
        datastores=vsphere_data["datastores"], vm_name=vsphere_data["vm_name"],
        environment=environments,
        draft_data=draft_data,
        workflow_id=workflow_id,
        # 若 vm_index.html 有分頁/Tab，這裡可選擇預設到 Create 分頁
        active_tab="create"
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