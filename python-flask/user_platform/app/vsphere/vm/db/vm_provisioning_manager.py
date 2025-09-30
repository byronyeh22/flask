# app/vsphere/vm/db/vm_provisioning_manager.py
import json
from mysql.connector import Error
import logging
from typing import Any, Dict, List, Tuple

from app.mysql.db import get_db_connection
# 後續仍會用到以維持原路由流程
from .workflow_manager import update_request_status

logger = logging.getLogger(__name__)


# =========================
# Helpers（沿用你的寫法）
# =========================
class _Helpers:
    @staticmethod
    def _first_scalar(value, default=None):
        if isinstance(value, list):
            return value[0] if value else default
        return value if value is not None else default

    @staticmethod
    def _as_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _to_int(value, default=0):
        scalar_value = _Helpers._first_scalar(value)
        try:
            return int(scalar_value)
        except (TypeError, ValueError):
            return default


# =========================
# Disk parsing（新流程版）
# =========================
def _normalize_provisioning(provisioning: str) -> str:
    """
    將 UI 字串歸一化為後端三種合法值：
      - 'thin'
      - 'thick_lazy'  （等同 'lazy', 'thick', 'lazy_zeroed'）
      - 'thick_eager' （等同 'eager', 'eager_zeroed'）
    其他狀況一律回退 'thin'
    """
    if not provisioning:
        return "thin"
    provisioning_lower = str(provisioning).strip().lower()
    if provisioning_lower in {"thin"}:
        return "thin"
    if provisioning_lower in {"thick_lazy", "lazy", "thick", "lazy_zeroed", "lazy-zeroed"}:
        return "thick_lazy"
    if provisioning_lower in {"thick_eager", "eager", "eager_zeroed", "eager-zeroed"}:
        return "thick_eager"
    return "thin"


def _extract_disks_for_create(form_data: Dict[str, Any]) -> List[Tuple[int, str]]:
    """
    從 Create 動作的表單萃取「額外磁碟」：(size_gb, provisioning)
    支援兩種來源：
      1) additional_disks = [ {size: 100, provisioning: "thin"}, ... ]
      2) 平行陣列 create_vm_disk_size[] / create_vm_disk_provisioning[]
    （注意：舊的 SCSI 欄位會被忽略，改為由後端自動分配）
    """
    disks: List[Tuple[int, str]] = []

    # 1) 結構化 additional_disks
    additional_disks = form_data.get("additional_disks")
    if isinstance(additional_disks, list) and additional_disks:
        for disk_entry in additional_disks:
            try:
                size_value = disk_entry.get("size")
                provisioning_value = disk_entry.get("provisioning", "thin")
                if size_value is None:
                    continue
                size_int = int(str(size_value).strip())
                if size_int <= 0:
                    continue
                disks.append((size_int, _normalize_provisioning(provisioning_value)))
            except Exception as error:
                logger.warning("Skip invalid disk in additional_disks: %s (err=%s)", disk_entry, error)

    # 2) 平行陣列（舊表單欄位）
    size_list = _Helpers._as_list(form_data.get("create_vm_disk_size[]"))
    provisioning_list = _Helpers._as_list(form_data.get("create_vm_disk_provisioning[]"))
    count = min(len(size_list), len(provisioning_list)) if size_list or provisioning_list else 0

    for index in range(count):
        try:
            size_raw = size_list[index]
            provisioning_raw = provisioning_list[index] if index < len(provisioning_list) else "thin"
            if size_raw is None or str(size_raw).strip() == "":
                continue
            size_int = int(str(size_raw).strip())
            if size_int <= 0:
                continue
            disks.append((size_int, _normalize_provisioning(provisioning_raw)))
        except Exception as error:
            logger.warning(
                "Skip invalid disk at index %s: size=%s provisioning=%s (err=%s)",
                index,
                size_list[index] if index < len(size_list) else None,
                provisioning_list[index] if index < len(provisioning_list) else None,
                error,
            )

    return disks


