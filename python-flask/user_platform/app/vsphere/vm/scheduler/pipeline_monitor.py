import time
import threading
import json
from datetime import datetime

from app.mysql.db import get_db_connection

# ---------- LOG ----------
import logging
from app.log.logging_setup import log_context, bind_context, new_trace_id
logger = logging.getLogger(__name__)


# ---------- GitLab ----------
from app.vsphere.vm.db.update_gitlab_pipeline_details import update_gitlab_pipeline_details
from app.vsphere.vm.gitlab_api.get_pipeline_status_from_gitlab import get_pipeline_status_from_gitlab

# ---------- Jira ----------
from app.vsphere.vm.db.get_jira_tickets_and_stats import get_jira_ticket_by_workflow_id
from app.vsphere.vm.db.insert_jira_info_to_db import insert_jira_info_to_db
from app.vsphere.vm.jira_api.create_jira_ticket import create_jira_ticket
from app.vsphere.vm.jira_api.get_jira_issue_detail import get_jira_issue_detail
from app.vsphere.vm.jira_api.issue_updates import jira_add_comment, jira_transition_issue

# ---------- vSphere ----------
from app.vsphere.vm.db.delete_vm_from_database import delete_vm_from_database
from app.vsphere.vm.db.vm_provisioning_manager import sync_disk_labels_to_database

# ---------- Vault & VMIP Update ----------
from app.vsphere.vm.vault.vault_manager import VaultManager
from app.vsphere.vm.db.vm_provisioning_manager import update_vm_ipv4_ip

# Update workflow status
from app.vsphere.vm.db.workflow_manager import update_request_status, get_workflow_status
from mysql.connector import Error as MySQLError

# ---------- vSphere Disk Manager ----------
from app.vsphere.vm.vsphere_api.disk_manager import add_disk_to_vm
from app.vsphere.vm.vsphere_api.disk_manager import update_disk_size
from app.vsphere.vm.vsphere_api.disk_manager import remove_disk_from_vm
from app.vsphere.vm.vsphere_api.disk_manager import get_vm_disks

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
            logger.info(f"💾 Failed message recorded for workflow {workflow_id}: {source} - {message}")
        finally:
            cur2.close()

    except Exception as e:
        logger.error(f"❌ Failed to save failed_message for workflow {workflow_id}: {str(e)}")
    finally:
        cur.close()


# ---------- helpers ----------
def _refresh_pipeline_from_gitlab(db_conn, pipeline_id: int) -> str:
    """
    直接打 GitLab 取最新 pipeline 詳情，並用 update_gitlab_pipeline_details 寫回 DB。
    回傳最新的狀態（小寫），失敗則回空字串。
    """
    try:
        result = get_pipeline_status_from_gitlab(pipeline_id)
        if result.get("success"):
            update_gitlab_pipeline_details(db_conn, pipeline_id, result)
            return (result.get("status") or "").lower()
        else:
            logging.warning(f"⚠️ GitLab refresh failed for pipeline {pipeline_id}: {result.get('error')}")
            return ""
    except Exception as e:
        logging.warning(f"⚠️ GitLab refresh threw for pipeline {pipeline_id}: {e}")
        return ""

def ensure_jira_after_success(db_conn, workflow_id: int) -> None:
    """
    Jira 建立邏輯，使用資料庫鎖避免重複建立
    """
    # 使用 MySQL 的命名鎖確保原子性
    lock_cur = db_conn.cursor(dictionary=True)
    lock_acquired = False

    try:
        # 獲取專門的 Jira 鎖
        lock_name = f"jira_creation_{workflow_id}"
        lock_cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
        lock_result = lock_cur.fetchone()
        lock_cur.fetchall()  # 清空剩餘結果

        if not (lock_result and list(lock_result.values())[0] == 1):
            logger.info(f"Skip Jira creation for workflow {workflow_id}: lock busy or timeout")
            return

        lock_acquired = True
        logger.info(f"Acquired Jira creation lock for workflow {workflow_id}")

        # 確認是否已存在 Jira ticket
        existing = get_jira_ticket_by_workflow_id(workflow_id)
        if existing and existing.get("ticket_id"):
            logger.info(f"Jira ticket already exists for workflow {workflow_id}: {existing.get('ticket_id')}")
            return

        # 獲取 request_payload
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

        # 根據 action_type 決定傳遞的資料結構
        action_type = payload.get("action_type", "create")

        if action_type == "update":
            # UPDATE 操作：傳遞完整的 payload(包含 original_config 和 new_config)
            form_data = payload
        elif action_type == "delete":
            # DELETE 操作：傳遞完整的 payload(包含 original_config)
            form_data = payload
        else:
            # CREATE 或其他操作：使用原有邏輯
            form_data = payload.get("new_config", payload)

        logger.info(f"Creating Jira ticket for workflow {workflow_id} with action_type: {action_type}")

        # 建立 Jira ticket
        jira_key = create_jira_ticket(form_data)
        logger.info(f"Successfully created Jira ticket: {jira_key} for workflow {workflow_id}")

        # 給 Jira 初始化時間
        time.sleep(5)

        # retry 機制：新增 comment
        max_retries = 5
        retry_delay = 3
        comment_success = False

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(retry_delay)
                logger.info(f"Adding comment to {jira_key}, attempt {attempt + 1}/{max_retries}")
                jira_add_comment(jira_key, "Auto-created by pipeline SUCCESS.")
                logger.info(f"Successfully added comment to {jira_key}")
                comment_success = True
                break
            except Exception as e:
                logging.warning(f"Add comment failed {attempt + 1}/{max_retries} for {jira_key}: {str(e)}")
                if attempt + 1 == max_retries:
                    set_failed_message(db_conn, workflow_id, "JIRA", f"Add comment failed after {max_retries} attempts: {str(e)}")

        # 如果 comment 失敗，記錄但繼續處理
        if not comment_success:
            logging.warning(f"Comment addition failed for {jira_key}, but continuing with transition")

        time.sleep(2)

        # retry 機制：轉換狀態到 Done
        transition_success = False
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(retry_delay)
                logger.info(f"Transitioning {jira_key} to Done, attempt {attempt + 1}/{max_retries}")
                jira_transition_issue(jira_key, "Done")
                logger.info(f"Successfully transitioned {jira_key} to Done")
                transition_success = True
                break
            except Exception as e:
                logging.warning(f"Transition failed {attempt + 1}/{max_retries} for {jira_key}: {str(e)}")
                if attempt + 1 == max_retries:
                    set_failed_message(db_conn, workflow_id, "JIRA", f"Transition to Done failed after {max_retries} attempts: {str(e)}")

        # 如果 transition 失敗，記錄但繼續處理
        if not transition_success:
            logging.warning(f"Transition to Done failed for {jira_key}, but continuing with database insertion")

        time.sleep(3)

        # 獲取最終的 ticket 詳細資訊並插入資料庫
        try:
            logger.info(f"Fetching final details for {jira_key}")
            ticket_data = get_jira_issue_detail(jira_key)
            insert_jira_info_to_db(workflow_id, ticket_data)
            logger.info(f"Successfully inserted ticket {jira_key} to database for workflow {workflow_id}")
        except Exception as e:
            logger.error(f"Insert ticket to DB failed for {jira_key}: {str(e)}")
            set_failed_message(db_conn, workflow_id, "JIRA", f"Insert ticket to DB failed: {str(e)}")
            return

        # 只有在 Jira ticket 完全建立並插入資料庫後，才標記為成功
        logger.info(f"Jira ticket creation process completed successfully for workflow {workflow_id}")

    except Exception as e:
        logger.error(f"Create ticket failed for workflow {workflow_id}: {str(e)}")
        set_failed_message(db_conn, workflow_id, "JIRA", f"Create ticket failed: {str(e)}")
    finally:
        # 釋放鎖
        if lock_acquired:
            try:
                lock_name = f"jira_creation_{workflow_id}"
                lock_cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                lock_cur.fetchone()  # 讀取結果
                lock_cur.fetchall()  # 清空剩餘結果
                logger.info(f"Released Jira creation lock for workflow {workflow_id}")
            except Exception as e:
                logger.error(f"Failed to release Jira lock for workflow {workflow_id}: {e}")

        lock_cur.close()

