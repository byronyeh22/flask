import time
import threading
import json
import logging
from datetime import datetime

from app.mysql.db import get_db_connection

# ---------- GitLab ----------
from app.vsphere.vm.db.update_gitlab_pipeline_details import update_gitlab_pipeline_details
from app.vsphere.vm.gitlab_api.get_pipeline_status_from_gitlab import get_pipeline_status_from_gitlab

# ---------- Jira ----------
from app.vsphere.vm.db.get_jira_tickets_and_stats import get_jira_ticket_by_workflow_id
from app.vsphere.vm.db.insert_jira_info_to_db import insert_jira_info_to_db
from app.vsphere.vm.jira_api.create_jira_ticket import create_jira_ticket
from app.vsphere.vm.jira_api.get_jira_issue_detail import get_jira_issue_detail
from app.vsphere.vm.jira_api.issue_updates import jira_add_comment, jira_transition_issue

# Update workflow status
from app.vsphere.vm.db.workflow_manager import update_request_status
from mysql.connector import Error as MySQLError

# ---------- vSphere Disk Manager ----------
from app.vsphere.vm.vsphere_api.disk_manager import add_disk_to_vm

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
PIPELINE_MANUAL_STATUS = "manual"

# ---------- Save failed_message as JSON (overwrite same source) ----------
def set_failed_message(db_conn, workflow_id: int, source: str, message: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT failed_message FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
        old_json = {}
        if row and row.get("failed_message"):
            try:
                old_json = json.loads(row["failed_message"])
            except Exception:
                old_json = {}

        old_json[source] = f"[{ts}] {message}"

        cur2 = db_conn.cursor()
        try:
            cur2.execute(
                """
                UPDATE workflow_runs
                   SET failed_message = %s,
                       updated_at     = NOW()
                 WHERE workflow_id   = %s
                """,
                (json.dumps(old_json), workflow_id),
            )
            db_conn.commit()
            logging.info(f"💾 Failed message recorded for workflow {workflow_id}: {source} - {message}")
        finally:
            cur2.close()

    except Exception as e:
        logging.error(f"❌ Failed to save failed_message for workflow {workflow_id}: {str(e)}")
    finally:
        cur.close()

def ensure_jira_after_success(db_conn, workflow_id: int) -> None:
    try:
        existing = get_jira_ticket_by_workflow_id(workflow_id)
        if existing and existing.get("ticket_id"):
            logging.info(f"✅ Jira ticket already exists for workflow {workflow_id}")
            return

        cur = db_conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
            row = cur.fetchone()
        finally:
            cur.close()

        if not row or not row.get("request_payload"):
            set_failed_message(db_conn, workflow_id, "JIRA", "No request_payload to create Jira.")
            return

        try:
            payload = json.loads(row["request_payload"])
        except Exception as je:
            set_failed_message(db_conn, workflow_id, "JIRA", f"Invalid request_payload JSON: {je}")
            return

        form_data = payload.get("new_config", payload)

        logging.info(f"🎫 Creating Jira ticket for workflow {workflow_id}")
        jira_key = create_jira_ticket(form_data)
        logging.info(f"✅ Jira ticket created: {jira_key} for workflow {workflow_id}")

        time.sleep(5)  # 給 Jira 初始化

        # retry 加 comment
        max_retries = 5
        retry_delay = 3
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(retry_delay)
                logging.info(f"💬 Adding comment to {jira_key}, attempt {attempt + 1}/{max_retries}")
                jira_add_comment(jira_key, "Auto-created by pipeline SUCCESS.")
                logging.info(f"✅ Comment added successfully to {jira_key}")
                break
            except Exception as e:
                logging.warning(f"⚠️  [JIRA RETRY] Add comment failed {attempt + 1}/{max_retries} for {jira_key}: {str(e)}")
                if attempt + 1 == max_retries:
                    set_failed_message(db_conn, workflow_id, "JIRA", f"Add comment failed after {max_retries} attempts: {str(e)}")
                    return

        time.sleep(2)

        # retry transition
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(retry_delay)
                logging.info(f"🔄 Transitioning {jira_key} to Done, attempt {attempt + 1}/{max_retries}")
                jira_transition_issue(jira_key, "Done")
                logging.info(f"✅ Successfully transitioned {jira_key} to Done")
                break
            except Exception as e:
                logging.warning(f"⚠️  [JIRA RETRY] Transition failed {attempt + 1}/{max_retries} for {jira_key}: {str(e)}")
                if attempt + 1 == max_retries:
                    set_failed_message(db_conn, workflow_id, "JIRA", f"Transition to Done failed after {max_retries} attempts: {str(e)}")
                    return

        time.sleep(3)

        try:
            logging.info(f"📋 Fetching final details for {jira_key}")
            ticket_data = get_jira_issue_detail(jira_key)
            insert_jira_info_to_db(workflow_id, ticket_data)
            logging.info(f"✅ Successfully inserted ticket {jira_key} to database for workflow {workflow_id}")
        except Exception as e:
            logging.error(f"❌ Insert ticket to DB failed for {jira_key}: {str(e)}")
            set_failed_message(db_conn, workflow_id, "JIRA", f"Insert ticket to DB failed: {str(e)}")
            return

    except Exception as e:
        logging.error(f"❌ Create ticket failed for workflow {workflow_id}: {str(e)}")
        set_failed_message(db_conn, workflow_id, "JIRA", f"Create ticket failed: {str(e)}")

# ---------- Disk helpers ----------
def _get_vm_config_id_by_workflow(db_conn, workflow_id: int) -> int:
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
        if not row or not row.get("request_payload"):
            raise ValueError("No request_payload")

        payload = json.loads(row["request_payload"])
        form_data = payload.get("new_config", payload)
        environment_value = (form_data.get("environment") or "").strip()
        vm_name_prefix_value = (form_data.get("vm_name_prefix") or "").strip()
        if not environment_value or not vm_name_prefix_value:
            raise ValueError("Missing environment or vm_name_prefix in payload")

        cur.execute(
            """
            SELECT id
              FROM vm_configurations
             WHERE environment = %s AND vm_name_prefix = %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (environment_value, vm_name_prefix_value),
        )
        vmc = cur.fetchone()
        if not vmc:
            raise ValueError(f"vm_configurations not found for env={environment_value}, prefix={vm_name_prefix_value}")
        return vmc["id"]
    finally:
        cur.close()

def _get_vm_name_prefix_by_config_id(db_conn, vm_configuration_id: int) -> str:
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT vm_name_prefix FROM vm_configurations WHERE id = %s", (vm_configuration_id,))
        row = cur.fetchone()
        if not row or not row.get("vm_name_prefix"):
            raise ValueError(f"vm_configurations {vm_configuration_id} has no vm_name_prefix")
        return row["vm_name_prefix"]
    finally:
        cur.close()

def _acquire_batch_lock(cur, workflow_id: int, timeout_sec: int = 10) -> bool:
    lock_name = f"ensure_disks_{workflow_id}"
    cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, timeout_sec))
    got = cur.fetchone()
    return bool(got and list(got.values())[0] == 1)

def _release_batch_lock(cur, workflow_id: int) -> None:
    lock_name = f"ensure_disks_{workflow_id}"
    try:
        cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
    except Exception:
        pass

def ensure_disks_after_success(db_conn, workflow_id: int) -> None:
    # 使用獨立的 cursor 處理鎖
    lock_cur = db_conn.cursor(dictionary=True)
    lock_acquired = False

    try:
        # 獲取鎖
        lock_name = f"ensure_disks_{workflow_id}"
        lock_cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
        lock_result = lock_cur.fetchone()
        lock_cur.fetchall()  # 清空剩餘結果

        if not (lock_result and list(lock_result.values())[0] == 1):
            logging.info(f"🧷 Skip ensure_disks_after_success for workflow {workflow_id}: lock busy")
            return

        lock_acquired = True

        try:
            vm_configuration_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)
            vm_name_prefix = _get_vm_name_prefix_by_config_id(db_conn, vm_configuration_id)

            # 使用新的 cursor 查詢磁碟
            disk_cur = db_conn.cursor(dictionary=True)
            try:
                disk_cur.execute(
                    """
                    SELECT id, size, disk_provisioning
                      FROM vm_disks
                     WHERE vm_configuration_id = %s
                       AND status = 'PENDING_CREATION'
                     ORDER BY id ASC
                    """,
                    (vm_configuration_id,),
                )
                pending_disks = disk_cur.fetchall()
            finally:
                disk_cur.close()

            if not pending_disks:
                logging.info(f"🟢 No pending disks for workflow {workflow_id} (vm_config_id={vm_configuration_id})")
                return

            logging.info(f"💽 Creating {len(pending_disks)} disk(s) for VM '{vm_name_prefix}' (workflow {workflow_id})")

            # 其餘磁碟處理邏輯保持不變...
            for disk_row in pending_disks:
                disk_id = disk_row["id"]
                size_gb = int(disk_row["size"])
                provisioning = (disk_row["disk_provisioning"] or "").strip().lower()
                if provisioning not in ("thin", "thick_lazy", "thick_eager"):
                    provisioning = "thin"

                # 1) 搶單
                claim_cur = db_conn.cursor()
                try:
                    claim_cur.execute(
                        """
                        UPDATE vm_disks
                           SET status='CREATING',
                               updated_at=NOW()
                         WHERE id=%s
                           AND status='PENDING_CREATION'
                        """,
                        (disk_id,)
                    )
                    db_conn.commit()
                    if claim_cur.rowcount != 1:
                        continue
                finally:
                    claim_cur.close()

                try:
                    # 2) 呼叫新增磁碟
                    result = add_disk_to_vm(vm_name_prefix, {
                        "size_gb": size_gb,
                        "provision_type": provisioning
                    })

                    scsi_controller = result.get("controller_bus")
                    unit_number = result.get("unit_number")
                    label_number = result.get("label_number")
                    vmdk_path = result.get("vmdk_path")

                    # 3) 成功回寫
                    ok_cur = db_conn.cursor()
                    try:
                        ok_cur.execute(
                            """
                            UPDATE vm_disks
                               SET scsi_controller = %s,
                                   unit_number     = %s,
                                   label           = %s,
                                   vmdk_path       = %s,
                                   status          = 'CREATED',
                                   updated_at      = NOW()
                             WHERE id = %s
                               AND status = 'CREATING'
                            """,
                            (scsi_controller, unit_number, label_number, vmdk_path, disk_id)
                        )
                        db_conn.commit()
                    finally:
                        ok_cur.close()

                    logging.info(f"✅ Disk #{disk_id} created at scsi({scsi_controller}:{unit_number}) label=Hard disk {label_number}")

                except Exception as e:
                    logging.error(f"❌ Failed to create disk #{disk_id} for VM '{vm_name_prefix}': {e}")

                    try:
                        fail_cur = db_conn.cursor()
                        try:
                            fail_cur.execute(
                                """
                                UPDATE vm_disks
                                   SET status='FAILED',
                                       updated_at=NOW()
                                 WHERE id=%s
                                   AND status IN ('PENDING_CREATION','CREATING')
                                """,
                                (disk_id,)
                            )
                            db_conn.commit()
                        finally:
                            fail_cur.close()
                    except Exception as write_err:
                        logging.error(f"❌ Also failed to mark disk #{disk_id} as FAILED: {write_err}")

                    set_failed_message(db_conn, workflow_id, f"DISK:{disk_id}", f"Create disk failed: {str(e)}")

        except Exception as e:
            logging.error(f"❌ ensure_disks_after_success error for workflow {workflow_id}: {e}")
            set_failed_message(db_conn, workflow_id, "DISK", f"Batch disk create failed: {str(e)}")

    finally:
        # 釋放鎖
        if lock_acquired:
            try:
                lock_name = f"ensure_disks_{workflow_id}"
                lock_cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                lock_cur.fetchone()  # 讀取結果
                lock_cur.fetchall()  # 清空剩餘結果
            except Exception as e:
                logging.error(f"❌ Failed to release lock for workflow {workflow_id}: {e}")

        lock_cur.close()

def _summarize_disk_batch_state(db_conn, workflow_id: int):
    """
    回傳 (vm_configuration_id, counts)
    counts 是 dict：{'PENDING_CREATION':x,'CREATING':y,'CREATED':z,'FAILED':w}
    """
    vm_configuration_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT status, COUNT(*) AS cnt
              FROM vm_disks
             WHERE vm_configuration_id = %s
             GROUP BY status
            """,
            (vm_configuration_id,)
        )
        rows = cur.fetchall()
        counts = {'PENDING_CREATION': 0, 'CREATING': 0, 'CREATED': 0, 'FAILED': 0}
        for r in rows:
            s = (r['status'] or '').upper()
            if s in counts:
                counts[s] = int(r['cnt'])
        return vm_configuration_id, counts
    finally:
        cur.close()

# ---------- 修正：獲取 Pipeline 狀態和磁碟狀態的統一函數 ----------
def get_workflow_state_info(db_conn, workflow_id: int) -> dict:
    """
    一次性獲取 workflow 的所有狀態信息，避免重複查詢
    """
    try:
        # 1. 獲取 Pipeline 狀態
        cur = db_conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT status
                  FROM gitlab_pipelines
                 WHERE workflow_id = %s
                 ORDER BY started_at DESC, pipeline_id DESC
                 LIMIT 1
                """,
                (workflow_id,)
            )
            pipeline = cur.fetchone()
        finally:
            cur.close()

        pipeline_status = (pipeline.get("status") or "").lower() if pipeline else "unknown"

        # 2. 獲取磁碟狀態
        vmc_id, actual_counts = _summarize_disk_batch_state(db_conn, workflow_id)

        # 3. 計算有效磁碟狀態
        effective_counts = actual_counts.copy()
        if pipeline_status in ("failed", "canceled"):
            effective_counts['PENDING_CREATION'] = 0
            effective_counts['CREATING'] = 0
            logging.info(f"🚫 Pipeline {pipeline_status} for workflow {workflow_id}, ignoring pending/creating disks")

        return {
            'vm_configuration_id': vmc_id,
            'actual_counts': actual_counts,
            'effective_counts': effective_counts,
            'pipeline_status': pipeline_status
        }

    except Exception as e:
        logging.error(f"❌ Error getting workflow state info for workflow {workflow_id}: {e}")
        return {
            'vm_configuration_id': 0,
            'actual_counts': {'PENDING_CREATION': 0, 'CREATING': 0, 'CREATED': 0, 'FAILED': 0},
            'effective_counts': {'PENDING_CREATION': 0, 'CREATING': 0, 'CREATED': 0, 'FAILED': 0},
            'pipeline_status': 'unknown'
        }

# ---------- 修正：根據狀態決定 Workflow 目標狀態 ----------
def determine_workflow_status_after_pipeline(db_conn, workflow_id: int, pipeline_status: str) -> str:
    """根據 Pipeline 狀態和磁碟狀態決定 Workflow 狀態"""

    logging.info(f"🔍 Determining status for workflow {workflow_id}, pipeline_status={pipeline_status}")

    if pipeline_status == "failed":
        logging.info(f"➡️ Workflow {workflow_id} -> FAILED (pipeline failed)")
        return "FAILED"
    elif pipeline_status == "canceled":
        logging.info(f"➡️ Workflow {workflow_id} -> CANCELED (pipeline canceled)")
        return "CANCELED"
    elif pipeline_status == "manual":
        logging.info(f"➡️ Workflow {workflow_id} -> PENDING_APPROVAL (pipeline manual)")
        return "PENDING_APPROVAL"
    elif pipeline_status == "success":
        state_info = get_workflow_state_info(db_conn, workflow_id)
        counts = state_info['effective_counts']

        pend = counts['PENDING_CREATION'] + counts['CREATING']
        fail = counts['FAILED']

        logging.info(f"📊 Workflow {workflow_id} effective disk counts: {counts}")

        if fail > 0:
            logging.info(f"➡️ Workflow {workflow_id} -> FAILED ({fail} disks failed)")
            return "FAILED"
        elif pend > 0:
            logging.info(f"➡️ Workflow {workflow_id} -> DEPLOYING ({pend} disks pending)")
            return "DEPLOYING"
        else:
            logging.info(f"➡️ Workflow {workflow_id} -> SUCCESS (all disks completed)")
            return "SUCCESS"
    else:
        logging.info(f"➡️ Workflow {workflow_id} -> IN_PROGRESS (pipeline status: {pipeline_status})")
        return "IN_PROGRESS"

# ---------- Check if GitLab pipeline is manual ----------
def is_pipeline_manual_for_workflow(db_conn, workflow_id: int) -> bool:
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT status, pipeline_id
              FROM gitlab_pipelines
             WHERE workflow_id = %s
             ORDER BY started_at DESC, pipeline_id DESC
             LIMIT 1
            """,
            (workflow_id,),
        )
        row = cur.fetchone()
        if not row:
            set_failed_message(db_conn, workflow_id, "GITLAB", "No pipeline found for this workflow")
            return False
        return (row.get("status") or "").strip().lower() == PIPELINE_MANUAL_STATUS
    finally:
        cur.close()

