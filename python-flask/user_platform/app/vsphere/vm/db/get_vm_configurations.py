# app/vsphere/vm/db/get_vm_configurations.py
from mysql.connector import Error

def get_environment(db_conn):
    """
    從 vm_configurations 表中獲取所有不重複的 environment 名稱列表。
    """
    environments = []
    cursor = None
    try:
        cursor = db_conn.cursor()
        query = "SELECT DISTINCT environment FROM vm_configurations ORDER BY environment"
        cursor.execute(query)
        environments = [item[0] for item in cursor.fetchall()]
    except Exception as e:
        print(f"An error occurred while getting environments from the database: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
    return environments


def get_vms_by_environment(db_conn, environment):
    """
    根據 environment 獲取所有對應的 vm_name_prefix。
    """
    vms = []
    cursor = None
    try:
        cursor = db_conn.cursor()
        query = """
            SELECT vm_name_prefix
            FROM vm_configurations
            WHERE environment = %s
            ORDER BY vm_name_prefix
        """
        cursor.execute(query, (environment,))
        vms = [item[0] for item in cursor.fetchall()]
    finally:
        if cursor:
            cursor.close()
    return vms


def get_vm_config(db_conn, environment, vm_name_prefix):
    """
    根據 environment 和 vm_name_prefix 獲取特定 VM 的完整設定 (包含關聯的磁碟)。

    Returns:
        dict or None:
        {
          id: int,
          environment: str,
          resource: str,
          os_type: str,
          ... (vm_configurations 其他欄位，包含 vm_scsi_controller_count)
          additional_disks: [
            {
              id: int,
              scsi_controller: int,
              unit_number: int,
              ui_disk_number: int | None,
              size: int,
              disk_provisioning: str,
              thin_provisioned: bool,
              eagerly_scrub: bool
            },
            ...
          ]
        }
    """
    config = None
    cursor = None
    try:
        # 1) 讀 vm_configurations（包含 vm_scsi_controller_count）
        cursor = db_conn.cursor(dictionary=True)
        query = """
            SELECT *
            FROM vm_configurations
            WHERE environment = %s AND vm_name_prefix = %s
            LIMIT 1
        """
        cursor.execute(query, (environment, vm_name_prefix))
        config = cursor.fetchone()

        if not config:
            return None

        vm_id = config["id"]

        # 2) 讀 vm_disks（已移除 label，改用 scsi_controller / unit_number / ui_disk_number）
        disk_cursor = db_conn.cursor(dictionary=True)
        disk_query = """
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
        """
        disk_cursor.execute(disk_query, (vm_id,))
        disks = disk_cursor.fetchall()
        disk_cursor.close()

        # 3) 塞回 config
        config["additional_disks"] = disks

    except Error as e:
        print(f"An error occurred while querying VM configuration: {e}")
        return None
    finally:
        if cursor:
            cursor.close()

    return config