# app/vsphere/vm/db/vm_provisioning_manager.py (新建)
import json
from mysql.connector import Error
import logging
from app.mysql.db import get_db_connection
# from .workflow_manager import _Helpers

# 保留 _Helpers 類別
class _Helpers:
    @staticmethod
    def _first_scalar(value, default=None):
        if isinstance(value, list): return value[0] if value else default
        return value if value is not None else default

    @staticmethod
    def _as_list(value):
        if value is None: return []
        if isinstance(value, list): return value
        return [value]

    @staticmethod
    def _to_int(value, default=0):
        scalar_value = _Helpers._first_scalar(value)
        try:
            return int(scalar_value)
        except (TypeError, ValueError):
            return default

def _apply_create_action(db_conn, form_data):
    """私有函式：處理 Create 請求的資料庫寫入"""
    cursor = None
    try:
        cursor = db_conn.cursor()

        # 1) 插入主表
        sql_vm_config = """
            INSERT INTO vm_configurations (
                environment, resource, os_type, vsphere_host, vsphere_datacenter, vsphere_cluster, vsphere_esxi_host,
                vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
                vm_instance_type, vm_num_cpus, vm_memory, vm_scsi_controller_count,
                vm_ipv4_gateway, netbox_prefix, netbox_tenant
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params_vm_config = (
            _Helpers._first_scalar(form_data.get('environment')),
            _Helpers._first_scalar(form_data.get('resource')),
            _Helpers._first_scalar(form_data.get('os_type')),
            _Helpers._first_scalar(form_data.get('vsphere_host')),
            _Helpers._first_scalar(form_data.get('vsphere_datacenter')),
            _Helpers._first_scalar(form_data.get('vsphere_cluster')),
            _Helpers._first_scalar(form_data.get('vsphere_esxi_host')),
            _Helpers._first_scalar(form_data.get('vsphere_network')),
            _Helpers._first_scalar(form_data.get('vsphere_template')),
            _Helpers._first_scalar(form_data.get('vsphere_datastore')),
            _Helpers._first_scalar(form_data.get('vm_name_prefix')),
            _Helpers._first_scalar(form_data.get('vm_instance_type')),
            _Helpers._to_int(form_data.get('vm_num_cpus'), 2),
            _Helpers._to_int(form_data.get('vm_memory'), 2048),
            _Helpers._to_int(form_data.get('create_vm_scsi_controller_count'), 1),
            _Helpers._first_scalar(form_data.get('vm_ipv4_gateway')),
            _Helpers._first_scalar(form_data.get('netbox_prefix')),
            _Helpers._first_scalar(form_data.get('netbox_tenant')),
        )
        cursor.execute(sql_vm_config, params_vm_config)
        vm_config_id = cursor.lastrowid
        logging.info("   -> Applied CREATE action for vm_config_id: %s", vm_config_id)

        # 2) 插入附屬表 (磁碟記錄，狀態為 PENDING_CREATION)
        disk_sizes = _Helpers._as_list(form_data.get('create_vm_disk_size[]'))
        if disk_sizes:
            sql_vm_disk = """
                INSERT INTO vm_disks (
                    vm_configuration_id, scsi_controller, unit_number, size, 
                    disk_provisioning, status, ui_disk_number
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            disk_provs = _Helpers._as_list(form_data.get('create_vm_disk_provisioning[]'))
            disk_scsis = _Helpers._as_list(form_data.get('create_vm_disk_scsi_controller[]'))
            disk_units = _Helpers._as_list(form_data.get('create_vm_disk_unit_number[]'))

            rows = []
            for i, size in enumerate(disk_sizes):
                rows.append((
                    vm_config_id,
                    _Helpers._to_int(disk_scsis[i] if i < len(disk_scsis) else 0, 0),
                    _Helpers._to_int(disk_units[i] if i < len(disk_units) else (i + 1), i + 1),
                    _Helpers._to_int(size, 1),
                    (disk_provs[i] if i < len(disk_provs) else 'thin'),
                    'PENDING_CREATION',
                    i + 2
                ))
            cursor.executemany(sql_vm_disk, rows)
            logging.info("   -> Marked %d disks as PENDING_CREATION for vm_config_id: %s", len(rows), vm_config_id)

        db_conn.commit()

    except Error as e:
        logging.error("❌ DB error in _apply_create_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in _apply_create_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()

def _apply_update_action(db_conn, form_data):
    """私有函式：處理 Update 請求的資料庫寫入"""
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            # 1) 取關鍵欄位
            env    = _Helpers._first_scalar(form_data.get('environment'))
            prefix = _Helpers._first_scalar(form_data.get('vm_name_prefix'))

            # 2) 查詢目標 vm_config_id
            cursor.execute(
                "SELECT id FROM vm_configurations WHERE environment = %s AND vm_name_prefix = %s",
                (env, prefix)
            )
            vm_config = cursor.fetchone()
            if not vm_config:
                raise ValueError(f"Cannot apply update: VM '{prefix}' in '{env}' not found.")
            vm_config_id = vm_config['id']

            # 3) 更新主表
            cursor.execute(
                """
                UPDATE vm_configurations
                SET vm_num_cpus = %s,
                    vm_memory   = %s,
                    updated_at  = NOW()
                WHERE id = %s
                """,
                (
                    _Helpers._to_int(form_data.get('vm_num_cpus')),
                    _Helpers._to_int(form_data.get('vm_memory')),
                    vm_config_id
                )
            )

            logging.info("   -> Applied UPDATE action for vm_config_id: %s", vm_config_id)

        db_conn.commit()

    except Error as e:
        logging.error("❌ DB error in _apply_update_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error("❌ Unexpected error in _apply_update_action: %s", e)
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise

def apply_request_to_db(workflow_id):
    """
    在請求被批准後呼叫，將變更正式應用到 vm_configurations 和 vm_disks 表
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

        if not workflow or not workflow.get('request_payload'):
            raise ValueError(f"Workflow {workflow_id} not found or has no payload.")

        # 2) 解析 JSON
        form_data = json.loads(workflow['request_payload'])
        action_type = _Helpers._first_scalar(form_data.get('action_type'))

        # 3) 依 action_type 分派
        if action_type == 'create':
            _apply_create_action(db_conn, form_data)
        elif action_type == 'update':
            _apply_update_action(db_conn, form_data)
        else:
            raise ValueError(f"Unsupported action_type: {action_type}")

        # 4) 更新狀態為 IN_PROGRESS
        from .workflow_manager import update_request_status
        update_request_status(workflow_id, 'IN_PROGRESS')

    except Error as e:
        logging.error(f"❌ Database error in apply_request_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    except Exception as e:
        logging.error(f"❌ Unexpected error in apply_request_to_db for workflow_id {workflow_id}: {e}")
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        raise
    finally:
        if db_conn:
            db_conn.close()