# ---------- 修正：處理 workflow 狀態更新的統一函數 ----------
def process_workflow_status_update(db_conn, workflow_id: int, target_status: str, pipeline_id: int) -> None:
    """統一處理 workflow 狀態更新邏輯"""
    try:
        logging.info(f"🎯 Processing workflow {workflow_id} status update to {target_status}")

        if target_status == "SUCCESS":
            logging.info(f"✨ Processing SUCCESS for workflow {workflow_id}")
            ensure_disks_after_success(db_conn, workflow_id)
            logging.info(f"🎫 About to ensure Jira for workflow {workflow_id}")
            ensure_jira_after_success(db_conn, workflow_id)
            logging.info(f"✅ Completed SUCCESS processing for workflow {workflow_id}")

        elif target_status == "DEPLOYING":
            logging.info(f"⚙️ Processing DEPLOYING for workflow {workflow_id}")
            ensure_disks_after_success(db_conn, workflow_id)

        elif target_status in ("FAILED", "CANCELED"):
            logging.info(f"❌ Processing {target_status} for workflow {workflow_id}")
            set_failed_message(db_conn, workflow_id, "GITLAB", f"Pipeline {pipeline_id} status is {target_status.lower()}")

        logging.info(f"📝 About to update workflow {workflow_id} status to {target_status}")
        update_request_status(workflow_id, target_status)
        logging.info(f"✅ Updated workflow {workflow_id} status to {target_status}")

    except Exception as e:
        logging.error(f"❌ Error processing workflow {workflow_id} status update: {e}")
        import traceback
        logging.error(f"❌ Full traceback: {traceback.format_exc()}")

