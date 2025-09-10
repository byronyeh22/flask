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


# =======================================
# Update：保留你原邏輯（僅示範 CPU/Mem）
# =======================================
def _apply_update_action(db_conn, form_data):
    """私有函式：處理 Update 請求的資料庫寫入（不動磁碟）。"""
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            environment_value = _Helpers._first_scalar(form_data.get("environment"))
            vm_name_prefix_value = _Helpers._first_scalar(form_data.get("vm_name_prefix"))

            cursor.execute(
                "SELECT id FROM vm_configurations WHERE environment = %s AND vm_name_prefix = %s",
                (environment_value, vm_name_prefix_value)
            )
            vm_config = cursor.fetchone()
            if not vm_config:
                raise ValueError(f"Cannot apply update: VM '{vm_name_prefix_value}' in '{environment_value}' not found.")
            vm_config_id = vm_config["id"]

            cursor.execute(
                """
                UPDATE vm_configurations
                SET vm_num_cpus = %s,
                    vm_memory   = %s,
                    updated_at  = NOW()
                WHERE id = %s
                """,
                (
                    _Helpers._to_int(form_data.get("vm_num_cpus")),
                    _Helpers._to_int(form_data.get("vm_memory")),
                    vm_config_id
                )
            )
            logger.info("   -> Applied UPDATE action for vm_config_id: %s", vm_config_id)

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