def _plan_disks_for_update(data: Dict[str, Any], db_disks: List[Dict[str, Any]]):
    """
    根據 Update 表單（new_config）與 DB 既有磁碟，規劃三個動作清單：
      - to_create: List[Tuple[int size_gb, str provisioning]]
      - to_update: List[Tuple[int disk_id, int size_gb, str provisioning, str status]]
                   ※ 相容性保留 provisioning 欄位，但「對既有磁碟」的 provisioning 變更已被忽略。
                      status 只會是：PENDING_RESIZE
      - to_delete: List[int disk_id]

    規則：
      * 表單的平行陣列為：
          - update_disk_db_id[]              （可能缺、或值為空字串，代表新磁碟）
          - update_vm_disk_size[]            （size <= 0 或空 → 略過該列）
          - update_vm_disk_provisioning[]    （新增磁碟時使用；既有磁碟的變更將被忽略）
      * 新增：disk_id 空且 size 有值 → to_create += (size, provisioning)
      * 更新：disk_id 存在且 DB 有該 id，且 size 與 DB 不同 → to_update += (id, new_size, <原樣帶或正規化的prov>, 'PENDING_RESIZE')
             （僅 provisioning 變更將被忽略，不產生任何任務）
      * 刪除：DB 既有 id 不在「表單有 size 的列」之中 → to_delete += id
    """
    # --- 1) 把 DB 磁碟做成 map 方便查 ---
    db_by_id: Dict[int, Dict[str, Any]] = {}
    for row in (db_disks or []):
        try:
            did = int(row["id"])
            db_by_id[did] = row
        except Exception:
            continue

    # --- 2) 取出表單平行陣列（支援有無 [] 的兩種 key） ---
    ids   = _Helpers._as_list(data.get("update_disk_db_id[]") or data.get("update_disk_db_id"))
    sizes = _Helpers._as_list(data.get("update_vm_disk_size[]") or data.get("update_vm_disk_size"))
    provs = _Helpers._as_list(data.get("update_vm_disk_provisioning[]") or data.get("update_vm_disk_provisioning"))

    n = max(len(ids), len(sizes), len(provs))
    to_create: List[Tuple[int, str]] = []
    to_update: List[Tuple[int, int, str, str]] = []
    form_present_ids: set[int] = set()

    def _parse_int(x):
        try:
            if x is None:
                return None
            s = str(x).strip()
            if s == "":
                return None
            return int(s)
        except Exception:
            return None

    for i in range(n):
        id_raw   = ids[i]   if i < len(ids)   else ""
        size_raw = sizes[i] if i < len(sizes) else ""
        prov_raw = provs[i] if i < len(provs) else "thin"

        # 沒 size 視為該列不存在（不在此處判斷刪除）
        size_int = _parse_int(size_raw)
        if size_int is None or size_int <= 0:
            continue

        prov_norm = _normalize_provisioning(prov_raw)

        # 有 id → 既有磁碟；無 id → 新增
        did = _parse_int(id_raw)
        if did is None:
            # Create：新增磁碟仍需帶 provisioning
            to_create.append((size_int, prov_norm))
        else:
            form_present_ids.add(did)
            orig = db_by_id.get(did)
            if not orig:
                logger.warning("Skip update for unknown disk id=%s (not found in DB).", did)
                continue

            orig_size = _parse_int(orig.get("size"))
            # 只比較 size；provisioning 改動一律忽略
            size_changed = (orig_size != size_int)

            if size_changed:
                # 相容性保留 prov_norm 欄位，但後續實際不會用到（只做 resize）
                to_update.append((did, size_int, prov_norm, "PENDING_RESIZE"))
            else:
                # size 未變 → 不產生任何任務（即使 provisioning 不同也忽略）
                continue

    # --- 3) 刪除：DB 既有 - 表單（有 size 的那批） ---
    db_ids = set(db_by_id.keys())
    to_delete = sorted(list(db_ids - form_present_ids))

    return to_create, to_update, to_delete