# ---------- Monitor pipelines ----------
def monitor_pipelines(app):
    with app.app_context():
        while True:
            print("\n🚀 Start monitoring GitLab pipelines...")

            db_conn = None
            try:
                db_conn = get_db_connection()

                # 1) 列出所有尚未終態的 workflows（7 天內）
                cur = db_conn.cursor(dictionary=True)
                try:
                    cur.execute(
                        """
                        SELECT workflow_id, status
                          FROM workflow_runs
                         WHERE status NOT IN ('SUCCESS','FAILED','CANCELED')
                           AND created_at >= NOW() - INTERVAL 7 DAY
                         ORDER BY workflow_id DESC
                        """
                    )
                    workflows_todo = cur.fetchall()
                finally:
                    cur.close()

                for wr in workflows_todo:
                    workflow_id = wr["workflow_id"]

                    # 2) 該 workflow 最新一筆 pipeline
                    cur_p = db_conn.cursor(dictionary=True)
                    try:
                        cur_p.execute(
                            """
                            SELECT pipeline_id, workflow_id, status, started_at
                              FROM gitlab_pipelines
                             WHERE workflow_id = %s
                             ORDER BY started_at DESC, pipeline_id DESC
                             LIMIT 1
                            """,
                            (workflow_id,),
                        )
                        latest = cur_p.fetchone()
                    finally:
                        cur_p.close()

                    if not latest:
                        continue

                    pipeline_id = latest["pipeline_id"]
                    db_status   = (latest.get("status") or "").lower()

                    # 3) 非終態 → 打 GitLab API 更新 DB
                    if db_status not in ("success", "failed", "canceled"):
                        print(f"🔍 [poll] wf={workflow_id} pid={pipeline_id} DB={db_status} → query GitLab")

                        gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)
                        if gitlab_result.get("success"):
                            update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)
                            fresh = (gitlab_result.get("status") or "").lower()
                            print(f"⚙️  [poll] wf={workflow_id} pid={pipeline_id} GitLab={fresh}")

                            # RETURNED 不覆蓋
                            cur_now = db_conn.cursor(dictionary=True)
                            try:
                                cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                                wr_now = cur_now.fetchone()
                            finally:
                                cur_now.close()

                            if wr_now and (wr_now.get("status") or "").upper() == "RETURNED":
                                continue

                            # 使用新的統一邏輯
                            target_status = determine_workflow_status_after_pipeline(db_conn, workflow_id, fresh)
                            process_workflow_status_update(db_conn, workflow_id, target_status, pipeline_id)

                        else:
                            set_failed_message(
                                db_conn, workflow_id, "GITLAB_API",
                                f"Pipeline query failed: {gitlab_result.get('error')}",
                            )

                    else:
                        # 4) DB 已是終態 → 直接補同步
                        print(f"🧹 [finalize] wf={workflow_id} pid={pipeline_id} DB={db_status}")

                        cur_now = db_conn.cursor(dictionary=True)
                        try:
                            cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                            wr_now = cur_now.fetchone()
                        finally:
                            cur_now.close()

                        if wr_now and (wr_now.get("status") or "").upper() == "RETURNED":
                            continue

                        # 使用新的統一邏輯
                        target_status = determine_workflow_status_after_pipeline(db_conn, workflow_id, db_status)
                        process_workflow_status_update(db_conn, workflow_id, target_status, pipeline_id)

            except Exception as e:
                print(f"❌ Pipeline monitoring error: {e}")
                import traceback
                print(f"❌ Full traceback: {traceback.format_exc()}")
            finally:
                if db_conn:
                    db_conn.close()

            # 輪詢間隔
            time.sleep(5)

