# app/vsphere/vm/db/vsphere_connections_manager.py
from mysql.connector import Error
import logging
from cryptography.fernet import Fernet
from flask import current_app
from app.mysql.db import get_db_connection

# --- Encryption Utilities (Integrated) ---

def _get_cipher():
    """使用 App 設定的 FERNET_KEY 建立 Fernet 加解密器。"""
    key = current_app.config['FERNET_KEY']
    return Fernet(key.encode('utf-8'))

def _encrypt_password(password: str) -> str:
    """加密密碼（回傳字串）。"""
    if not password:
        return ""
    cipher = _get_cipher()
    encrypted_bytes = cipher.encrypt(password.encode('utf-8'))
    return encrypted_bytes.decode('utf-8')

def _decrypt_password(encrypted_password: str) -> str:
    """解密密碼字串（回傳明文）。"""
    if not encrypted_password:
        return ""
    cipher = _get_cipher()
    decrypted_bytes = cipher.decrypt(encrypted_password.encode('utf-8'))
    return decrypted_bytes.decode('utf-8')

# --- Database Management Functions ---

def get_all_vsphere_connections():
    """
    取得所有 vSphere 連線設定（不含密碼）
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT id, environment, host, user, is_active
                FROM vsphere_connections
                ORDER BY environment, host
            """)
            return cursor.fetchall()
    finally:
        if db_conn:
            db_conn.close()

def get_active_vsphere_connections():
    """
    取得所有 active 的 vSphere 連線設定（不含密碼）
    """
    try:
        db_conn = get_db_connection()
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT id, environment, host, user, is_active
                FROM vsphere_connections
                WHERE is_active = 1
                ORDER BY environment, host
            """)
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"[get_active_vsphere_connections] DB error: {e}")
        return []
    finally:
        if db_conn:
            db_conn.close()

def get_hosts_by_environment(environment, active_only: bool = True):
    """
    取得指定 environment 底下的 host 清單。
    預設僅回傳 is_active = 1 的 host。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            if active_only:
                cursor.execute("""
                    SELECT host
                    FROM vsphere_connections
                    WHERE environment = %s AND is_active = 1
                    ORDER BY host
                """, (environment,))
            else:
                cursor.execute("""
                    SELECT host
                    FROM vsphere_connections
                    WHERE environment = %s
                    ORDER BY host
                """, (environment,))
            rows = cursor.fetchall() or []
            # rows 為 list[tuple] -> 只取第一欄 host
            return [r[0] for r in rows]
    finally:
        if db_conn:
            db_conn.close()
