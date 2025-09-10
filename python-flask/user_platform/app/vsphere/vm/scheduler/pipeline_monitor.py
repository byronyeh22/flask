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
    """
    Save error message into workflow_runs.failed_message as JSON.
    Example: {"JIRA": "[ts] Jira ticket not created or not found", "GITLAB": "[ts] Pipeline 123 failed"}
    Same source will be overwritten if retried.
    """
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
        cur2.close()
        logging.info(f"💾 Failed message recorded for workflow {workflow_id}: {source} - {message}")

    except Exception as e:
        logging.error(f"❌ Failed to save failed_message for workflow {workflow_id}: {str(e)}")
    finally:
        cur.close()

def ensure_jira_after_success(db_conn, workflow_id: int) -> None:
    """
    若該 workflow 還沒有 Jira，就在 pipeline success 後開單（idempotent）。
    建單後：
      1) 先加一則 comment（若失敗 -> 記 failed_message 並 return）
      2) 再 transition 到 Done（若失敗 -> 記 failed_message 並 return）
      3) 成功後重新抓 issue 詳細，再 insert 到 DB（最終狀態才入庫）
    """
    try:
        existing = get_jira_ticket_by_workflow_id(workflow_id)
        if existing and existing.get("ticket_id"):
            logging.info(f"✅ Jira ticket already exists for workflow {workflow_id}")
            return

        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
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

