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


def get_vms_by_environment(environment):
    """
    根據 environment 獲取所有對應的 vm_name_prefix。
    - 自行建立/關閉連線
    """
    db_conn = get_db_connection()
    vms = []
    try:
        with db_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT vm_name_prefix
                FROM vm_configurations
                WHERE environment = %s
                ORDER BY vm_name_prefix
                """,
                (environment,)
            )
            rows = cursor.fetchall() or []
            vms = [item[0] for item in rows]
    except Error as e:
        logging.error(f"[get_vms_by_environment] DB error: {e}")
        return []
    except Exception as e:
        logging.error(f"[get_vms_by_environment] Unexpected error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()
    return vms


def get_vms_by_environment(environment):
    """
    根據 environment 獲取所有對應的 vm_name_prefix。
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor() as cursor:
            cursor.execute("""
                SELECT vm_name_prefix
                FROM vm_configurations
                WHERE environment = %s
                ORDER BY vm_name_prefix
            """, (environment,))
            return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"[get_vms_by_environment] DB error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()

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

        # 查磁碟
        with db_conn.cursor(dictionary=True) as disk_cursor:
            disk_cursor.execute("""
                SELECT
                    id,
                    scsi_controller,
                    unit_number,
                    ui_disk_number,
                    size,
                    disk_provisioning,
                    thin_provisioned,
                    eagerly_scrub
                FROM vm_disks
                WHERE vm_configuration_id = %s
                ORDER BY scsi_controller ASC, unit_number ASC
            """, (vm_id,))
            config["additional_disks"] = disk_cursor.fetchall()

        return config

    except Error as e:
        logging.error(f"[get_vm_config] DB error: {e}")
        return None
    finally:
        if db_conn:
            db_conn.close()