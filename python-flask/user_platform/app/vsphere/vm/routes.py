from flask import render_template, request, redirect, session, flash, jsonify
# from flask_login import login_required
# from app.vsphere.vm import vm_bp  # 導入 Blueprint 實例
from . import vm_bp  # 導入 Blueprint 實例

# ----------Manage VM page & Call vSphere API ----------
from .vsphere_api.get_vsphere_objects import get_vsphere_objects
from app.mysql.db import get_db_connection
from .db.get_vm_configurations import get_environment, get_vms_by_environment, get_vm_config

@vm_bp.route("/vsphere/vm")
# @login_required
def vm_index():
    VCENTER_HOST = "172.26.1.60"
    VCENTER_USER = "administrator@vsphere.local"
    VCENTER_PASSWORD = "Gict@1688+"

    vsphere_data = get_vsphere_objects(VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD)
    db_conn = get_db_connection()

    try:
        environment = get_environment(db_conn)
    except Exception as e:
        print(f"Database connection error: {e}")
        environment = []
    finally:
        if db_conn:
            db_conn.close()

    return render_template(
        "vm_index.html",
        datacenters=vsphere_data["datacenters"],
        clusters=vsphere_data["clusters"],
        templates=vsphere_data["templates"],
        networks=vsphere_data["networks"],
        datastores=vsphere_data["datastores"],
        vm_name=vsphere_data["vm_name"],
        environment=environment
    )

# ----------Overview page----------
from .db.get_jira_tickets_and_stats import get_jira_tickets_and_stats
from .db.get_gitlab_pipeline_detail_and_stats import get_gitlab_pipeline_detail_and_stats

@vm_bp.route("/vsphere/overview")
def overview_index():
    db_conn = get_db_connection()
    try:
        jira_tickets = get_jira_tickets_and_stats(db_conn)
        pipeline_data = get_gitlab_pipeline_detail_and_stats(db_conn)
    finally:
        db_conn.close()

    return render_template("overview_index.html", jira_tickets=jira_tickets, pipeline_data=pipeline_data)

# ----------Create VM and Review ----------
@vm_bp.route("/vsphere/vm/create/review", methods=["POST"])
def vsphere_create_vm_review():
    """
    Create 表單 Review：
    - 一般欄位用 to_dict()
    - 磁碟陣列同時存「有中括號」與「無中括號」兩份，避免模板鍵名不一致造成還原失敗
    - 追加 SCSI hidden 欄位：create_vm_disk_scsi_controller[] / create_vm_disk_unit_number[]
    - 追加總控制器數：create_vm_scsi_controller_count
    - 設定 form_scope=create，並清除 update 的 session
    """
    form_data = request.form.to_dict()

    # ---- Disk arrays（尺寸/佈署方式/旗標）----
    sizes = request.form.getlist('create_vm_disk_size[]')
    if sizes:
        form_data['create_vm_disk_size[]'] = sizes
        form_data['create_vm_disk_size'] = sizes  # 給 form/review 模板都好處理

    provs = request.form.getlist('create_vm_disk_provisioning[]')
    if provs:
        form_data['create_vm_disk_provisioning[]'] = provs
        form_data['create_vm_disk_provisioning'] = provs

    thins = request.form.getlist('create_vm_disk_thin_provisioned[]')
    if thins:
        form_data['create_vm_disk_thin_provisioned[]'] = thins
        form_data['create_vm_disk_thin_provisioned'] = thins

    scrubs = request.form.getlist('create_vm_disk_eagerly_scrub[]')
    if scrubs:
        form_data['create_vm_disk_eagerly_scrub[]'] = scrubs
        form_data['create_vm_disk_eagerly_scrub'] = scrubs

    # ---- 關鍵：SCSI hidden 欄位 + 控制器數（由前端 JS reassignScsiSlots 計算）----
    scsi_bus = request.form.getlist('create_vm_disk_scsi_controller[]')  # e.g. ["0","0","1",...]
    if scsi_bus:
        form_data['create_vm_disk_scsi_controller[]'] = scsi_bus
        form_data['create_vm_disk_scsi_controller'] = scsi_bus

    unit_nums = request.form.getlist('create_vm_disk_unit_number[]')     # e.g. ["1","2","8",...]
    if unit_nums:
        form_data['create_vm_disk_unit_number[]'] = unit_nums
        form_data['create_vm_disk_unit_number'] = unit_nums

    controller_count = request.form.get('create_vm_scsi_controller_count', '1')
    form_data['create_vm_scsi_controller_count'] = controller_count

    session["form_scope"] = "create"
    session["create_vm_form_data"] = form_data
    session.pop("vm_update_form_data", None)

    return render_template("create/review.html", data=form_data)