# ---------- Disk creation after SUCCESS ----------
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
    """
    用 MySQL GET_LOCK 當跨程序互斥鎖，避免 ensure_disks_after_success 在多條路徑被同時執行。
    """
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
    """
    在 pipeline success 後：
      - 以 DB 鎖防重入（GET_LOCK）
      - 撈出該 VM 的 PENDING_CREATION 磁碟
      - 逐顆以「搶單」UPDATE 將狀態改為 CREATING（只有搶到的那顆才會真的建立）
      - 呼叫 add_disk_to_vm()
      - 成功：回寫 slot & 路徑 & 狀態='CREATED'
      - 失敗：狀態='FAILED' + failed_message
    """
    lock_cur = db_conn.cursor(dictionary=True)
    try:
        if not _acquire_batch_lock(lock_cur, workflow_id, timeout_sec=10):
            logging.info(f"🧷 Skip ensure_disks_after_success for workflow {workflow_id}: lock busy")
            return

        try:
            vm_configuration_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)
            vm_name_prefix = _get_vm_name_prefix_by_config_id(db_conn, vm_configuration_id)

            cur = db_conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT id, size, disk_provisioning
                  FROM vm_disks
                 WHERE vm_configuration_id = %s
                   AND status = 'PENDING_CREATION'
                 ORDER BY id ASC
                """,
                (vm_configuration_id,),
            )
            pending_disks = cur.fetchall()
            cur.close()

            if not pending_disks:
                logging.info(f"🟢 No pending disks for workflow {workflow_id} (vm_config_id={vm_configuration_id})")
                return

            logging.info(f"💽 Creating {len(pending_disks)} disk(s) for VM '{vm_name_prefix}' (workflow {workflow_id})")

            for disk_row in pending_disks:
                disk_id = disk_row["id"]
                size_gb = int(disk_row["size"])
                provisioning = (disk_row["disk_provisioning"] or "").strip().lower()
                if provisioning not in ("thin", "thick_lazy", "thick_eager"):
                    provisioning = "thin"

                # 1) 先「搶單」：只處理 status=PENDING_CREATION 的那顆
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
                        # 已被其它程序/輪次處理了，跳過
                        continue
                finally:
                    claim_cur.close()

                try:
                    # 2) 呼叫 vSphere / mock 新增磁碟
                    result = add_disk_to_vm(vm_name_prefix, {
                        "size_gb": size_gb,
                        "provision_type": provisioning
                    })

                    scsi_controller = result.get("controller_bus")
                    unit_number = result.get("unit_number")
                    label_number = result.get("label_number")
                    vmdk_path = result.get("vmdk_path")

                    # 3) 成功回寫（守門：只在 CREATING 狀態下寫入）
                    ok_cur = db_conn.cursor()
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
                    ok_cur.close()

                    logging.info(f"✅ Disk #{disk_id} created at scsi({scsi_controller}:{unit_number}) label=Hard disk {label_number}")

                except Exception as e:
                    logging.error(f"❌ Failed to create disk #{disk_id} for VM '{vm_name_prefix}': {e}")

                    try:
                        fail_cur = db_conn.cursor()
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
                        fail_cur.close()
                    except Exception as write_err:
                        logging.error(f"❌ Also failed to mark disk #{disk_id} as FAILED: {write_err}")

                    set_failed_message(db_conn, workflow_id, f"DISK:{disk_id}", f"Create disk failed: {str(e)}")

        finally:
            _release_batch_lock(lock_cur, workflow_id)

    except Exception as e:
        logging.error(f"❌ ensure_disks_after_success error for workflow {workflow_id}: {e}")
        set_failed_message(db_conn, workflow_id, "DISK", f"Batch disk create failed: {str(e)}")
    finally:
        lock_cur.close()

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

# ---------- Advance to PENDING_APPROVAL when GitLab is manual ----------
def maybe_advance_to_pending_approval(db_conn, workflow_id: int) -> bool:
    try:
        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        wf_row = cur.fetchone()
        cur.close()

        if wf_row and (wf_row.get("status") or "").upper() == "RETURNED":
            return False

        manual_ok = is_pipeline_manual_for_workflow(db_conn, workflow_id)
        if not manual_ok:
            return False

        update_request_status(workflow_id, "PENDING_APPROVAL")
        return True

    except MySQLError as e:
        set_failed_message(db_conn, workflow_id, "WORKFLOW", f"Failed to advance status: {e}")
        return False

# ---------- Monitor pipelines ----------
def monitor_pipelines(app):
    with app.app_context():
        while True:
            print("\n🚀 Start monitoring GitLab pipelines...")

            db_conn = get_db_connection()
            try:
                # 1) 列出所有尚未終態的 workflows（7 天內）
                cur = db_conn.cursor(dictionary=True)
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
                cur.close()

                for wr in workflows_todo:
                    workflow_id = wr["workflow_id"]

                    # 2) 該 workflow 最新一筆 pipeline
                    cur_p = db_conn.cursor(dictionary=True)
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
                            cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                            wr_now = cur_now.fetchone()
                            cur_now.close()
                            if wr_now and (wr_now.get("status") or "").upper() == "RETURNED":
                                continue

                            if fresh == "manual":
                                maybe_advance_to_pending_approval(db_conn, workflow_id)

                            elif fresh == "success":
                                # 先處理磁碟，再更新 workflow 狀態 & Jira
                                ensure_disks_after_success(db_conn, workflow_id)
                                update_request_status(workflow_id, "SUCCESS")
                                ensure_jira_after_success(db_conn, workflow_id)

                            elif fresh == "failed":
                                update_request_status(workflow_id, "FAILED")
                                set_failed_message(
                                    db_conn, workflow_id, "GITLAB",
                                    f"Pipeline {pipeline_id} status is failed"
                                )

                            elif fresh == "canceled":
                                update_request_status(workflow_id, "CANCELED")
                                set_failed_message(db_conn, workflow_id, "GITLAB",
                                                   f"Pipeline {pipeline_id} status is canceled"
                                )

                        else:
                            set_failed_message(
                                db_conn, workflow_id, "GITLAB_API",
                                f"Pipeline query failed: {gitlab_result.get('error')}",
                            )

                    else:
                        # 4) DB 已是終態 → 直接補同步
                        print(f"🧹 [finalize] wf={workflow_id} pid={pipeline_id} DB={db_status}")

                        cur_now = db_conn.cursor(dictionary=True)
                        cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                        wr_now = cur_now.fetchone()
                        cur_now.close()
                        if wr_now and (wr_now.get("status") or "").upper() == "RETURNED":
                            continue

                        if db_status == "success":
                            ensure_disks_after_success(db_conn, workflow_id)
                            update_request_status(workflow_id, "SUCCESS")
                            ensure_jira_after_success(db_conn, workflow_id)
                        elif db_status == "failed":
                            update_request_status(workflow_id, "FAILED")
                            set_failed_message(db_conn, workflow_id, "GITLAB",
                                               f"Pipeline {pipeline_id} status is failed")
                        elif db_status == "canceled":
                            set_failed_message(db_conn, workflow_id, "GITLAB",
                                               f"Pipeline {pipeline_id} status is canceled")

            except Exception as e:
                print(f"❌ Pipeline monitoring error: {e}")
            finally:
                db_conn.close()

            # 輪詢間隔
            time.sleep(5)

# ---------- Scan IN_PROGRESS workflows ----------
def monitor_workflows(app):
    with app.app_context():
        while True:
            print("\n🧭 Start scanning workflows for status advancement...")

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

                for row in items:
                    wf_id = row["workflow_id"]
                    maybe_advance_to_pending_approval(db_conn, wf_id)

            except Exception as e:
                print(f"❌ Workflow scanning error: {e}")

            finally:
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