def _get_vm_config_details_for_ip_update(db_conn, vm_configuration_id: int): # 移除 Optional[Dict] 類型提示
    """
    根據 vm_configuration_id 從 DB 取得 IP 更新所需的欄位 (env, os_type, vm_name_prefix)。
    """
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT environment, os_type, vm_name_prefix
              FROM vm_configurations
             WHERE id = %s
             LIMIT 1
            """,
            (vm_configuration_id,)
        )
        return cur.fetchone()
    finally:
        cur.close()

# ---------- Vault IP Update ----------
# ---------- Vault IP Update ----------
def update_vm_ip_from_vault(db_conn, workflow_id: int) -> bool:
    """
    從 Vault 取得 VM IP 並更新到資料庫
    回傳: True=成功, False=失敗
    """
    # 🔒 使用 MySQL Named Lock 防止並發更新
    lock_cur = db_conn.cursor(dictionary=True)
    lock_acquired = False
    
    try:
        # 獲取 IP 更新鎖
        lock_name = f"ip_update_{workflow_id}"
        lock_cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
        lock_result = lock_cur.fetchone()
        lock_cur.fetchall()  # 清空剩餘結果
        
        if not (lock_result and list(lock_result.values())[0] == 1):
            logger.info(f"⏭️ [IP_SKIP] IP update for workflow {workflow_id} is being processed, skipping")
            return True  # 返回 True,因為其他線程會處理
        
        lock_acquired = True
        logger.info(f"🔒 [IP_LOCK] Acquired IP update lock for workflow {workflow_id}")
        
        try:
            logger.info(f"🌐 [IP_UPDATE] Starting IP update for workflow {workflow_id}")

            # 1. 取得 vm_config_id
            try:
                vm_config_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)
            except Exception as e:
                logger.error(f"❌ [IP_UPDATE] Failed to get vm_config_id: {e}")
                set_failed_message(db_conn, workflow_id, "DB_VM_ID", f"Failed to get vm_config_id: {e}")
                return False

            # 2. 取得 VM 詳細資訊
            vm_details = _get_vm_config_details_for_ip_update(db_conn, vm_config_id)
            if not vm_details:
                logger.error(f"❌ [IP_UPDATE] Failed to retrieve VM details for vm_config_id={vm_config_id}")
                set_failed_message(db_conn, workflow_id, "DB_VM_INFO", "Failed to retrieve VM details for IP update.")
                return False

            logger.info(f"🔍 [IP_UPDATE] VM: {vm_details['environment']}-{vm_details['os_type']}/{vm_details['vm_name_prefix']}")

            # 3. 從 Vault 取得 IP (含重試機制)
            vault_manager = VaultManager()
            max_retries = 5
            retry_delay = 3
            ip_address = None

            for attempt in range(max_retries):
                if attempt > 0:
                    time.sleep(retry_delay)

                logger.info(f"🔄 [IP_UPDATE] Attempt {attempt + 1}/{max_retries}")
                ip_address = vault_manager.get_vm_ipv4_ip(
                    environment=vm_details["environment"],
                    os_type=vm_details["os_type"],
                    vm_name_prefix=vm_details["vm_name_prefix"]
                )

                if ip_address:
                    break

            # 4. 更新資料庫
            if ip_address:
                logger.info(f"💾 [IP_UPDATE] Updating DB with IP={ip_address}")

                # 🔄 重新獲取 vm_config_id 以確保使用最新的記錄
                try:
                    vm_config_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)
                    logger.info(f"🔄 [IP_UPDATE] Re-fetched vm_config_id={vm_config_id}")
                except Exception as e:
                    logger.error(f"❌ [IP_UPDATE] Failed to re-fetch vm_config_id: {e}")
                    set_failed_message(db_conn, workflow_id, "DB_VM_ID", f"Failed to re-fetch vm_config_id: {e}")
                    return False

                if update_vm_ipv4_ip(vm_config_id, ip_address):
                    logger.info(f"✅ [IP_UPDATE] Successfully updated IP for workflow {workflow_id}")
                    return True
                else:
                    logger.error(f"❌ [IP_UPDATE] Failed to update DB")
                    set_failed_message(db_conn, workflow_id, "DB_IP", "Failed to update VM IPv4 IP in database.")
                    return False
            else:
                vault_path = f"secret/{vm_details['environment']}-{vm_details['os_type']}/{vm_details['vm_name_prefix']}"
                logger.error(f"❌ [IP_UPDATE] Failed after {max_retries} attempts. Path: {vault_path}")
                set_failed_message(db_conn, workflow_id, "VAULT_IP", f"Failed to retrieve IP after {max_retries} attempts.")
                return False
        
        except Exception as e:
            logger.error(f"❌ [IP_UPDATE] Exception: {e}")
            import traceback
            logger.error(traceback.format_exc())
            set_failed_message(db_conn, workflow_id, "IP_UPDATE", f"Error during IP update: {str(e)}")
            return False
    
    finally:
        # 釋放鎖
        if lock_acquired:
            try:
                lock_name = f"ip_update_{workflow_id}"
                lock_cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                lock_cur.fetchone()
                lock_cur.fetchall()
                logger.info(f"🔓 [IP_UNLOCK] Released IP update lock for workflow {workflow_id}")
            except Exception as e:
                logger.error(f"❌ Failed to release IP update lock for workflow {workflow_id}: {e}")

        lock_cur.close()

# ---------- Disk helpers ----------
def _get_vm_config_id_by_workflow(db_conn, workflow_id: int) -> int:
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
        if not row or not row.get("request_payload"):
            raise ValueError("No request_payload")

        payload = json.loads(row["request_payload"])
        action_type = payload.get("action_type", "create")

        # 根據 action_type 決定從哪裡取資料
        if action_type == "update":
            form_data = payload.get("new_config", payload)
        elif action_type == "delete":
            form_data = payload.get("original_config", payload)
            environment_value = (form_data.get("environment") or "").strip()
            vm_name_prefix_value = (form_data.get("vm_name_prefix") or "").strip()
            if not environment_value or not vm_name_prefix_value:
                logger.info(f"🗑️ [VM_CONFIG] Workflow {workflow_id} is DELETE operation with missing VM info, returning dummy ID")
                return 0  # 返回特殊值 0 表示這是 DELETE 操作但缺少完整資訊
        else:
            # create 或其他操作
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

# def _acquire_batch_lock(cur, workflow_id: int, timeout_sec: int = 10) -> bool:
#     lock_name = f"ensure_disks_{workflow_id}"
#     cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, timeout_sec))
#     got = cur.fetchone()
#     return bool(got and list(got.values())[0] == 1)

# def _release_batch_lock(cur, workflow_id: int) -> None:
#     lock_name = f"ensure_disks_{workflow_id}"
#     try:
#         cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
#     except Exception:
#         pass

def _normalize_prov(v: str) -> str:
    v = (v or "").strip().lower()
    if v in ("thin", "thick_lazy", "thick_eager"):
        return v
    return "thin"

def ensure_disks_after_success(db_conn, workflow_id: int) -> None:
    logger.info(f"🔧 [DISK_DEBUG] Starting ensure_disks_after_success for workflow {workflow_id}")
    """
    執行 pipeline 成功後的磁碟處理（含 Create/Resize/Delete）：
      - PENDING_CREATION → CREATING → SUCCESS/FAILED
      - PENDING_RESIZE   → RESIZING → SUCCESS/FAILED
      - PENDING_DELETE   → DELETING → row deleted / FAILED
    """

    # 首先檢查是否為 DELETE 操作
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
        if row and row.get("request_payload"):
            try:
                payload = json.loads(row["request_payload"])
                action_type = payload.get("action_type", "create")

                # 如果是 DELETE 操作，直接返回，不進行後續處理
                if action_type == "delete":
                    logger.info(f"🗑️ [DISK_SKIP] Workflow {workflow_id} is DELETE operation, skipping disk operations")
                    return
            except json.JSONDecodeError:
                logging.warning(f"⚠️ Invalid JSON in request_payload for workflow {workflow_id}")
    except Exception as e:
        logging.warning(f"⚠️ Failed to check action_type in ensure_disks_after_success for workflow {workflow_id}: {e}")
    finally:
        cur.close()

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
            logger.info(f"🧷 Skip ensure_disks_after_success for workflow {workflow_id}: lock busy")
            return

        lock_acquired = True

        try:
            vm_configuration_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)
            vm_name_prefix = _get_vm_name_prefix_by_config_id(db_conn, vm_configuration_id)

            # ---------- 1) CREATE ----------
            create_cur = db_conn.cursor(dictionary=True)
            try:
                create_cur.execute(
                    """
                    SELECT id, size, disk_provisioning
                      FROM vm_disks
                     WHERE vm_configuration_id = %s
                       AND status = 'PENDING_CREATION'
                     ORDER BY id ASC
                    """,
                    (vm_configuration_id,),
                )
                pending_creates = create_cur.fetchall()
            finally:
                create_cur.close()

            for disk_row in (pending_creates or []):
                disk_id = disk_row["id"]
                size_gb = int(disk_row["size"])
                provisioning = _normalize_prov(disk_row.get("disk_provisioning"))

                # claim
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
                    # 呼叫 vSphere 新增磁碟
                    result = add_disk_to_vm(vm_name_prefix, {
                        "size_gb": size_gb,
                        "provision_type": provisioning
                    })

                    scsi_controller = result.get("controller_bus")
                    unit_number     = result.get("unit_number")
                    label_number    = result.get("label_number")
                    vmdk_path       = result.get("vmdk_path")

                    ok_cur = db_conn.cursor()
                    try:
                        ok_cur.execute(
                            """
                            UPDATE vm_disks
                               SET scsi_controller = %s,
                                   unit_number     = %s,
                                   label           = %s,
                                   vmdk_path       = %s,
                                   status          = 'SUCCESS',
                                   updated_at      = NOW()
                             WHERE id = %s
                               AND status = 'CREATING'
                            """,
                            (scsi_controller, unit_number, label_number, vmdk_path, disk_id)
                        )
                        db_conn.commit()
                    finally:
                        ok_cur.close()

                    logger.info(f"✅ Disk #{disk_id} created at scsi({scsi_controller}:{unit_number}) label=Hard disk {label_number}")

                except Exception as e:
                    logger.error(f"❌ Failed to create disk #{disk_id} for VM '{vm_name_prefix}': {e}")
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
                        logger.error(f"❌ Also failed to mark disk #{disk_id} as FAILED: {write_err}")

                    set_failed_message(db_conn, workflow_id, f"DISK:{disk_id}", f"Create disk failed: {str(e)}")

            # ---------- 2) RESIZE ----------
            resize_cur = db_conn.cursor(dictionary=True)
            try:
                resize_cur.execute(
                    """
                    SELECT id, size AS new_size, scsi_controller, unit_number, label, vmdk_path, status
                      FROM vm_disks
                     WHERE vm_configuration_id = %s
                       AND status IN ('PENDING_RESIZE', 'RESIZING')
                     ORDER BY id ASC
                    """,
                    (vm_configuration_id,),
                )
                pending_resizes = resize_cur.fetchall()
            finally:
                resize_cur.close()

            for disk_row in (pending_resizes or []):
                disk_id = disk_row["id"]
                new_size_gb = int(disk_row["new_size"])
                scsi_controller = disk_row.get("scsi_controller")
                unit_number = disk_row.get("unit_number")
                label = disk_row.get("label")
                vmdk_path = disk_row.get("vmdk_path")

                # 檢查目前狀態，決定是否需要 claim
                current_status = disk_row.get("status")  # 需要在查詢中加入 status 欄位

                if current_status == 'PENDING_RESIZE':
                    # claim
                    claim_cur = db_conn.cursor()
                    try:
                        claim_cur.execute(
                            """
                            UPDATE vm_disks
                               SET status='RESIZING',
                                   updated_at=NOW()
                             WHERE id=%s
                               AND status='PENDING_RESIZE'
                            """,
                            (disk_id,)
                        )
                        db_conn.commit()
                        if claim_cur.rowcount != 1:
                            continue
                    finally:
                        claim_cur.close()

                try:
                    # 從 vSphere 查詢當前磁碟狀態來取得 disk_key
                    current_disks = get_vm_disks(vm_name_prefix)

                    target_disk_key = None
                    for disk in current_disks:
                        if (disk.get("controller_bus") == scsi_controller and 
                            disk.get("unit_number") == unit_number):
                            target_disk_key = disk.get("key")
                            break

                    if not target_disk_key:
                        raise ValueError(f"Cannot find disk_key for scsi({scsi_controller}:{unit_number})")

                    logger.info(f"About to call update_disk_size with vm_name={vm_name_prefix}, disk_key={target_disk_key}, new_size_gb={new_size_gb}")
                    logger.info(f"update_disk_size function: {update_disk_size}")

                    # 呼叫 vSphere 調整大小
                    result = update_disk_size(vm_name_prefix, target_disk_key, new_size_gb)
                    logger.info(f"update_disk_size result: {result}")

                    # 更新資料庫狀態為成功
                    logger.info(f"Now updating database status for disk #{disk_id} from RESIZING to SUCCESS")
                    ok_cur = db_conn.cursor()
                    try:
                        ok_cur.execute(
                            """
                            UPDATE vm_disks
                               SET status='SUCCESS',
                                   updated_at=NOW()
                             WHERE id = %s
                               AND status = 'RESIZING'
                            """,
                            (disk_id,)
                        )
                        db_conn.commit()
                    finally:
                        ok_cur.close()

                    logger.info(f"✅ Disk #{disk_id} resized to {new_size_gb} GB")

                except Exception as e:
                    logger.error(f"❌ Failed to resize disk #{disk_id} for VM '{vm_name_prefix}': {e}")
                    logger.error(f"Exception type: {type(e)}")
                    logger.error(f"Error args: {e.args}")

                    # 更新資料庫狀態為失敗
                    try:
                        fail_cur = db_conn.cursor()
                        try:
                            fail_cur.execute(
                                """
                                UPDATE vm_disks
                                   SET status='FAILED',
                                       updated_at=NOW()
                                 WHERE id=%s
                                   AND status IN ('PENDING_RESIZE','RESIZING')
                                """,
                                (disk_id,)
                            )
                            db_conn.commit()
                        finally:
                            fail_cur.close()
                    except Exception as write_err:
                        logger.error(f"❌ Also failed to mark disk #{disk_id} as FAILED: {write_err}")

                    set_failed_message(db_conn, workflow_id, f"DISK:{disk_id}", f"Resize disk failed: {str(e)}")

            # ---------- 3) DELETE ----------
            delete_cur = db_conn.cursor(dictionary=True)
            try:
                delete_cur.execute(
                    """
                    SELECT id, scsi_controller, unit_number, label, vmdk_path, status
                      FROM vm_disks
                     WHERE vm_configuration_id = %s
                       AND status = 'PENDING_DELETE'
                     ORDER BY id ASC
                    """,
                    (vm_configuration_id,),
                )
                pending_deletes = delete_cur.fetchall()
            finally:
                delete_cur.close()

            for disk_row in (pending_deletes or []):
                disk_id = disk_row["id"]
                scsi_controller = disk_row.get("scsi_controller")
                unit_number = disk_row.get("unit_number")
                label = disk_row.get("label")
                vmdk_path = disk_row.get("vmdk_path")
                current_status = disk_row.get("status")

                if current_status == 'PENDING_DELETE':
                    # claim
                    claim_cur = db_conn.cursor()
                    try:
                        claim_cur.execute(
                            """
                            UPDATE vm_disks
                               SET status='DELETING',
                                   updated_at=NOW()
                             WHERE id=%s
                               AND status='PENDING_DELETE'
                            """,
                            (disk_id,)
                        )
                        db_conn.commit()
                        if claim_cur.rowcount != 1:
                            continue
                    finally:
                        claim_cur.close()

                try:
                    disk_already_removed = False

                    # 從 vSphere 查詢當前磁碟狀態來取得 disk_key
                    current_disks = get_vm_disks(vm_name_prefix)

                    target_disk_key = None
                    for disk in current_disks:
                        if (disk.get("controller_bus") == scsi_controller and
                            disk.get("unit_number") == unit_number):
                            target_disk_key = disk.get("key")
                            break

                    if not target_disk_key:
                        # 檢查是否為 NULL 值的情況
                        if scsi_controller is None or unit_number is None:
                            logging.warning(f"Disk #{disk_id} has NULL scsi_controller({scsi_controller}) or unit_number({unit_number}), marking as deleted")
                            ok_cur = db_conn.cursor()
                            try:
                                ok_cur.execute("DELETE FROM vm_disks WHERE id = %s", (disk_id,))
                                db_conn.commit()
                                logger.info(f"✅ Disk #{disk_id} record deleted (NULL values)")
                            finally:
                                ok_cur.close()
                            disk_already_removed = True
                        else:
                            # 磁碟可能已被手動刪除
                            logging.warning(f"Disk #{disk_id} not found in vSphere at scsi({scsi_controller}:{unit_number}), assuming already deleted")
                            ok_cur = db_conn.cursor()
                            try:
                                ok_cur.execute("DELETE FROM vm_disks WHERE id = %s", (disk_id,))
                                db_conn.commit()
                                logger.info(f"✅ Disk #{disk_id} record cleaned up (not found in vSphere)")
                            finally:
                                ok_cur.close()
                            disk_already_removed = True

                    # 如果磁碟還存在於 vSphere，才呼叫刪除
                    if not disk_already_removed:
                        remove_disk_from_vm(vm_name_prefix, target_disk_key)
                        ok_cur = db_conn.cursor()
                        try:
                            ok_cur.execute("DELETE FROM vm_disks WHERE id = %s", (disk_id,))
                            db_conn.commit()
                            logger.info(f"✅ Disk #{disk_id} database record deleted")
                        finally:
                            ok_cur.close()

                    # 無論如何都要同步 label
                    try:
                        updated_disks = get_vm_disks(vm_name_prefix)
                        sync_data = []
                        for disk in updated_disks:
                            label_text = disk.get("label", "")
                            label_number = None

                            # 解析 "Hard disk 2" label nubmer
                            if label_text and isinstance(label_text, str):
                                try:
                                    label_number = int(label_text.replace("Hard disk ", "").strip())
                                except (ValueError, AttributeError):
                                    logging.warning(f"Failed to parse label: {label_text}")

                            sync_data.append({
                                "controller_bus": disk.get("controller_bus"),
                                "unit_number": disk.get("unit_number"),
                                "label_text": label_text,
                                "label_number": label_number,
                            })
                        sync_disk_labels_to_database(vm_name_prefix, sync_data)
                        logger.info(f"✅ Synced labels after deleting disk #{disk_id}")
                    except Exception as sync_err:
                        logging.warning(f"⚠️ Failed to sync labels after delete: {sync_err}")

                    logger.info(f"✅ Disk #{disk_id} deletion process completed")

                except Exception as e:
                    logger.error(f"❌ Failed to delete disk #{disk_id} for VM '{vm_name_prefix}': {e}")
                    try:
                        fail_cur = db_conn.cursor()
                        try:
                            fail_cur.execute(
                                """
                                UPDATE vm_disks
                                   SET status='FAILED',
                                       updated_at=NOW()
                                 WHERE id=%s
                                   AND status IN ('PENDING_DELETE','DELETING')
                                """,
                                (disk_id,)
                            )
                            db_conn.commit()
                        finally:
                            fail_cur.close()
                    except Exception as write_err:
                        logger.error(f"❌ Also failed to mark disk #{disk_id} as FAILED: {write_err}")

                    set_failed_message(db_conn, workflow_id, f"DISK:{disk_id}", f"Delete disk failed: {str(e)}")

        except Exception as e:
            logger.error(f"❌ ensure_disks_after_success error for workflow {workflow_id}: {e}")
            set_failed_message(db_conn, workflow_id, "DISK", f"Batch disk ops failed: {str(e)}")

    finally:
        # 釋放鎖
        if lock_acquired:
            try:
                lock_name = f"ensure_disks_{workflow_id}"
                lock_cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                lock_cur.fetchone()  # 讀取結果
                lock_cur.fetchall()  # 清空剩餘結果
            except Exception as e:
                logger.error(f"❌ Failed to release lock for workflow {workflow_id}: {e}")

        lock_cur.close()

def _summarize_disk_batch_state(db_conn, workflow_id: int):
    """
    回傳 (vm_configuration_id, counts)
    counts 是 dict：包含 pending / working / final 狀態
    """
    vm_configuration_id = _get_vm_config_id_by_workflow(db_conn, workflow_id)

    # 加入這段代碼：
    # 如果配置 ID 為 0（表示 DELETE 操作且缺少完整資訊），直接返回空計數
    if vm_configuration_id == 0:
        empty_counts = {
            'PENDING_CREATION': 0, 'CREATING': 0,
            'PENDING_RESIZE': 0, 'RESIZING': 0,
            'PENDING_DELETE': 0, 'DELETING': 0,
            'SUCCESS': 0, 'FAILED': 0
        }
        return vm_configuration_id, empty_counts

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
        counts = {
            'PENDING_CREATION': 0,
            'CREATING': 0,
            'PENDING_RESIZE': 0,
            'RESIZING': 0,
            'PENDING_DELETE': 0,
            'DELETING': 0,
            'SUCCESS': 0,
            'FAILED': 0
        }
        for r in rows:
            s = (r['status'] or '').upper()
            if s in counts:
                counts[s] = int(r['cnt'])
        return vm_configuration_id, counts
    finally:
        cur.close()

# ---------- 統一查詢工作流狀態 + 磁碟狀態 ----------
def get_workflow_state_info(db_conn, workflow_id: int) -> dict:
    try:
        # 1. Pipeline 狀態
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

        # 2. 磁碟狀態
        vmc_id, actual_counts = _summarize_disk_batch_state(db_conn, workflow_id)

        # 3. 有些 pipeline 終態可忽略 pending（例如失敗/取消）
        effective_counts = actual_counts.copy()
        if pipeline_status in ("failed", "canceled"):
            for k in ("PENDING_CREATION","CREATING","PENDING_RESIZE","RESIZING","PENDING_DELETE","DELETING"):
                effective_counts[k] = 0
            logger.info(f"🚫 Pipeline {pipeline_status} for workflow {workflow_id}, ignoring pending jobs")

        return {
            'vm_configuration_id': vmc_id,
            'actual_counts': actual_counts,
            'effective_counts': effective_counts,
            'pipeline_status': pipeline_status
        }

    except Exception as e:
        logger.error(f"❌ Error getting workflow state info for workflow {workflow_id}: {e}")
        return {
            'vm_configuration_id': 0,
            'actual_counts': {k:0 for k in ('PENDING_CREATION','CREATING','PENDING_RESIZE','RESIZING','PENDING_DELETE','DELETING','SUCCESS','FAILED')},
            'effective_counts': {k:0 for k in ('PENDING_CREATION','CREATING','PENDING_RESIZE','RESIZING','PENDING_DELETE','DELETING','SUCCESS','FAILED')},
            'pipeline_status': 'unknown'
        }

# ---------- 決定 Workflow 目標狀態 ----------
def determine_workflow_status_after_pipeline(db_conn, workflow_id: int, pipeline_status: str) -> str:
    """
    根據 pipeline_status 和建立額外硬碟狀態回傳 workflow 狀態
    """
    ps = (pipeline_status or "").strip().lower()
    with log_context(component="pipeline", workflow_id=str(workflow_id), trace_id=new_trace_id()):
        logger.info("Determining status for workflow %s, pipeline_status=%s", workflow_id, pipeline_status, stacklevel=2)


    # 先檢查 workflow 當前狀態，如果已經是 RETURNED，保持不變
    status_result = get_workflow_status(workflow_id)
    if status_result.get("success"):
        current_status = (status_result.get("status") or "").upper()
        if current_status == "RETURNED":
            logger.info(f"⭐️ Workflow {workflow_id} is RETURNED, preserving status despite pipeline={ps}")
            return "RETURNED"
    else:
        logging.warning(f"⚠️ Failed to get status for workflow {workflow_id}: {status_result.get('error')}")

    # 判斷 gitlab pipeline 狀態來返回 workflow 狀態
    if ps == "failed":
        return "FAILED"
    elif ps == "canceled":
        return "CANCELED"
    elif ps == "manual":
        return "PENDING_APPROVAL"
    elif ps == "success":
        # 檢查 action_type
        cur = db_conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
            row = cur.fetchone()
            if row and row.get("request_payload"):
                payload = json.loads(row["request_payload"])
                action_type = payload.get("action_type", "create")

                #  如果 action_type 為 delete 時跳過檢查硬碟建立邏輯直接返回 SUCCESS
                if action_type == "delete":
                    logger.info(f"🗑️ Workflow {workflow_id} is DELETE operation, returning SUCCESS directly")
                    return "SUCCESS"
        except Exception as e:
            logging.warning(f"⚠️ Failed to check action_type for workflow {workflow_id}: {e}")
        finally:
            cur.close()

        # action_type 為 delete 以外的，進入檢查硬碟建立邏輯
        state_info = get_workflow_state_info(db_conn, workflow_id)
        c = state_info['effective_counts']

        pending = (
            c['PENDING_CREATION'] + c['CREATING'] +
            c['PENDING_RESIZE']   + c['RESIZING'] +
            c['PENDING_DELETE']   + c['DELETING']
        )
        failed = c['FAILED']

        logger.info(f"📊 Workflow {workflow_id} effective disk counts: {c}")
        logger.info(f"📊 Total pending disks: {pending}, failed: {failed}")

        # 檢查磁碟統計狀態，如有 pending → 回傳 DEPLOYING，沒有 pending → 回傳 SUCCESS
        if failed > 0:
            return "FAILED"
        elif pending > 0:
            logger.info(f"📊 Workflow {workflow_id} has {pending} pending disks, returning DEPLOYING")
            return "DEPLOYING"
        else:
            return "SUCCESS"

    # pipeline_status 在 running/pending/created/scheduled/... → 回傳 IN_PROGRESS
    elif ps in {"running", "pending", "created", "scheduled", "preparing", "waiting_for_resource"}:
        return "IN_PROGRESS"
    elif ps == "skipped":
        return "CANCELED"

    logging.warning(f"⚠️ Unrecognized pipeline status '{ps}' for workflow {workflow_id}, defaulting to IN_PROGRESS")
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

# ---------- 處理 workflow 狀態更新 ----------
# ---------- 處理 workflow 狀態更新 ----------
def process_workflow_status_update(db_conn, workflow_id: int, target_status: str, pipeline_id: int) -> None:
    """
    db workflow_run.status 更新及 Jira / disk 處理
    """
    lock_cur = db_conn.cursor(dictionary=True)
    lock_acquired = False
    
    try:
        # 獲取鎖
        lock_name = f"process_workflow_{workflow_id}"
        lock_cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
        lock_result = lock_cur.fetchone()
        lock_cur.fetchall()
        
        if not (lock_result and list(lock_result.values())[0] == 1):
            logger.info(f"⏭️ [SKIP_LOCK] Workflow {workflow_id} is being processed, skipping")
            return
        
        lock_acquired = True
        logger.info(f"🔒 [PROCESS_LOCK] Acquired processing lock for workflow {workflow_id}")
        
        # ✅ 優化：一次查詢同時取得 status 和 request_payload
        cur_check = db_conn.cursor(dictionary=True)
        try:
            cur_check.execute(
                "SELECT status, request_payload FROM workflow_runs WHERE workflow_id = %s",
                (workflow_id,)
            )
            row = cur_check.fetchone()
            
            if not row:
                logging.warning(f"⚠️ Workflow {workflow_id} not found in database")
                return
            
            current_status = (row.get("status") or "").upper()
            request_payload = row.get("request_payload")
            
            # 如果當前狀態 == 目標狀態，直接返回
            if current_status == target_status.upper():
                logger.info(f"⏭️ [SKIP_DUPLICATE] Workflow {workflow_id} already in {target_status}")
                return
                
            # 如果已經是終態，不允許修改
            final_states = ("SUCCESS", "FAILED", "CANCELED", "RETURNED")
            if current_status in final_states:
                logger.info(f"⏭️ [SKIP_FINAL] Workflow {workflow_id} is in final state {current_status}")
                return
        finally:
            cur_check.close()

        # 處理 SUCCESS 狀態
        if target_status == "SUCCESS":
            if not request_payload:
                logging.warning(f"⚠️ No request_payload for workflow {workflow_id}")
                return
            
            try:
                payload = json.loads(request_payload)
                action_type = payload.get("action_type", "create").lower()
            except json.JSONDecodeError as e:
                logger.error(f"❌ Invalid JSON in request_payload for workflow {workflow_id}: {e}")
                return

            # DELETE 操作特殊處理
            if action_type == "delete":
                logger.info(f"🗑️ DELETE operation for workflow {workflow_id}")
                delete_vm_from_database(workflow_id)
                ensure_jira_after_success(db_conn, workflow_id)
                update_request_status(workflow_id, target_status)
                return

            # CREATE/UPDATE 操作: IP → Disk → Jira
            if action_type in ("create", "update"):
                update_vm_ip_from_vault(db_conn, workflow_id)

            ensure_disks_after_success(db_conn, workflow_id)
            ensure_jira_after_success(db_conn, workflow_id)

        # pipeline success 但仍有 pending disk
        elif target_status == "DEPLOYING":
            logger.info(f"Workflow {workflow_id} marked as DEPLOYING, waiting for pipeline to complete before disk operations")

        elif target_status in ("FAILED", "CANCELED"):
            set_failed_message(db_conn, workflow_id, "GITLAB", f"Pipeline {pipeline_id} status is {target_status.lower()}")

        update_request_status(workflow_id, target_status)
        logger.info(f"✅ Updated workflow {workflow_id} status to {target_status}")

    except Exception as e:
        logger.error(f"❌ Error processing workflow {workflow_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # 釋放鎖
        if lock_acquired:
            try:
                lock_name = f"process_workflow_{workflow_id}"
                lock_cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                lock_cur.fetchone()
                lock_cur.fetchall()
                logger.info(f"🔓 [PROCESS_UNLOCK] Released processing lock for workflow {workflow_id}")
            except Exception as e:
                logger.error(f"❌ Failed to release processing lock for workflow {workflow_id}: {e}")
        
        lock_cur.close()

# ---------- Monitor pipelines ----------
# 檔案: pipeline_monitor.py
# 函式: monitor_pipelines

def monitor_pipelines(app):
    with app.app_context():
        while True:
            print("\n🚀 Start monitoring GitLab pipelines...")

            db_conn = None
            try:
                db_conn = get_db_connection()

                cur = db_conn.cursor(dictionary=True)
                try:
                    cur.execute(
                        """
                        SELECT workflow_id, status
                          FROM workflow_runs
                         WHERE status NOT IN ('SUCCESS','FAILED','CANCELED','RETURNED')
                           AND created_at >= NOW() - INTERVAL 7 DAY
                         ORDER BY workflow_id DESC
                        """
                    )
                    workflows_todo = cur.fetchall()
                finally:
                    cur.close()

                # 另外把「gitlab_pipelines 還缺 finished_at 或 duration」的 workflow 也抓進來
                cur2 = db_conn.cursor(dictionary=True)
                try:
                    cur2.execute(
                        """
                        SELECT DISTINCT gp.workflow_id
                          FROM gitlab_pipelines gp
                          JOIN workflow_runs wr ON gp.workflow_id = wr.workflow_id
                         WHERE (gp.finished_at IS NULL OR gp.duration IS NULL)
                           AND gp.started_at >= NOW() - INTERVAL 7 DAY
                           AND wr.status NOT IN ('SUCCESS','FAILED','CANCELED','RETURNED')
                         ORDER BY gp.workflow_id DESC
                        """
                    )
                    missing_fd = cur2.fetchall()
                finally:
                    cur2.close()

                # 合併：未終態 + 缺欄位 的 workflow
                wf_map = {w['workflow_id']: w for w in (workflows_todo or [])}
                for r in (missing_fd or []):
                    wid = r['workflow_id']
                    if wid not in wf_map:
                        # 若 workflow_runs 此刻已是 SUCCESS，這裡用一個占位 status，不影響後續 GitLab 輪詢
                        wf_map[wid] = {'workflow_id': wid, 'status': 'SUCCESS'}
                workflows_todo = list(wf_map.values())

                for wr in workflows_todo:
                    workflow_id = wr["workflow_id"]
                    current_status = wr["status"]

                    # 🔐 重新從資料庫確認最新狀態，避免處理已經變成終態的 workflow
                    cur_check = db_conn.cursor(dictionary=True)
                    try:
                        cur_check.execute(
                            "SELECT status FROM workflow_runs WHERE workflow_id = %s",
                            (workflow_id,)
                        )
                        check_row = cur_check.fetchone()
                        if check_row:
                            latest_status = (check_row.get("status") or "").upper()
                            if latest_status in ("SUCCESS", "FAILED", "CANCELED", "RETURNED"):
                                print(f"⏭️ [skip] wf={workflow_id} is in final state {latest_status}, skipping")
                                continue
                            current_status = latest_status
                        else:
                            print(f"⚠️ [skip] wf={workflow_id} not found in database")
                            continue
                    finally:
                        cur_check.close()

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
                        pipeline_row = cur_p.fetchone()
                    finally:
                        cur_p.close()

                    if not pipeline_row:
                        continue

                    pipeline_id = pipeline_row["pipeline_id"]
                    db_status = (pipeline_row.get("status") or "").lower()

                    # 特殊處理 DEPLOYING 狀態
                    if current_status == "DEPLOYING":
                        # 檢查是否為 DELETE 操作
                        cur_check = db_conn.cursor(dictionary=True)
                        try:
                            cur_check.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                            check_row = cur_check.fetchone()
                            is_delete = False
                            if check_row and check_row.get("request_payload"):
                                check_payload = json.loads(check_row["request_payload"])
                                check_action = check_payload.get("action_type", "create")
                                is_delete = (check_action == "delete")
                        finally:
                            cur_check.close()

                        # DELETE 操作：直接標記為 SUCCESS，不處理磁碟
                        if is_delete:
                            print(f"[delete_check] wf={workflow_id} is DELETE operation, checking pipeline status first")

                            # 先確認 pipeline 狀態
                            try:
                                fresh_status = _refresh_pipeline_from_gitlab(db_conn, pipeline_id)
                                print(f"[pipeline_refresh] wf={workflow_id} fresh_status={fresh_status}")

                                # 優先檢查 pipeline 是否失敗或取消
                                if fresh_status in ("failed", "canceled"):
                                    target_status = "FAILED" if fresh_status == "failed" else "CANCELED"
                                    update_request_status(workflow_id, target_status)
                                    print(f"[pipeline_terminated] wf={workflow_id} pipeline {fresh_status}, marking as {target_status}")
                                    continue  # ← Pipeline 失敗/取消後跳到下一個 workflow

                                # 如果 pipeline 還沒成功，等待
                                if fresh_status != "success":
                                    print(f"[delete_wait] wf={workflow_id} pipeline not yet success ({fresh_status}), waiting...")
                                    continue  # ← Pipeline 未完成，跳到下一個 workflow

                            except Exception as refresh_err:
                                print(f"[refresh_error] wf={workflow_id} pipeline refresh failed: {refresh_err}")
                                continue  # ← 查詢失敗，跳到下一個 workflow

                            # 只有 pipeline 成功後才執行刪除
                            print(f"[delete_execute] wf={workflow_id} pipeline success, proceeding with deletion")

                            # 刪除資料庫中的 VM 記錄
                            try:
                                delete_success = delete_vm_from_database(workflow_id)
                                if delete_success:
                                    print(f"[delete_db] wf={workflow_id} successfully deleted VM from database")
                                else:
                                    print(f"[delete_db_warn] wf={workflow_id} failed to delete VM from database")
                                    logging.warning(f"Failed to delete VM from database for workflow {workflow_id}")
                                    # 資料庫刪除失敗，標記為 FAILED
                                    update_request_status(workflow_id, "FAILED")
                                    set_failed_message(db_conn, workflow_id, "DB_DELETE", "Failed to delete VM from database")
                                    continue
                            except Exception as del_err:
                                print(f"[delete_db_error] wf={workflow_id} error deleting VM: {del_err}")
                                logger.error(f"Error deleting VM from database for workflow {workflow_id}: {del_err}")
                                update_request_status(workflow_id, "FAILED")
                                set_failed_message(db_conn, workflow_id, "DB_DELETE", f"Error deleting VM: {str(del_err)}")
                                continue

                            update_request_status(workflow_id, "SUCCESS")
                            ensure_jira_after_success(db_conn, workflow_id)
                            print(f"[delete_complete] wf={workflow_id} DELETE completed, moving to SUCCESS")
                            continue  # ← DELETE 完成後跳到下一個 workflow

                        # 非 DELETE 操作：正常處理磁碟
                        print(f"[disk_check] wf={workflow_id} processing pending disks")
                        # 確認 pipeline 狀態
                        try:
                            fresh_status = _refresh_pipeline_from_gitlab(db_conn, pipeline_id)
                            print(f"[pipeline_refresh] wf={workflow_id} fresh_status={fresh_status}")

                            # 優先檢查 pipeline 是否失敗或取消
                            if fresh_status in ("failed", "canceled"):
                                target_status = "FAILED" if fresh_status == "failed" else "CANCELED"
                                update_request_status(workflow_id, target_status)
                                print(f"[pipeline_terminated] wf={workflow_id} pipeline {fresh_status}, marking as {target_status}")
                                continue  # ← Pipeline 失敗/取消後跳到下一個 workflow

                            # 如果 pipeline 還沒成功，等待
                            if fresh_status != "success":
                                print(f"[disk_wait] wf={workflow_id} pipeline not yet success ({fresh_status}), waiting...")
                                continue  # ← Pipeline 未完成，跳到下一個 workflow

                        except Exception as refresh_err:
                            print(f"[refresh_error] wf={workflow_id} pipeline refresh failed: {refresh_err}")
                            continue  # ← 查詢失敗，跳到下一個 workflow

                        # 只有 pipeline 成功後才執行磁碟操作
                        try:
                            ensure_disks_after_success(db_conn, workflow_id)
                        except Exception as disk_err:
                            print(f"[disk_error] wf={workflow_id} ensure_disks_after_success failed: {disk_err}")
                            logger.error(f"ensure_disks_after_success error for workflow {workflow_id}: {disk_err}")

                        # 取得磁碟狀態
                        try:
                            state_info = get_workflow_state_info(db_conn, workflow_id)
                            c = state_info['effective_counts']
                            print(f"[disk_state] wf={workflow_id} effective_counts={c}")
                        except Exception as state_err:
                            print(f"[state_error] wf={workflow_id} get_workflow_state_info failed: {state_err}")
                            logger.error(f"get_workflow_state_info error for workflow {workflow_id}: {state_err}")
                            continue  # ← 無法取得狀態，跳到下一個 workflow

                        pending_disks = (
                            c['PENDING_CREATION'] + c['CREATING'] +
                            c['PENDING_RESIZE']   + c['RESIZING'] +
                            c['PENDING_DELETE']   + c['DELETING']
                        )
                        print(f"[disk_count] wf={workflow_id} pending_disks={pending_disks}, failed={c['FAILED']}")

                        # 判斷是否所有磁碟都完成
                        if pending_disks == 0 and c['FAILED'] == 0:
                            if fresh_status == "success":
                                process_workflow_status_update(db_conn, workflow_id, "SUCCESS", pipeline_id)
                                print(f"[disk_complete] wf={workflow_id} all disks done, moving to SUCCESS")
                                continue  # 處理完就跳出
                            else:
                                print(f"[disk_complete] wf={workflow_id} disks done but pipeline state is {fresh_status}; remain DEPLOYING")
                                continue
                        elif c['FAILED'] > 0:
                            update_request_status(workflow_id, "FAILED")
                            print(f"[disk_failed] wf={workflow_id} has failed disks, marking as FAILED")
                            continue
                        else:
                            print(f"[disk_pending] wf={workflow_id} still has {pending_disks} pending disks")
                            continue

                    # 統一處理所有 GitLab 狀態查詢與更新邏輯 (主要監控邏輯)
                    # 注意：只有 current_status 不是 DEPLOYING 的 workflow 才會執行到這裡
                    print(f"🔍 [poll] wf={workflow_id} pid={pipeline_id} DB={db_status} → query GitLab")

                    gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)
                    if gitlab_result.get("success"):
                        # 無論狀態是否終態，都更新資料庫
                        update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)
                        fresh_status = (gitlab_result.get("status") or "").lower()
                        print(f"⚙️ [poll] wf={workflow_id} pid={pipeline_id} GitLab={fresh_status}")

                        # 從資料庫重新讀取 workflow 狀態，確保是最新的
                        cur_now = db_conn.cursor(dictionary=True)
                        try:
                            cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                            wr_now = cur_now.fetchone()
                        finally:
                            cur_now.close()

                        current_workflow_status = (wr_now.get("status") or "").upper() if wr_now else ""
                        target_status = determine_workflow_status_after_pipeline(db_conn, workflow_id, fresh_status)

                        if target_status is None:
                            logger.error(f"❗ determine_workflow_status_after_pipeline returned None (wf={workflow_id}, fresh_status={fresh_status})")

                        final_states = ("SUCCESS", "FAILED", "CANCELED", "RETURNED")
                        if current_workflow_status in final_states:
                            # 如果 workflow 已是終態，不要再更新或降級
                            print(f"⭐ [skip] wf={workflow_id} in final state {current_workflow_status}, skip status update")
                        elif current_workflow_status != (target_status or "IN_PROGRESS").upper():
                            print(f"📊 [status_change] wf={workflow_id} {current_workflow_status} → {target_status}")
                            process_workflow_status_update(db_conn, workflow_id, target_status, pipeline_id)
                        else:
                            print(f"⭐ [skip] wf={workflow_id} already in target status {target_status}")
                    else:
                        set_failed_message(
                            db_conn, workflow_id, "GITLAB_API",
                            f"Pipeline query failed: {gitlab_result.get('error')}",
                        )

                # 同步已終止 workflow 的 pipeline 狀態 (補充同步邏輯)
                try:
                    sync_cur = db_conn.cursor(dictionary=True)
                    try:
                        sync_cur.execute(
                            """
                            SELECT DISTINCT gp.workflow_id, gp.pipeline_id
                              FROM gitlab_pipelines gp
                              JOIN workflow_runs wr ON gp.workflow_id = wr.workflow_id
                             WHERE wr.status IN ('SUCCESS','FAILED','CANCELED','RETURNED')
                               AND (gp.finished_at IS NULL OR gp.duration IS NULL)
                               AND gp.updated_at >= NOW() - INTERVAL 1 HOUR
                             ORDER BY gp.workflow_id DESC
                             LIMIT 50
                            """
                        )
                        pipelines_to_sync = sync_cur.fetchall()
                    finally:
                        sync_cur.close()

                    for row in (pipelines_to_sync or []):
                        wf_id = row['workflow_id']
                        pipe_id = row['pipeline_id']

                        print(f"🔄 [sync_only] Syncing pipeline {pipe_id} for final workflow {wf_id}")

                        try:
                            gitlab_result = get_pipeline_status_from_gitlab(pipe_id)
                            if gitlab_result.get("success"):
                                # 只更新 gitlab_pipelines，不觸碰 workflow_runs
                                update_gitlab_pipeline_details(db_conn, pipe_id, gitlab_result)
                                print(f"✅ [sync_only] Updated pipeline {pipe_id} status to {gitlab_result.get('status')}")
                            else:
                                print(f"⚠️ [sync_only] Failed to sync pipeline {pipe_id}: {gitlab_result.get('error')}")
                        except Exception as sync_err:
                            print(f"❌ [sync_only] Error syncing pipeline {pipe_id}: {sync_err}")

                except Exception as sync_error:
                    print(f"❌ Pipeline sync error: {sync_error}")

            except Exception as e:
                print(f"❌ Pipeline monitoring error: {e}")
                import traceback
                print(f"❌ Full traceback: {traceback.format_exc()}")
            finally:
                if db_conn:
                    db_conn.close()

            time.sleep(30)

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
                    db_conn.close()

            time.sleep(60)

def start_monitor_thread(app):
    t1 = threading.Thread(target=monitor_pipelines, args=(app,), daemon=True)
    t1.start()
    print("✅ Pipeline Monitor Thread started")

    t2 = threading.Thread(target=monitor_workflows, args=(app,), daemon=True)
    t2.start()
    print("✅ Workflow Monitor Thread started")
