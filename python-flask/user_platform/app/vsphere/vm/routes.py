# app/vsphere/vm/routes.py

from flask import render_template, request, redirect, url_for, jsonify, flash
import traceback
import json
import logging
from datetime import datetime

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

# 可保留 import（若你後續在 workflow_manager 中擴充邏輯可替換成那邊的 Helper）
# from .db.workflow_manager import record_pending_request, update_request_status, cancel_request, apply_request_to_db

# --- API 函式 ---
from .vsphere_api.get_vsphere_objects import get_vsphere_objects
from .jira_api.create_jira_ticket import create_jira_ticket
from .jira_api.get_jira_issue_detail import get_jira_issue_detail
from .gitlab_api.trigger_gitlab_pipeline import trigger_gitlab_pipeline
from .gitlab_api.run_manual_job import run_manual_job


# ========== Utilities ==========

def _current_username() -> str:
    """
    取得目前登入者識別（只作為身分 / 審計使用；不再用 session 保存表單）。
    依你的認證機制調整：例如 flask-login 的 current_user.username 或反向代理 header。
    """
    try:
        from flask import session  # 僅用於讀取登入者，不用來保存表單資料
        if session.get("username"):
            return str(session["username"])
    except Exception:
        pass
    return request.headers.get("X-User") or request.headers.get("X-Username") or "unknown"


def _flatten_form(form):
    """
    將 request.form 轉為「可 JSON 序列化」的扁平 dict：
      - 單值欄位 -> 字串
      - 多值欄位（name[]）-> list
    """
    raw = form.to_dict(flat=False)
    out = {}
    for k, v in raw.items():
        if k.endswith('[]'):
            out[k] = v
        else:
            out[k] = v[0] if isinstance(v, list) and v else v
    return out


def _json_loads(value):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _fetch_workflow(db_conn, workflow_id: int):
    cur = db_conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
    row = cur.fetchone()
    cur.close()
    return row


def _ensure_owner_or_404(db_conn, workflow_id: int, username: str):
    wf = _fetch_workflow(db_conn, workflow_id)
    if not wf:
        return None, ("Workflow not found.", 404)
    if wf.get("created_by") != username:
        return None, ("Permission denied for this workflow.", 403)
    return wf, None


# ========== Views ==========

@vm_bp.route("/vsphere/vm")
def vm_index():
    """
    Render the main VM management page.
    不再傳遞 session_data；表單送出後會直接寫入 workflow_runs.request_payload。
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
        environment=environment
    )


@vm_bp.route("/vsphere/overview")
def overview_index():
    """
    Render the overview page with all requests and their statuses.
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        jira_tickets = get_jira_tickets_and_stats(db_conn)
        pipeline_data = get_gitlab_pipeline_detail_and_stats(db_conn)
    except Exception as e:
        logging.error(f"Database error in overview_index: {e}")
        jira_tickets = []
        pipeline_data = []
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return render_template("overview_index.html", jira_tickets=jira_tickets, pipeline_data=pipeline_data)


# ========== Public APIs（Read-Only） ==========

@vm_bp.route('/api/get_vms_by_environment/<string:environment>')
def get_vms_by_environment_api(environment):
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


# ========== Draft：Create / Update（不再用 session，改寫入 DB JSON） ==========