# ----------Update VM and Review ----------

# 根據 update/form.html 選擇的 environment 獲取 vm_name_prefix 列表
@vm_bp.route('/api/get_vms_by_environment/<string:environment>')
def get_vms_by_environment_api(environment):
    """API: 根據 environment 獲取 vm_name_prefix 列表。"""
    db_conn = get_db_connection()
    try:
        vms = get_vms_by_environment(db_conn, environment)
        return jsonify(vms)
    except Exception as e:
        print(f"Error in get_vms_by_environment_api: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        db_conn.close()

# 根據 update/form.html 選擇的 environment 和 vm_name_prefix 獲取詳細 VM 設定
@vm_bp.route('/api/get_vm_config/<string:environment>/<string:vm_name_prefix>')
def get_vm_config_api(environment, vm_name_prefix):
    db_conn = get_db_connection()
    try:
        config = get_vm_config(db_conn, environment, vm_name_prefix)
        if not config:
            return jsonify({"error": "Configuration not found"}), 404
        return jsonify(config)
    except Exception as e:
        print(f"Error in get_vm_config_api: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        db_conn.close()

@vm_bp.route("/vsphere/vm/update/review", methods=["POST"])
def vsphere_update_vm_review():
    """
    Update 表單 Review
    - 將磁碟欄位用 update_vm_disk_*[] / update_disk_*[] 存進 session
    - 讀取 original_config
    - 設定 form_scope=update，並清掉 create 的 session
    """
    new_config = request.form.to_dict()

    # === 關鍵：所有多值欄位一律 getlist()，並統一 update_vm_*[] / update_disk_*[] ===
    new_config['update_vm_disk_size[]'] = request.form.getlist('update_vm_disk_size[]') or request.form.getlist('vm_disk_size[]')
    new_config['update_vm_disk_provisioning[]'] = request.form.getlist('update_vm_disk_provisioning[]') or request.form.getlist('vm_disk_provisioning[]')
    new_config['update_vm_disk_thin_provisioned[]'] = request.form.getlist('update_vm_disk_thin_provisioned[]') or request.form.getlist('vm_disk_thin_provisioned[]')
    new_config['update_vm_disk_eagerly_scrub[]'] = request.form.getlist('update_vm_disk_eagerly_scrub[]') or request.form.getlist('vm_disk_eagerly_scrub[]')

    # 這兩個是保持硬碟 identity 與 label 穩定的關鍵
    new_config['update_disk_db_id[]'] = request.form.getlist('update_disk_db_id[]') or request.form.getlist('disk_db_id[]')
    new_config['update_disk_label[]'] = request.form.getlist('update_disk_label[]')  # 一定要用 getlist()

    # （新增）若表單有帶 SCSI 欄位，保留進 session，供 review 顯示或後端參考；沒有就忽略
    upd_buses = request.form.getlist('update_scsi_controller[]')
    if upd_buses:
        new_config['update_scsi_controller[]'] = upd_buses
    upd_units = request.form.getlist('update_unit_number[]')
    if upd_units:
        new_config['update_unit_number[]'] = upd_units

    env = new_config.get('environment')
    prefix = new_config.get('vm_name_prefix')

    original_config = {}
    db_conn = get_db_connection()
    try:
        original_config = get_vm_config(db_conn, env, prefix)
    finally:
        db_conn.close()

    session['form_scope'] = 'update'
    session['vm_update_form_data'] = {
        'original_config': original_config,
        'new_config': new_config
    }
    session.pop('create_vm_form_data', None)

    # Debug：可先看看 keys
    print("[DEBUG] vm_update_form_data.new_config keys:", list(new_config.keys()))

    return render_template("update/review.html", data=session["vm_update_form_data"])

# ----------Cancel ----------
@vm_bp.route("/vsphere/vm/cancel")
# @login_required
def vsphere_cancel_vm_form():
    # 清除兩邊的 session，避免殘留
    session.pop("create_vm_form_data", None)
    session.pop("vm_update_form_data", None)
    session.pop("form_scope", None)
    # return redirect("/")
    return redirect("/vsphere/overview")


# ----------Submit to Jira & trigger gitlab ci----------
from .jira_api.create_jira_ticket import create_jira_ticket
from .jira_api.update_jira_custom_fields import update_jira_custom_fields
from .jira_api.get_jira_issue_detail import get_jira_issue_detail
from .gitlab_api.trigger_gitlab_pipeline import trigger_gitlab_pipeline
from .db.insert_workflow_run_to_db import insert_workflow_run_to_db
from .db.insert_jira_info_to_db import insert_jira_info_to_db
from .db.insert_gitlab_pipeline_info_to_db import insert_gitlab_pipeline_info_to_db
from .db.insert_vm_configuration_to_db import insert_vm_configuration_to_db

import datetime
import traceback

# --- helper for Jira and Gitlab ---
def _handle_jira_and_gitlab_workflow(db_conn, workflow_id, submission_data, jira_ticket_data):
    """
    共用：建立 Jira 工單 + 觸發 GitLab pipeline + 寫 DB
    """
    # 建立 Jira ticket
    jira_key = create_jira_ticket(jira_ticket_data)
    if not jira_key:
        flash("Failed to create Jira ticket", "danger")
        return

    flash(f"Jira ticket created: {jira_key}", "success")

    # 更新 Jira 自訂欄位
    try:
        status_code, response_text = update_jira_custom_fields(jira_key, submission_data)
        print(f"Custom fields updated: {status_code} - {response_text}")
    except Exception as e:
        flash(f"Failed to update custom fields: {str(e)}", "warning")

    # 檢查 Jira 單號狀態並存入 DB
    ticket_data = get_jira_issue_detail(jira_key, fields=["project", "summary", "description", "status"])
    if ticket_data:
        insert_jira_info_to_db(db_conn, workflow_id, ticket_data)
    else:
        flash("Failed to fetch Jira detail for DB insert", "warning")

    # 觸發 GitLab CI pipeline
    try:
        pipeline_result = trigger_gitlab_pipeline(jira_key, submission_data)
        if pipeline_result.get("success"):
            flash(f"GitLab pipeline triggered successfully: {pipeline_result['pipeline_id']}", "success")
            # 將 GitLab pipeline 資訊存入 DB
            try:
                insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_result, submission_data)
                print(f"GitLab pipeline info saved to DB: {pipeline_result['pipeline_id']}")
            except Exception as db_error:
                flash(f"Failed to save pipeline info to DB: {str(db_error)}", "warning")
                print(f"DB Error: {db_error}")
        else:
            flash(f"Failed to trigger GitLab pipeline: {pipeline_result.get('error', 'Unknown error')}", "danger")
            print(f"Pipeline trigger failed: {pipeline_result}")
    except Exception as pipeline_error:
        flash(f"Error triggering GitLab pipeline: {str(pipeline_error)}", "danger")
        print(f"Pipeline Error: {pipeline_error}")

# --- Create VM ---
@vm_bp.route("/vsphere/vm/create/submit", methods=["POST"])
def vsphere_create_vm_submit():
    # 確認 scope，避免拿到 update 的 session
    if session.get("form_scope") != "create":
        flash("Form scope mismatch. Please review the Create form again.", "warning")
        return redirect("/vsphere/overview")

    create_vm_form_data = session.get("create_vm_form_data")
    if not create_vm_form_data:
        flash("No form data found in session. Please start over.", "danger")
        return redirect("/vsphere/overview")

    try:
        db_conn = get_db_connection()
        workflow_id = insert_workflow_run_to_db(db_conn, triggered_by="webform_create")

        # === 標準化 create 的磁碟鍵名 -> DB 期望的通用鍵名 vm_disk_*[] ===
        normalized_for_db = create_vm_form_data.copy()

        if 'create_vm_disk_size[]' in normalized_for_db:
            normalized_for_db['vm_disk_size[]'] = normalized_for_db.pop('create_vm_disk_size[]')
        if 'create_vm_disk_provisioning[]' in normalized_for_db:
            normalized_for_db['vm_disk_provisioning[]'] = normalized_for_db.pop('create_vm_disk_provisioning[]')
        if 'create_vm_disk_thin_provisioned[]' in normalized_for_db:
            normalized_for_db['vm_disk_thin_provisioned[]'] = normalized_for_db.pop('create_vm_disk_thin_provisioned[]')
        if 'create_vm_disk_eagerly_scrub[]' in normalized_for_db:
            normalized_for_db['vm_disk_eagerly_scrub[]'] = normalized_for_db.pop('create_vm_disk_eagerly_scrub[]')

        # === 關鍵：SCSI hidden 欄位映射成通用鍵名 ===
        if 'create_vm_disk_scsi_controller[]' in normalized_for_db:
            normalized_for_db['vm_disk_scsi_controller[]'] = normalized_for_db.pop('create_vm_disk_scsi_controller[]')
        if 'create_vm_disk_unit_number[]' in normalized_for_db:
            normalized_for_db['vm_disk_unit_number[]'] = normalized_for_db.pop('create_vm_disk_unit_number[]')

        # === 控制器總數：寫到 vm_configurations（欄位 vm_scsi_controller_count） ===
        if 'create_vm_scsi_controller_count' in normalized_for_db:
            normalized_for_db['vm_scsi_controller_count'] = normalized_for_db.pop('create_vm_scsi_controller_count')

        # 傳遞標準化資料給 DB 函式（它會處理主表和磁碟同步）
        insert_vm_configuration_to_db(db_conn, normalized_for_db)

        # 觸發 pipeline 前，重新查詢以獲取包含 additional_disks 的最新資料
        reloaded_config = get_vm_config(db_conn, create_vm_form_data.get('environment'), create_vm_form_data.get('vm_name_prefix'))
        reloaded_config['action_type'] = create_vm_form_data.get('action_type', 'create')

        # 建議：把 vm_scsi_controller_count 一併帶給 CI（insert_gitlab_pipeline_info_to_db 已有支援自動推算；但有值更好）
        if 'vm_scsi_controller_count' not in reloaded_config and 'vm_scsi_controller_count' in normalized_for_db:
            reloaded_config['vm_scsi_controller_count'] = normalized_for_db['vm_scsi_controller_count']

        _handle_jira_and_gitlab_workflow(db_conn, workflow_id, reloaded_config, reloaded_config)

    except Exception as e:
        flash(f"An unexpected error occurred: {e}", "danger")
        traceback.print_exc()
    finally:
        if 'db_conn' in locals() and db_conn.is_connected():
            db_conn.close()

    # 清除自己的 session
    session.pop("create_vm_form_data", None)
    session.pop("form_scope", None)
    return redirect("/vsphere/overview")

# --- Update VM ---
@vm_bp.route("/vsphere/vm/update/submit", methods=["POST"])
def vsphere_update_vm_submit():
    # 確認 scope，避免拿到 create 的 session
    if session.get("form_scope") != "update":
        flash("Form scope mismatch. Please review the Update form again.", "warning")
        return redirect("/vsphere/overview")

    # 1. 從 update 專用的 session key 獲取資料
    vm_update_form_data = session.get("vm_update_form_data")
    if not vm_update_form_data:
        flash("No update data found in session. Please start over.", "danger")
        return redirect("/vsphere/overview")

    new_values = vm_update_form_data.get("new_config", {})
    env = new_values.get('environment')
    prefix = new_values.get('vm_name_prefix')

    if not env or not prefix:
        flash("Environment or VM Name Prefix is missing in the update data.", "danger")
        return redirect("/vsphere/overview")

    try:
        db_conn = get_db_connection()
        fresh_original_config = get_vm_config(db_conn, env, prefix)

        if not fresh_original_config:
            flash(f"Could not find original configuration for VM '{prefix}' in environment '{env}'.", "danger")
            db_conn.close()
            return redirect("/vsphere/overview")

        # 合併原始配置與新值
        final_config = fresh_original_config.copy()
        final_config.update(new_values)
        final_config['environment'] = env
        final_config['vm_name_prefix'] = prefix

        # === 標準化 update 的磁碟鍵名 -> DB 期望的通用鍵名 vm_disk_*[] ===
        if 'update_vm_disk_size[]' in final_config:
            final_config['vm_disk_size[]'] = final_config.pop('update_vm_disk_size[]')
        if 'update_vm_disk_provisioning[]' in final_config:
            final_config['vm_disk_provisioning[]'] = final_config.pop('update_vm_disk_provisioning[]')
        if 'update_vm_disk_thin_provisioned[]' in final_config:
            final_config['vm_disk_thin_provisioned[]'] = final_config.pop('update_vm_disk_thin_provisioned[]')
        if 'update_vm_disk_eagerly_scrub[]' in final_config:
            final_config['vm_disk_eagerly_scrub[]'] = final_config.pop('update_vm_disk_eagerly_scrub[]')
        if 'update_disk_db_id[]' in final_config:
            final_config['disk_db_id[]'] = final_config.pop('update_disk_db_id[]')

        # （新增）若表單有帶 SCSI 欄位就原樣轉交；沒有則讓後端 allocator 依 DB 自動分配
        if 'update_scsi_controller[]' in final_config:
            final_config['scsi_controller[]'] = final_config.pop('update_scsi_controller[]')
        if 'update_unit_number[]' in final_config:
            final_config['unit_number[]'] = final_config.pop('update_unit_number[]')

        # 序列化 datetime 物件（保險）
        for key, value in list(final_config.items()):
            if isinstance(value, datetime.datetime):
                final_config[key] = value.isoformat()

        # 先依照使用者提交內容同步 DB
        workflow_id = insert_workflow_run_to_db(db_conn, triggered_by="webform_update")
        insert_vm_configuration_to_db(db_conn, final_config)

        # 再重新從資料庫讀取最終配置（包含最新磁碟清單），用於觸發 pipeline
        final_config_with_disks = get_vm_config(db_conn, env, prefix)
        final_config_with_disks['action_type'] = new_values.get('action_type', 'update')

        # # 若 DB 有 vm_scsi_controller_count，一併傳給 CI（否則 trigger 會自行推算）
        # if 'vm_scsi_controller_count' in fresh_original_config:
        #     final_config_with_disks['vm_scsi_controller_count'] = fresh_original_config['vm_scsi_controller_count']

        # 以「更新後重撈」的值為準；若 DB 還沒寫到就做保守推估
        scsi_cnt = final_config_with_disks.get('vm_scsi_controller_count')
        if not scsi_cnt:
            add_disks = final_config_with_disks.get('additional_disks') or []
            # 只算「非系統碟」數量
            n = sum(1 for d in add_disks
                    if isinstance(d, dict) and not (str(d.get('scsi_controller', 0)) == '0' and str(d.get('unit_number', '')) == '0'))
            # 每顆控制器有效 slot = 14（1..15 跳 7）
            import math
            scsi_cnt = max(1, min(4, math.ceil(n / 14)))
            # 若有人用到 unit 15，provider 在 count=1 時會抱怨 → 至少 2
            if any(str(d.get('unit_number')) == '15' for d in add_disks if isinstance(d, dict)):
                scsi_cnt = max(scsi_cnt, 2)
            final_config_with_disks['vm_scsi_controller_count'] = scsi_cnt


        _handle_jira_and_gitlab_workflow(db_conn, workflow_id, final_config_with_disks, vm_update_form_data)

    except Exception as e:
        flash(f"An unexpected error occurred: {e}", "danger")
        print(f"General Error in Update Submit: {e}")
        traceback.print_exc()
    finally:
        if 'db_conn' in locals() and db_conn.is_connected():
            db_conn.close()

    # 清除自己的 session
    session.pop("vm_update_form_data", None)
    session.pop("form_scope", None)
    return redirect("/vsphere/overview")

# ----------run_manual_job----------
from .gitlab_api.run_manual_job import run_manual_job
from .db.get_jira_tickets_and_stats import get_jira_ticket_by_pipeline_id
from .db.get_gitlab_pipeline_detail_and_stats import get_pipeline_details_by_id

# 顯示 pipeline 的詳細資訊供 Approve page
@vm_bp.route('/pipeline-approve/<int:pipeline_id>', methods=['GET'])
def pipeline_approve(pipeline_id):
    try:
        db_conn = get_db_connection()  # 你的資料庫連接函數

        # 從 DB 獲取 pipeline 資料
        pipeline_data = get_pipeline_details_by_id(db_conn, pipeline_id)
        if not pipeline_data:
            flash("❌ Pipeline not found", "danger")
            return redirect("/vsphere/overview")

        # 從 DB 獲取對應的 Jira 資料
        jira_ticket = get_jira_ticket_by_pipeline_id(db_conn, pipeline_id)

        db_conn.close()

        return render_template("create/approve.html", pipeline=pipeline_data, jira_ticket=jira_ticket)

    except Exception as e:
        if 'db_conn' in locals():
            db_conn.close()
        flash(f"❌ Error loading pipeline details: {str(e)}", "danger")
        return redirect("/vsphere/overview")

# 實際觸發 manual job
@vm_bp.route('/trigger-manual-job/<int:pipeline_id>', methods=['POST'])
def trigger_manual_job(pipeline_id):
    result = run_manual_job(pipeline_id)

    if result.get("success"):
        flash("✅ Successfully triggered manual job", "success")
    else:
        flash(f"❌ Failed to trigger: {result.get('error')}", "danger")

    return redirect("/vsphere/overview")