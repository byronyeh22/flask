from .. import vm_bp
from flask import render_template, jsonify, request
import logging
import traceback

# --- DB 函式 ---
from ..db.vsphere_connections_manager import (
    get_hosts_by_environment,
    get_vsphere_connection_by_host
)
from ..db.get_vm_configurations import get_vms_by_filters, get_vm_config, get_validate_vm_exists

# --- API 函式 ---
from ..vsphere_api.get_vsphere_objects import get_vsphere_objects


# --- app/vsphere/vm/templates/create/form.html ---
# 依 Environment 取得「Active vSphere Host」清單（供前端在 Environment 選定後載入 Host 下拉選單）
@vm_bp.route('/api/vsphere/hosts/<string:environment>')
def get_hosts_by_environment_api(environment):
   """
   API: 依 environment 取得可用的 vSphere hosts
   """
   try:
       hosts = get_hosts_by_environment(environment, active_only=True) or []
       return jsonify({"environment": environment, "hosts": hosts})
   except Exception as e:
       logging.error(f"[get_hosts_by_environment_api] {environment} -> {e}")
       return jsonify({"error": "An internal server error occurred"}), 500

# 依 vSphere Host 取得 vSphere 物件（供前端在 Host 選定後載入 Placement/Template/Datastore/Network 等）
@vm_bp.route('/api/vsphere_objects/by_host/<path:host>')
def get_vsphere_objects_by_host_api(host):
   """
   取得指定 host 的 vSphere 物件清單。
   """
   try:
       conn_info = get_vsphere_connection_by_host(host, require_active=True)
       if not conn_info:
           return jsonify({"error": f"Active vSphere connection not found for host: {host}"}), 404

       # 解密失敗時，沿用你原本的回傳內容與狀態碼
       if conn_info.get('password') is None:
           error_msg = conn_info.get('decrypt_error', 'Password decryption failed.')
           return jsonify({"success": False, "message": error_msg}), 400

       # 呼叫 vSphere API 取得物件
       vsphere_data = get_vsphere_objects(
           host=conn_info['host'],
           user=conn_info['user'],
           password=conn_info['password']
       ) or {}

       return jsonify(vsphere_data)

   except Exception as e:
       logging.error(f"Error in get_vsphere_objects_by_host_api for host {host}: {e}")
       traceback.print_exc()
       return jsonify({"error": "An internal server error occurred"}), 500

@vm_bp.route('/api/get_vms_by_environment/<string:environment>')
def get_vms_by_environment_api(environment):
    """
    API endpoint to fetch VMs for a given environment, optionally filtered by host.
    """
    try:
        vsphere_esxi_host = request.args.get('vsphere_esxi_host')
        vms = get_vms_by_filters(environment, vsphere_esxi_host)
        return jsonify(vms)
    except Exception as e:
        logging.error(f"Error in get_vms_by_environment_api: {e}")
        return jsonify({"error": "Internal server error"}), 500

@vm_bp.route('/api/get_vm_config/<string:environment>/<string:vm_name_prefix>')
def get_vm_config_api(environment, vm_name_prefix):
    """
    API endpoint to fetch a specific VM's configuration.
    """
    try:
        logging.info(f"API called with env={environment}, vm={vm_name_prefix}")

        # 驗證 VM 是否存在
        validation_result = get_validate_vm_exists(environment, vm_name_prefix)
        if not validation_result['exists']:
            logging.warning(f"VM not found: {vm_name_prefix} in {environment}")
            logging.info(f"Available VMs in {environment}: {validation_result['available_vms']}")
            return jsonify({
                "error": f"Configuration not found for VM '{vm_name_prefix}' in environment '{environment}'"
            }), 404

        # 獲取 VM 配置
        config = get_vm_config(environment, vm_name_prefix)
        if not config:
            return jsonify({
                "error": f"Configuration not found for VM '{vm_name_prefix}' in environment '{environment}'"
            }), 404

        return jsonify(config)

    except Exception as e:
        logging.error(f"Error in get_vm_config_api: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500