# ---------- Scan IN_PROGRESS workflows ----------
def monitor_workflows(app):
    with app.app_context():
        while True:
            print("\n🧭 Start scanning workflows for status advancement...")

            db_conn = None
            try:
                db_conn = get_db_connection()
                cur = db_conn.cursor(dictionary=True)
                try:
                    cur.execute(
                        """
                        SELECT workflow_id
                          FROM workflow_runs
                         WHERE status = 'IN_PROGRESS'
                           AND created_at >= NOW() - INTERVAL 7 DAY
                        """
                    )
                    items = cur.fetchall()
                finally:
                    cur.close()

                for row in items:
                    wf_id = row["workflow_id"]
                    # 僅在 pipeline=manual 時推進至 PENDING_APPROVAL
                    if is_pipeline_manual_for_workflow(db_conn, wf_id):
                        update_request_status(wf_id, "PENDING_APPROVAL")

            except Exception as e:
                print(f"❌ Workflow scanning error: {e}")
                import traceback
                print(f"❌ Full traceback: {traceback.format_exc()}")
            finally:
                if db_conn:
                    cur.close()
                    db_conn.close()

            time.sleep(60)

def start_monitor_thread(app):
    t1 = threading.Thread(target=monitor_pipelines, args=(app,), daemon=True)
    t1.start()
    print("✅ Pipeline Monitor Thread started")

    t2 = threading.Thread(target=monitor_workflows, args=(app,), daemon=True)
    t2.start()
    print("✅ Workflow Monitor Thread started")