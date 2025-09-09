from .. import vm_bp
from flask import render_template, request, redirect, url_for, flash, jsonify, current_app
import logging

# --- DB 函式 ---
from ..db.vsphere_connections_manager import (
    get_all_vsphere_connections,
    add_or_update_vsphere_connection,
    update_connection_password,
    delete_vsphere_connection_by_id,
    toggle_connection_status,
    get_vsphere_connection_by_id
)

# --- API 函式 ---
from ..vsphere_api.test_connection import test_vsphere_connection
from ..vault.vault_manager import VaultManager

@vm_bp.route("/vsphere/connections", methods=['GET', 'POST'])
def manage_vsphere_connections():
    try:
        if request.method == 'POST':
            add_or_update_vsphere_connection(
                env=request.form['environment'],
                host=request.form['host'],
                user=request.form['user'],
                password_plain=request.form['password']
            )
            flash(f"Connection for '{request.form['host']}' saved and synced to Vault successfully.", "success")
            return redirect(url_for('vm.manage_vsphere_connections'))

        connections = get_all_vsphere_connections()
        return render_template("manage_vsphere_connection.html", connections=connections)

    except Exception as e:
        flash(f"An error occurred: {e}", "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

# vsphere 連線測試
@vm_bp.route("/api/vsphere/connections/test/<int:conn_id>", methods=['POST'])
def test_vsphere_connection_api(conn_id):
   """
   連線測試
   """
   try:
       conn_info = get_vsphere_connection_by_id(conn_id)
       if not conn_info:
           return jsonify({"success": False, "message": "Connection not found or is inactive."}), 404
       if conn_info.get('password') is None:
           return jsonify({
               "success": False,
               "message": conn_info.get('decrypt_error', 'Password decrypt failed.')
           }), 400
       is_ok, message = test_vsphere_connection(
           host=conn_info['host'],
           user=conn_info['user'],
           password=conn_info['password']
       )
       return jsonify({"success": is_ok, "message": message})
   except Exception as e:
       logging.error(f"Error in test_vsphere_connection_api for conn_id {conn_id}: {e}")
       return jsonify({"success": False, "message": "An internal server error occurred."}), 500

@vm_bp.route("/vsphere/connections/change_password/<int:conn_id>", methods=['POST'])
def change_vsphere_password(conn_id):
    """
    Supports both:
    - Normal form submit (flash + redirect)
    - AJAX (X-Requested-With: XMLHttpRequest) -> JSON {success, message, errors?}
    Expected fields: current_password, new_password, confirm_new_password
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    try:
        current_password = request.form.get('current_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_new_password = request.form.get('confirm_new_password', '').strip()

        # Basic validation
        errors = {}
        if not current_password:
            errors["current_password"] = "Current password is required."
        if not new_password:
            errors["new_password"] = "New password is required."
        if confirm_new_password != new_password:
            errors["confirm_new_password"] = "New passwords do not match."

        if errors:
            if is_ajax:
                return jsonify({"success": False, "message": "Validation failed.", "errors": errors}), 400
            for _, msg in errors.items():
                flash(msg, "danger")
            return redirect(url_for('vm.manage_vsphere_connections'))

        success, message = update_connection_password(conn_id, current_password, new_password)

        if is_ajax:
            return jsonify({"success": success, "message": message}), (200 if success else 400)

        flash(message, "success" if success else "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": f"Internal error: {e}"}), 500
        flash(f"Error updating password: {e}", "danger")
        return redirect(url_for('vm.manage_vsphere_connections'))

@vm_bp.route("/vsphere/connections/delete/<int:conn_id>", methods=['POST'])
def delete_vsphere_connection(conn_id):
    """
    刪除指定的 vSphere 連線。
    """
    try:
        affected = delete_vsphere_connection_by_id(conn_id)
        if affected == 0:
            flash(f"Connection {conn_id} not found.", "warning")
        else:
            flash("Connection deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting connection: {e}", "danger")

    return redirect(url_for('vm.manage_vsphere_connections'))

@vm_bp.route("/vsphere/connections/toggle/<int:conn_id>", methods=['POST'])
def toggle_vsphere_connection(conn_id):
    """
    切換 vsphere 連線的啟用狀態。
    """
    try:
        affected = toggle_connection_status(conn_id)
        if affected > 0:
            flash("Connection status updated.", "info")
        else:
            flash("No connection updated (maybe not found).", "warning")
    except Exception as e:
        logging.error(f"[toggle_vsphere_connection] {e}")
        flash(f"Error updating status: {e}", "danger")

    return redirect(url_for('vm.manage_vsphere_connections'))

# Vault 連線測試
@vm_bp.route("/api/vault/connections/test", methods=['POST'])
def test_vault_connection():
    """
    測試 Vault 連線狀態
    """
    try:
        vault_manager = VaultManager()
        success, message = vault_manager.test_connection()

        return jsonify({
            "success": success,
            "message": message,
            "vault_configured": bool(current_app.config.get('VAULT_TOKEN'))
        })

    except Exception as e:
        logging.error(f"Vault test error: {e}")
        return jsonify({
            "success": False,
            "message": f"Test failed: {str(e)}"
        }), 500

# 手動同步到 Vault
@vm_bp.route("/api/vault/sync/<int:conn_id>", methods=['POST'])
def sync_vsphere_connection_to_vault(conn_id):
    """
    手動將指定連線同步到 Vault（管理員功能）
    """
    try:
        # 取得連線資訊（含解密密碼）
        conn_info = get_vsphere_connection_by_id(conn_id)
        if not conn_info:
            return jsonify({"success": False, "message": "Connection not found"}), 404

        if conn_info.get('password') is None:
            return jsonify({
                "success": False,
                "message": conn_info.get('decrypt_error', 'Password decrypt failed')
            }), 400

        # 同步到 Vault
        vault_manager = VaultManager()
        success, message = vault_manager.store_vsphere_credentials(
            environment=conn_info['environment'],
            host=conn_info['host'],
            user=conn_info['user'],
            password=conn_info['password']
        )

        return jsonify({"success": success, "message": message})

    except Exception as e:
        logging.error(f"Vault sync error for conn_id {conn_id}: {e}")
        return jsonify({"success": False, "message": f"Sync failed: {str(e)}"}), 500