def get_vsphere_connection_by_host(host, require_active=True):
    """
    用 host 取得單一 vSphere 連線設定（包含解密的密碼）。
    預設僅允許 is_active = 1；如需忽略啟用狀態，require_active=False。
    （本函式自行建立/關閉 DB 連線）
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            if require_active:
                cursor.execute(
                    """
                    SELECT id, environment, host, user, password, is_active
                    FROM vsphere_connections
                    WHERE host = %s AND is_active = 1
                    """,
                    (host,)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, environment, host, user, password, is_active
                    FROM vsphere_connections
                    WHERE host = %s
                    """,
                    (host,)
                )
            conn_info = cursor.fetchone()

        if conn_info:
            try:
                conn_info['password'] = _decrypt_password(conn_info['password'])
            except Exception as e:
                conn_info['password'] = None
                conn_info['decrypt_error'] = f"Password decrypt failed: {type(e).__name__}: {e}"

        return conn_info

    except Error as e:
        logging.error(f"[get_vsphere_connection_by_host] DB error (host={host}): {e}")
        raise
    except Exception as e:
        logging.error(f"[get_vsphere_connection_by_host] Unexpected error (host={host}): {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()

def add_or_update_vsphere_connection(env, host, user, password_plain):
    """
    新增或更新一筆 vSphere 連線設定。
    以 host 為唯一鍵（UNIQUE KEY uq_vsphere_host (host)）。
    - 若已存在相同 host，則更新 environment / user / password。
    - is_active 不在此函式內強制覆蓋（沿用既有狀態），避免意外啟用或停用。
    """
    db_conn = get_db_connection()
    try:
        encrypted_pass = _encrypt_password(password_plain)
        with db_conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO vsphere_connections (environment, host, user, password)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    environment = VALUES(environment),
                    user        = VALUES(user),
                    password    = VALUES(password)
            """, (env, host, user, encrypted_pass))
        db_conn.commit()
    finally:
        if db_conn:
            db_conn.close()

def update_connection_password(conn_id, current_password_plain, new_password_plain):
    """
    更新指定連線的密碼（先驗證 current_password）。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            # 1) 查詢目前的加密密碼
            cursor.execute("SELECT password FROM vsphere_connections WHERE id = %s", (conn_id,))
            result = cursor.fetchone()
            if not result:
                return (False, "Connection not found.")

            stored_encrypted_pass = result["password"]
            stored_decrypted_pass = _decrypt_password(stored_encrypted_pass)

            # 2) 驗證舊密碼
            if stored_decrypted_pass != current_password_plain:
                return (False, "Current password does not match.")

            # 3) 更新為新密碼
            new_encrypted_pass = _encrypt_password(new_password_plain)
            cursor.execute(
                "UPDATE vsphere_connections SET password = %s WHERE id = %s",
                (new_encrypted_pass, conn_id)
            )
            db_conn.commit()

            logging.info("✅ Password updated for vsphere_connection id=%s", conn_id)
            return (True, "Password updated successfully.")

    except Error as e:
        if db_conn:
            db_conn.rollback()
        logging.error("❌ DB error during password update for conn_id %s: %s", conn_id, e)
        return (False, "Database error occurred.")
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error("❌ Unexpected error during password update for conn_id %s: %s", conn_id, e)
        return (False, "An unexpected error occurred.")
    finally:
        if db_conn:
            db_conn.close()

def delete_vsphere_connection_by_id(conn_id):
    """
    根據 ID 刪除一筆 vSphere 連線設定。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            cursor.execute("DELETE FROM vsphere_connections WHERE id = %s", (conn_id,))
            affected = cursor.rowcount
        db_conn.commit()
        return affected
    except Error as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"[delete_vsphere_connection_by_id] DB error: {e}")
        raise
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"[delete_vsphere_connection_by_id] Unexpected error: {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()

def toggle_connection_status(conn_id):
    """
    切換指定連線的 is_active 狀態。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor() as cursor:
            cursor.execute(
                "UPDATE vsphere_connections SET is_active = NOT is_active WHERE id = %s",
                (conn_id,)
            )
            affected = cursor.rowcount
        db_conn.commit()
        return affected
    except Error as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"[toggle_connection_status] DB error: {e}")
        raise
    except Exception as e:
        if db_conn:
            db_conn.rollback()
        logging.error(f"[toggle_connection_status] Unexpected error: {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()

def get_vsphere_connection_by_id(conn_id):
    """
    根據 ID 取得單一 vSphere 連線設定（包含解密的密碼）。
    """
    db_conn = get_db_connection()
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT id, environment, host, user, password, is_active
                FROM vsphere_connections
                WHERE id = %s
            """, (conn_id,))
            conn_info = cursor.fetchone()

        if conn_info:
            try:
                conn_info['password'] = _decrypt_password(conn_info['password'])
            except Exception as e:
                conn_info['password'] = None
                conn_info['decrypt_error'] = f"Password decrypt failed: {type(e).__name__}: {e}"

        return conn_info
    except Error as e:
        logging.error(f"[get_vsphere_connection_by_id] DB error: {e}")
        raise
    except Exception as e:
        logging.error(f"[get_vsphere_connection_by_id] Unexpected error: {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()