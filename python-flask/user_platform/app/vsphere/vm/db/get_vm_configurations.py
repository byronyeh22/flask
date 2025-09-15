# app/vsphere/vm/db/get_vm_configurations.py
from mysql.connector import Error
import logging
from app.mysql.db import get_db_connection

def get_environment():
    """
    從 vm_configurations 表中獲取所有不重複的 environment 名稱列表。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT environment FROM vm_configurations ORDER BY environment"
            )
            rows = cursor.fetchall() or []
            return [item[0] for item in rows]

    except Error as e:
        logging.error(f"[get_environment] DB error: {e}")
        return []
    except Exception as e:
        logging.error(f"[get_environment] Unexpected error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()


# 修正：將兩個同名函式合併成一個，並增加新的過濾條件
def get_vms_by_filters(environment, vsphere_esxi_host=None):
    """
    根據 environment 和可選的 vsphere_esxi_host 獲取 VM 名稱前綴。
    """
    vms = []
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            sql = "SELECT vm_name_prefix FROM vm_configurations WHERE environment = %s"
            params = [environment]

            if vsphere_esxi_host:
                sql += " AND vsphere_esxi_host = %s"
                params.append(vsphere_esxi_host)

            sql += " ORDER BY vm_name_prefix"

            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall() or []
            vms = [item[0] for item in rows]
    except Error as e:
        logging.error(f"[get_vms_by_filters] DB error: {e}")
        return []
    except Exception as e:
        logging.error(f"[get_vms_by_filters] Unexpected error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()
    return vms

def get_vm_config(environment, vm_name_prefix):
    """
    根據 environment 和 vm_name_prefix 獲取特定 VM 的完整設定 (包含關聯的磁碟)。
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT *
                FROM vm_configurations
                WHERE environment = %s AND vm_name_prefix = %s
                LIMIT 1
            """, (environment, vm_name_prefix))
            config = cursor.fetchone()

        if not config:
            return None

        vm_id = config["id"]

        # 查磁碟 - 修正欄位名稱
        with db_conn.cursor(dictionary=True) as disk_cursor:
            disk_cursor.execute("""
                SELECT
                    id,
                    scsi_controller,
                    unit_number,
                    label,                    -- 改為 label
                    size,
                    disk_provisioning,
                    status,
                    vmdk_path,
                    created_at,
                    updated_at
                FROM vm_disks
                WHERE vm_configuration_id = %s
                ORDER BY scsi_controller ASC, unit_number ASC
            """, (vm_id,))
            disks = disk_cursor.fetchall()

            # 為了向後兼容，將 label 映射為 ui_disk_number
            for disk in disks:
                disk['ui_disk_number'] = disk['label']

            config["additional_disks"] = disks

        return config

    except Error as e:
        logging.error(f"[get_vm_config] DB error: {e}")
        return None
    finally:
        if db_conn:
            db_conn.close

def get_validate_vm_exists(environment, vm_name_prefix):
    """
    驗證指定的 VM 是否存在於指定環境中

    Args:
        environment (str): 環境名稱
        vm_name_prefix (str): VM 名稱前綴

    Returns:
        dict: {
            'exists': bool,
            'environment_count': int,
            'available_vms': list
        }
    """
    try:
        from app.mysql.db import get_db_connection

        with get_db_connection() as db_conn:
            with db_conn.cursor(dictionary=True) as cursor:
                # 檢查環境中的 VM 總數
                cursor.execute(
                    "SELECT COUNT(*) as count FROM vm_configurations WHERE environment = %s",
                    (environment,)
                )
                env_count = cursor.fetchone()['count']

                # 獲取該環境中所有 VM 名稱
                cursor.execute(
                    "SELECT vm_name_prefix FROM vm_configurations WHERE environment = %s",
                    (environment,)
                )
                all_vms = [vm['vm_name_prefix'] for vm in cursor.fetchall()]

                # 檢查指定的 VM 是否存在
                vm_exists = vm_name_prefix in all_vms

                return {
                    'exists': vm_exists,
                    'environment_count': env_count,
                    'available_vms': all_vms
                }

    except Exception as e:
        logging.error(f"Error validating VM existence: {e}", exc_info=True)
        return {
            'exists': False,
            'environment_count': 0,
            'available_vms': []
        }