# =======================================
# 從 vSphere 同步 label 到資料庫
# =======================================
def sync_disk_labels_to_database(vm_name: str, vsphere_disks: List[Dict]):
    """將從 vSphere 讀取的磁盤狀態同步到資料庫"""
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            # 找到對應的 vm_configuration_id
            cursor.execute("""
                SELECT id FROM vm_configurations 
                WHERE vm_name_prefix = %s ORDER BY id DESC LIMIT 1
            """, (vm_name,))
            
            vm_config = cursor.fetchone()
            if not vm_config:
                return
            
            vm_configuration_id = vm_config["id"]
            
            # 更新每個磁盤的 label
            for disk in vsphere_disks:
                cursor.execute("""
                    UPDATE vm_disks 
                    SET label = %s
                    WHERE vm_configuration_id = %s 
                    AND scsi_controller = %s 
                    AND unit_number = %s
                """, (
                    disk["label_number"], 
                    vm_configuration_id,
                    disk["controller_bus"], 
                    disk["unit_number"]
                ))
            
            db_conn.commit()
    except Exception as e:
        db_conn.rollback()
        raise e
    finally:
        db_conn.close()

# =======================================
# Create：寫 vm_configurations + PENDING 磁碟
# （符合新流程：磁碟只標 PENDING_CREATION，SCSI/label/vmdk_path 由 pyVmomi 建立後回寫）
# =======================================
def _apply_create_action(db_conn, form_data):
    """
    私有函式：處理 Create 請求的資料庫寫入（新流程）
      1) 寫入 vm_configurations
      2) 將額外磁碟以 PENDING_CREATION 寫入 vm_disks（不寫 scsi/unit/label/vmdk_path）
    """
    cursor = None
    try:
        cursor = db_conn.cursor()

        # --- 1) 插入主表 vm_configurations ---
        # os_type 有舊表單 'vm_os_type' 與新鍵 'os_type' 的差異，雙軌相容
        os_type_value = _Helpers._first_scalar(form_data.get("os_type"))
        if not os_type_value:
            os_type_value = _Helpers._first_scalar(form_data.get("vm_os_type"))

        sql_vm_config = """
            INSERT INTO vm_configurations (
                environment, resource, os_type, vsphere_host, vsphere_datacenter, vsphere_cluster, vsphere_esxi_host,
                vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
                vm_instance_type, vm_num_cpus, vm_memory, vm_scsi_controller_count,
                vm_ipv4_gateway, netbox_prefix, netbox_tenant
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params_vm_config = (
            _Helpers._first_scalar(form_data.get("environment")),
            _Helpers._first_scalar(form_data.get("resource")),
            os_type_value,
            _Helpers._first_scalar(form_data.get("vsphere_host")),
            _Helpers._first_scalar(form_data.get("vsphere_datacenter")),
            _Helpers._first_scalar(form_data.get("vsphere_cluster")),
            _Helpers._first_scalar(form_data.get("vsphere_esxi_host")),
            _Helpers._first_scalar(form_data.get("vsphere_network")),
            _Helpers._first_scalar(form_data.get("vsphere_template")),
            _Helpers._first_scalar(form_data.get("vsphere_datastore")),
            _Helpers._first_scalar(form_data.get("vm_name_prefix")),
            _Helpers._first_scalar(form_data.get("vm_instance_type")),
            _Helpers._to_int(form_data.get("vm_num_cpus"), 2),
            _Helpers._to_int(form_data.get("vm_memory"), 2048),
            _Helpers._to_int(form_data.get("create_vm_scsi_controller_count"), 1),
            _Helpers._first_scalar(form_data.get("vm_ipv4_gateway")),
            _Helpers._first_scalar(form_data.get("netbox_prefix")),
            _Helpers._first_scalar(form_data.get("netbox_tenant")),
        )
        cursor.execute(sql_vm_config, params_vm_config)
        vm_config_id = cursor.lastrowid
        logger.info("   -> Applied CREATE action for vm_config_id: %s", vm_config_id)

        # --- 2) 插入附屬表 vm_disks：只寫 PENDING_CREATION（關鍵變更） ---
        disks = _extract_disks_for_create(form_data)
        if disks:
            # 新流程：不寫 scsi_controller / unit_number / label / vmdk_path
            sql_vm_disk = """
                INSERT INTO vm_disks (
                    vm_configuration_id, size, disk_provisioning, status
                ) VALUES (%s, %s, %s, 'PENDING_CREATION')
            """
            rows = [(vm_config_id, size_gb, provisioning) for (size_gb, provisioning) in disks]
            cursor.executemany(sql_vm_disk, rows)
            logger.info("   -> Marked %d disks as PENDING_CREATION for vm_config_id: %s", len(rows), vm_config_id)

        db_conn.commit()

    except Error as db_error:
        logger.error("❌ DB error in _apply_create_action: %s", db_error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as error:
        logger.error("❌ Unexpected error in _apply_create_action: %s", error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()


def _apply_update_action(db_conn, form_data):
    """
    私有函式：處理 Update 請求的資料庫寫入（不動磁碟）。
    統一介面：接受整包 form_data；若含 new_config 則以 new_config 為準。
    """
    cursor = None
    try:
        # 以 new_config（若存在）為準，否則回退頂層（相容舊 payload）
        if isinstance(form_data, dict) and isinstance(form_data.get("new_config"), dict):
            data = form_data["new_config"]
        else:
            data = form_data

        environment_value    = _Helpers._first_scalar(data.get("environment"))
        vm_name_prefix_value = _Helpers._first_scalar(data.get("vm_name_prefix"))

        if not environment_value or not vm_name_prefix_value:
            raise ValueError("Cannot apply update: missing environment/vm_name_prefix in payload.")

        cursor = db_conn.cursor(dictionary=True)

        # 1) 先查 vm_config
        cursor.execute(
            "SELECT id FROM vm_configurations WHERE environment = %s AND vm_name_prefix = %s",
            (environment_value, vm_name_prefix_value)
        )
        vm_config = cursor.fetchone()
        if not vm_config:
            raise ValueError(f"Cannot apply update: VM '{vm_name_prefix_value}' in '{environment_value}' not found.")
        vm_config_id = vm_config["id"]

        # 2) 更新 CPU / Memory（沿用你的邏輯）
        cursor.execute(
            """
            UPDATE vm_configurations
            SET vm_num_cpus = %s,
                vm_memory   = %s,
                updated_at  = NOW()
            WHERE id = %s
            """,
            (
                _Helpers._to_int(data.get("vm_num_cpus")),
                _Helpers._to_int(data.get("vm_memory")),
                vm_config_id
            )
        )
        logger.info("   -> Applied UPDATE action for vm_config_id: %s", vm_config_id)

        # ----------------------  新增：磁碟三態  ----------------------
        # 2.1 撈出 DB 目前的磁碟（上鎖避免競態）
        cursor.execute(
            """
            SELECT id, size, disk_provisioning, status
            FROM vm_disks
            WHERE vm_configuration_id = %s
            FOR UPDATE
            """,
            (vm_config_id,)
        )
        db_disks = cursor.fetchall() or []

        # 2.2 計畫 create / update / delete
        to_create, to_update, to_delete = _plan_disks_for_update(data, db_disks)

        # 2.3 執行 create
        if to_create:
            cursor.executemany(
                """
                INSERT INTO vm_disks (vm_configuration_id, size, disk_provisioning, status)
                VALUES (%s, %s, %s, 'PENDING_CREATION')
                """,
                [(vm_config_id, size_gb, prov) for (size_gb, prov) in to_create]
            )
            logger.info("   -> Disks to create: %d", len(to_create))

        # 2.4 執行 update（大小/配置/皆變）
        for (disk_id, size_gb, prov, new_status) in to_update:
            cursor.execute(
                """
                UPDATE vm_disks
                SET size = %s,
                    disk_provisioning = %s,
                    status = %s
                WHERE id = %s
                """,
                (size_gb, _normalize_provisioning(prov), new_status, disk_id)
            )
        if to_update:
            logger.info("   -> Disks to update: %d", len(to_update))

        # 2.5 執行 delete（標記待刪）
        if to_delete:
            cursor.executemany(
                "UPDATE vm_disks SET status = 'PENDING_DELETE' WHERE id = %s",
                [(did,) for did in to_delete]
            )
            logger.info("   -> Disks to delete: %d", len(to_delete))
        # ----------------------  磁碟三態 end  ----------------------

        db_conn.commit()

    except Error as db_error:
        logger.error("❌ DB error in _apply_update_action: %s", db_error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as error:
        logger.error("❌ Unexpected error in _apply_update_action: %s", error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()

def _apply_delete_action(db_conn, form_data):
    """
    私有函式：處理 Delete 請求的資料庫寫入。
    將目標 VM 的 lifecycle_status 標記為 'DELETING'。
    """
    cursor = None
    try:
        # 以 original_config（若存在）為準，否則回退頂層（相容舊 payload）
        if isinstance(form_data, dict) and isinstance(form_data.get("original_config"), dict):
            data = form_data["original_config"]
        else:
            data = form_data

        environment_value    = _Helpers._first_scalar(data.get("environment"))
        vm_name_prefix_value = _Helpers._first_scalar(data.get("vm_name_prefix"))

        if not environment_value or not vm_name_prefix_value:
            raise ValueError("Cannot apply delete: missing environment/vm_name_prefix in payload.")

        cursor = db_conn.cursor(dictionary=True)

        # 1) 先查 vm_configurations 是否存在
        cursor.execute(
            "SELECT id FROM vm_configurations WHERE environment = %s AND vm_name_prefix = %s",
            (environment_value, vm_name_prefix_value)
        )
        vm_config = cursor.fetchone()
        if not vm_config:
            raise ValueError(f"Cannot apply delete: VM '{vm_name_prefix_value}' in '{environment_value}' not found.")
        vm_config_id = vm_config["id"]

        # 2) 更新 lifecycle_status 為 DELETING
        cursor.execute(
            """
            UPDATE vm_configurations
            SET lifecycle_status = 'DELETING',
                updated_at = NOW()
            WHERE id = %s
            """,
            (vm_config_id,)
        )
        logger.info("   -> Applied DELETE action for vm_config_id: %s, marked as DELETING", vm_config_id)

        db_conn.commit()

    except Error as db_error:
        logger.error("❌ DB error in _apply_delete_action: %s", db_error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as error:
        logger.error("❌ Unexpected error in _apply_delete_action: %s", error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()



# =======================================
# 供 route 呼叫：保留並相容你的流程
# =======================================
def apply_request_to_db(workflow_id):
    """
    被 /workflow/execute/<workflow_id> 呼叫：
      - 從 workflow_runs.request_payload 取出 form_data
      - 依 action_type = create / update 寫入 DB
      - create：只把額外磁碟標記為 PENDING_CREATION（不寫 scsi/unit/label/vmdk_path）
      - 成功後把 workflow 狀態設為 IN_PROGRESS（維持你的原流程）
    """
    db_conn = get_db_connection()
    try:
        # 1) 讀取 request_payload
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT request_payload FROM workflow_runs WHERE workflow_id = %s",
                (workflow_id,)
            )
            workflow = cursor.fetchone()

        if not workflow or not workflow.get("request_payload"):
            raise ValueError(f"Workflow {workflow_id} not found or has no payload.")

        # 2) 解析 JSON
        form_data = json.loads(workflow["request_payload"])
        action_type = _Helpers._first_scalar(form_data.get("action_type"))

        # 3) 依 action_type 分派
        if action_type == "create":
            _apply_create_action(db_conn, form_data)
        elif action_type == "update":
            _apply_update_action(db_conn, form_data)
        elif action_type == "delete":
            _apply_delete_action(db_conn, form_data)
        else:
            raise ValueError(f"Unsupported action_type: {action_type}")

        # 4) 更新狀態為 IN_PROGRESS（維持一致）
        update_request_status(workflow_id, "IN_PROGRESS")

    except Error as db_error:
        logger.error("❌ Database error in apply_request_to_db for workflow_id %s: %s", workflow_id, db_error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as error:
        logger.error("❌ Unexpected error in apply_request_to_db for workflow_id %s: %s", workflow_id, error)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()