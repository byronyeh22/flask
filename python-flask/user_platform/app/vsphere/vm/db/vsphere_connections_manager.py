# app/vsphere/vm/db/vsphere_connections_manager.py
from mysql.connector import Error
from cryptography.fernet import Fernet
from flask import current_app

# --- Encryption Utilities (Integrated) ---

def _get_cipher():
    """Initializes Fernet cipher with the key from app config."""
    key = current_app.config['FERNET_KEY']
    return Fernet(key.encode('utf-8'))

def _encrypt_password(password: str) -> str:
    """Encrypts a password and returns it as a string."""
    if not password:
        return ""
    cipher = _get_cipher()
    encrypted_bytes = cipher.encrypt(password.encode('utf-8'))
    return encrypted_bytes.decode('utf-8')

def _decrypt_password(encrypted_password: str) -> str:
    """Decrypts an encrypted password string and returns it."""
    if not encrypted_password:
        return ""
    cipher = _get_cipher()
    decrypted_bytes = cipher.decrypt(encrypted_password.encode('utf-8'))
    return decrypted_bytes.decode('utf-8')

# --- Database Management Functions ---

def get_all_vsphere_connections(db_conn):
    """取得所有 vSphere 連線設定（不含密碼）。"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, environment, host, user, is_active FROM vsphere_connections ORDER BY environment")
        return cursor.fetchall()
    finally:
        cursor.close()

def get_active_vsphere_connections(db_conn):
    """僅取得所有「已啟用」的 vSphere 連線設定。"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, environment, host, user, is_active FROM vsphere_connections WHERE is_active = 1 ORDER BY environment")
        return cursor.fetchall()
    finally:
        cursor.close()

def get_vsphere_connection_by_env(db_conn, environment):
    """根據 environment 取得單一 vSphere 連線設定（包含解密的密碼）。"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT host, user, password FROM vsphere_connections WHERE environment = %s AND is_active = 1",
            (environment,)
        )
        conn_info = cursor.fetchone()
        if conn_info:
            conn_info['password'] = _decrypt_password(conn_info['password'])
        return conn_info
    finally:
        cursor.close()

def add_or_update_vsphere_connection(db_conn, env, host, user, password_plain):
    """新增或更新一筆 vSphere 連線設定。"""
    cursor = db_conn.cursor()
    try:
        encrypted_pass = _encrypt_password(password_plain)
        cursor.execute("""
            INSERT INTO vsphere_connections (environment, host, user, password)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            host = VALUES(host),
            user = VALUES(user),
            password = VALUES(password)
        """, (env, host, user, encrypted_pass))
        db_conn.commit()
    finally:
        cursor.close()

def update_connection_password(db_conn, conn_id, new_password_plain):
    """更新指定連線的密碼。"""
    cursor = db_conn.cursor()
    try:
        encrypted_pass = _encrypt_password(new_password_plain)
        cursor.execute("UPDATE vsphere_connections SET password = %s WHERE id = %s", (encrypted_pass, conn_id))
        db_conn.commit()
    finally:
        cursor.close()

def delete_vsphere_connection_by_id(db_conn, conn_id):
    """根據 ID 刪除一筆 vSphere 連線設定。"""
    cursor = db_conn.cursor()
    try:
        cursor.execute("DELETE FROM vsphere_connections WHERE id = %s", (conn_id,))
        db_conn.commit()
    finally:
        cursor.close()

def toggle_connection_status(db_conn, conn_id):
    """切換指定連線的 is_active 狀態。"""
    cursor = db_conn.cursor()
    try:
        cursor.execute("UPDATE vsphere_connections SET is_active = NOT is_active WHERE id = %s", (conn_id,))
        db_conn.commit()
    finally:
        cursor.close()

def get_vsphere_connection_by_id(db_conn, conn_id):
    """根據 ID 取得單一 vSphere 連線設定（包含解密的密碼）。"""
    cursor = db_conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT host, user, password FROM vsphere_connections WHERE id = %s",
            (conn_id,)
        )
        conn_info = cursor.fetchone()
        if conn_info:
            conn_info['password'] = _decrypt_password(conn_info['password'])
        return conn_info
    finally:
        cursor.close()