@vm_bp.route("/vsphere/vm/create/review", methods=["POST"])
def vsphere_create_vm_review():
    """
    [改版] Create 表單的 Review：
    - 直接將表單內容寫入 workflow_runs.request_payload（status=DRAFT）
    - 回傳 review 頁面時，從 DB 讀 payload 顯示（不使用 session）
    """
    username = _current_username()
    form_payload = _flatten_form(request.form)

    db_conn = None
    try:
        db_conn = get_db_connection()
        cur = db_conn.cursor()
        # 新增 DRAFT workflow
        cur.execute("""
            INSERT INTO workflow_runs (created_by, status, request_payload)
            VALUES (%s, 'DRAFT', %s)
        """, (username, json.dumps(form_payload)))
        db_conn.commit()
        workflow_id = cur.lastrowid
        cur.close()

        # 供 review.html 使用
        return render_template("create/review.html", data=form_payload, workflow_id=workflow_id)

    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Error in vsphere_create_vm_review: {e}")
        traceback.print_exc()
        flash(f"Failed to prepare review: {e}", "danger")
        return redirect(url_for('vm.vm_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route("/vsphere/vm/update/review", methods=["POST"])
def vsphere_update_vm_review():
    """
    [改版] Update 表單的 Review：
    - 讀取原設定（original_config），組合 new_config
    - 將兩份資訊（尤其 new_config）寫入 workflow_runs.request_payload（status=DRAFT）
    - review 頁面直接從 payload 顯示
    """
    username = _current_username()
    new_config = _flatten_form(request.form)

    env = new_config.get('environment')
    prefix = new_config.get('vm_name_prefix')

    db_conn = None
    try:
        db_conn = get_db_connection()
        original_config = get_vm_config(db_conn, env, prefix) or {}
        payload = {
            "original_config": original_config,
            "new_config": new_config
        }

        cur = db_conn.cursor()
        cur.execute("""
            INSERT INTO workflow_runs (created_by, status, request_payload)
            VALUES (%s, 'DRAFT', %s)
        """, (username, json.dumps(payload)))
        db_conn.commit()
        workflow_id = cur.lastrowid
        cur.close()

        return render_template("update/review.html", data=payload, workflow_id=workflow_id)

    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Error in vsphere_update_vm_review: {e}")
        traceback.print_exc()
        flash(f"Failed to prepare update review: {e}", "danger")
        return redirect(url_for('vm.vm_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route("/vsphere/vm/request/<int:workflow_id>/draft/edit", methods=["POST"])
def vsphere_edit_draft(workflow_id: int):
    """
    [新增] 編輯既有 DRAFT（前端 Edit modal 提交）
    body: JSON { payload: {...} }
    """
    username = _current_username()
    body = request.get_json(silent=True) or {}
    payload = body.get("payload") or {}

    db_conn = None
    try:
        db_conn = get_db_connection()
        wf, err = _ensure_owner_or_404(db_conn, workflow_id, username)
        if err:
            msg, code = err
            return jsonify({"error": msg}), code

        if wf["status"] != "DRAFT":
            return jsonify({"error": "Only DRAFT can be edited."}), 409

        cur = db_conn.cursor()
        cur.execute("""
            UPDATE workflow_runs
               SET request_payload = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE workflow_id = %s
        """, (json.dumps(payload), workflow_id))
        db_conn.commit()
        cur.close()
        return jsonify({"workflow_id": workflow_id, "status": "DRAFT", "message": "draft updated"})
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Error in vsphere_edit_draft: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route("/vsphere/vm/request/<int:workflow_id>", methods=["GET"])
def get_request(workflow_id: int):
    """
    [新增] 取得單一 workflow 的詳細資料（含 payload）。
    供 Edit / Review modal 預載資料使用。
    """
    username = _current_username()
    db_conn = None
    try:
        db_conn = get_db_connection()
        wf, err = _ensure_owner_or_404(db_conn, workflow_id, username)
        if err:
            msg, code = err
            return jsonify({"error": msg}), code

        wf["request_payload"] = _json_loads(wf.get("request_payload"))
        return jsonify(wf)
    except Exception as e:
        logging.error(f"Error in get_request: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


# ========== Submit / Cancel / Return / Approve ==========

@vm_bp.route("/vsphere/vm/submit", methods=["POST"])
def vsphere_submit_request():
    """
    [改版] Submit：DRAFT -> PENDING_APPROVAL
    前端需送 workflow_id（而非從 session 取資料）
    然後建立 Jira、觸發 GitLab pipeline（manual job 等待批准），再更新狀態。
    """
    username = _current_username()
    workflow_id = request.form.get("workflow_id") or (request.get_json(silent=True) or {}).get("workflow_id")
    if not workflow_id:
        flash("Missing workflow_id", "warning")
        return redirect(url_for('vm.overview_index'))

    workflow_id = int(workflow_id)

    db_conn = None
    try:
        db_conn = get_db_connection()
        wf, err = _ensure_owner_or_404(db_conn, workflow_id, username)
        if err:
            msg, code = err
            flash(msg, "danger")
            return redirect(url_for('vm.overview_index'))

        if wf["status"] != "DRAFT":
            flash("Only DRAFT can be submitted.", "warning")
            return redirect(url_for('vm.overview_index'))

        payload = _json_loads(wf.get("request_payload")) or {}

        # 1. 建立 Jira
        jira_key = create_jira_ticket(payload)
        ticket_data = get_jira_issue_detail(jira_key)

        # 2. 寫入 Jira 到 DB
        insert_jira_info_to_db(db_conn, workflow_id, ticket_data)
        flash(f"Jira ticket {jira_key} created successfully.", "success")

        # 3. 觸發 GitLab Pipeline（等待手動批准）
        pipeline_result = trigger_gitlab_pipeline(jira_key, payload)
        if pipeline_result.get("success"):
            insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_result, payload)
            flash(f"Pipeline {pipeline_result.get('pipeline_id')} has been triggered and is now awaiting approval.", "info")
        else:
            raise Exception(f"Failed to trigger GitLab Pipeline: {pipeline_result.get('error', 'Unknown error')}")

        # 4. 更新狀態：PENDING_APPROVAL
        cur = db_conn.cursor()
        cur.execute("""
            UPDATE workflow_runs
               SET status = 'PENDING_APPROVAL',
                   approved_by = NULL,
                   approved_at = NULL,
                   cancelled_by = NULL,
                   cancelled_at = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE workflow_id = %s AND status = 'DRAFT'
        """, (workflow_id,))
        if cur.rowcount != 1:
            db_conn.rollback()
            flash("Submit conflict or invalid state.", "danger")
            return redirect(url_for('vm.overview_index'))
        db_conn.commit()
        cur.close()

    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"An error occurred during the submit process for workflow_id {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to submit request: {e}", "danger")
        return redirect(url_for('vm.vm_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


@vm_bp.route("/workflow/approve/<int:workflow_id>", methods=["GET"])
def workflow_approve_page(workflow_id):
    """
    顯示審批頁，資料從 workflow_runs.request_payload 讀取（不使用 session）
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        workflow = _fetch_workflow(db_conn, workflow_id)
        if not workflow:
            flash(f"Workflow ID {workflow_id} not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        jira_ticket = get_jira_ticket_by_workflow_id(db_conn, workflow_id)
        request_details = _json_loads(workflow.get('request_payload')) or {}

        return render_template("create/approve.html",
                               workflow=workflow,
                               pipeline=pipeline,
                               jira_ticket=jira_ticket,
                               request_details=request_details)
    except Exception as e:
        logging.error(f"Error loading approval page: {e}")
        flash(f"Error loading approval page: {e}", "danger")
        return redirect(url_for('vm.overview_index'))
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()


@vm_bp.route('/workflow/execute/<int:workflow_id>', methods=['POST'])
def workflow_execute(workflow_id):
    """
    Approve & Execute：
    - 僅 PENDING_APPROVAL 可執行
    - 寫入 approved_by/approved_at，狀態改 IN_PROGRESS
    - （在此或之後）將 payload 正式落盤到 vm_configurations / vm_disks
    - 解除 GitLab Pipeline manual job
    """
    username = _current_username()
    db_conn = None
    try:
        db_conn = get_db_connection()
        wf = _fetch_workflow(db_conn, workflow_id)
        if not wf:
            flash("Workflow not found.", "danger")
            return redirect(url_for('vm.overview_index'))

        if wf["status"] != "PENDING_APPROVAL":
            flash("Only PENDING_APPROVAL can be approved & executed.", "warning")
            return redirect(url_for('vm.overview_index'))

        payload = _json_loads(wf.get("request_payload")) or {}

        # 1) 更新 workflow 為 IN_PROGRESS + 記錄 approved_by/approved_at
        cur = db_conn.cursor()
        cur.execute("""
            UPDATE workflow_runs
               SET status = 'IN_PROGRESS',
                   approved_by = %s,
                   approved_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE workflow_id = %s AND status = 'PENDING_APPROVAL'
        """, (username, workflow_id))
        if cur.rowcount != 1:
            db_conn.rollback()
            flash("Approve conflict or invalid state.", "danger")
            return redirect(url_for('vm.overview_index'))
        db_conn.commit()
        cur.close()

        # 2) 正式落盤 payload（依你的 schema 套用）
        #    你已有 apply_request_to_db，可在此呼叫；這裡示意直接寫：
        # from .somewhere import apply_payload_to_vm_tables
        # cur = db_conn.cursor()
        # apply_payload_to_vm_tables(cur, payload)
        # db_conn.commit()
        # cur.close()

        # 3) 找到對應 pipeline -> 解鎖 manual job
        pipeline = get_pipeline_details_by_workflow_id(db_conn, workflow_id)
        if not pipeline or not pipeline.get('pipeline_id'):
            raise Exception("Could not find the associated pipeline to execute.")

        result = run_manual_job(pipeline['pipeline_id'])
        if not result.get("success"):
            raise Exception(f"Failed to trigger manual job: {result.get('error')}")

        flash(f"Request {workflow_id} approved. Pipeline is now running.", "success")

    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Failed to execute workflow {workflow_id}: {e}")
        traceback.print_exc()
        flash(f"Failed to approve request: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


@vm_bp.route("/workflow/cancel/<int:workflow_id>", methods=["POST"])
def workflow_cancel(workflow_id: int):
    """
    僅 PENDING_APPROVAL 可取消 -> CANCELLED（前端 Action：Cancel）
    """
    username = _current_username()
    db_conn = None
    try:
        db_conn = get_db_connection()
        wf, err = _ensure_owner_or_404(db_conn, workflow_id, username)
        if err:
            msg, code = err
            flash(msg, "danger")
            return redirect(url_for('vm.overview_index'))

        if wf["status"] != "PENDING_APPROVAL":
            flash("Only PENDING_APPROVAL can be cancelled.", "warning")
            return redirect(url_for('vm.overview_index'))

        cur = db_conn.cursor()
        cur.execute("""
            UPDATE workflow_runs
               SET status = 'CANCELLED',
                   cancelled_by = %s,
                   cancelled_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE workflow_id = %s AND status = 'PENDING_APPROVAL'
        """, (username, workflow_id))
        db_conn.commit()
        cur.close()
        flash("Request cancelled.", "info")
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Cancel error: {e}")
        flash(f"Cancel error: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


@vm_bp.route("/workflow/return/<int:workflow_id>", methods=["POST"])
def workflow_return(workflow_id: int):
    """
    退回作廢 -> RETURNED（你定義：作廢且不可再次提交）
    通常由審批者操作；若需權限控管，請在此加入判斷。
    """
    db_conn = None
    try:
        db_conn = get_db_connection()
        cur = db_conn.cursor()
        cur.execute("""
            UPDATE workflow_runs
               SET status = 'RETURNED',
                   updated_at = CURRENT_TIMESTAMP
             WHERE workflow_id = %s AND status IN ('PENDING_APPROVAL')
        """, (workflow_id,))
        if cur.rowcount != 1:
            db_conn.rollback()
            flash("Return conflict or invalid state.", "danger")
            return redirect(url_for('vm.overview_index'))
        db_conn.commit()
        cur.close()
        flash("Request returned.", "info")
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Return error: {e}")
        flash(f"Return error: {e}", "danger")
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()

    return redirect(url_for('vm.overview_index'))


# ========== Webhook（可選）：GitLab 回呼更新狀態 ==========

@vm_bp.route("/webhook/gitlab", methods=["POST"])
def gitlab_webhook():
    """
    根據你現有 webhook 設計對應更新：
    - 當 pipeline 完成：IN_PROGRESS -> SUCCESS / FAILED / CANCELLED
    - 同步寫入 gitlab_pipelines（若包含 job/pipeline 資訊）
    """
    body = request.get_json(silent=True) or {}
    workflow_id = body.get("workflow_id")
    pipeline_status = (body.get("status") or "").upper()

    if not workflow_id:
        return jsonify({"error": "workflow_id required"}), 400

    next_status = None
    if pipeline_status in ("SUCCESS", "PASSED"):
        next_status = "SUCCESS"
    elif pipeline_status in ("FAILED",):
        next_status = "FAILED"
    elif pipeline_status in ("CANCELED", "CANCELLED"):
        next_status = "CANCELLED"

    if not next_status:
        return jsonify({"ignored": True})

    db_conn = None
    try:
        db_conn = get_db_connection()
        cur = db_conn.cursor()
        cur.execute("""
            UPDATE workflow_runs
               SET status = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE workflow_id = %s AND status = 'IN_PROGRESS'
        """, (next_status, workflow_id))
        db_conn.commit()
        cur.close()
        return jsonify({"workflow_id": workflow_id, "status": next_status})
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"Webhook error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if db_conn and db_conn.is_connected():
            db_conn.close()