from mysql.connector import Error
import logging
from cryptography.fernet import Fernet
from flask import current_app
from app.mysql.db import get_db_connection
from app.vsphere.vm.vault.vault_manager import VaultManager

# --- Encryption Utilities (Integrated) ---
def _get_cipher():
    """使用 App 設定的 FERNET_KEY 建立 Fernet 加解密器。"""
    # 🔴【修正】修正縮排錯誤
    key = current_app.config['FERNET_KEY']
    return Fernet(key.encode('utf-8'))

def _encrypt_password(password: str) -> str:
    """加密密碼（回傳字串）。"""
    #【修正】修正縮排錯誤
    if not password: return ""
    return _get_cipher().encrypt(password.encode('utf-8')).decode('utf-8')

def _decrypt_password(encrypted_password: str) -> str:
    """解密密碼字串（回傳明文）。"""
    # 修正縮排錯誤
    if not encrypted_password: return ""
    return _get_cipher().decrypt(encrypted_password.encode('utf-8')).decode('utf-8')


def add_or_update_vsphere_connection(env, host, user, password_plain):
    """
    新增或更新 vSphere 連線，並同步到 Vault。
    """
    db_conn = get_db_connection()
    try:
        # 1. 加密密碼並存入 DB
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
        logging.info(f"Successfully saved connection for {host} to database.")

        # 2. 同步到 Vault
        vault_manager = VaultManager()
        success, message = vault_manager.store_vsphere_credentials(
            environment=env,
            host=host,
            user=user,
            password=password_plain
        )
        if not success:
            logging.warning(f"Vault sync failed for {host}: {message}")
            raise Exception(f"Database save succeeded, but Vault sync failed: {message}")

    except Exception as e:
        if db_conn and db_conn.is_connected():
            db_conn.rollback()
        logging.error(f"Failed to add/update connection for {host}: {e}")
        raise
    finally:
        if db_conn:
            db_conn.close()

def update_connection_password(conn_id, current_password_plain, new_password_plain):
    """
    更新指定連線的密碼，並同步更新 Vault。
    """
    db_conn = get_db_connection()
    conn_info_for_vault = None
    try:
        with db_conn.cursor(dictionary=True) as cursor:
            # 1) 查詢目前的連線資訊 (為 Vault 同步預先準備)
            cursor.execute("SELECT environment, host, user, password FROM vsphere_connections WHERE id = %s", (conn_id,))
            conn_info_for_vault = cursor.fetchone()
            if not conn_info_for_vault:
                return (False, "Connection not found.")

            # 2) 驗證舊密碼
            stored_encrypted_pass = conn_info_for_vault["password"]
            stored_decrypted_pass = _decrypt_password(stored_encrypted_pass)
            if stored_decrypted_pass != current_password_plain:
                return (False, "Current password does not match.")

            # 3) 更新 DB 中的新密碼
            new_encrypted_pass = _encrypt_password(new_password_plain)
            cursor.execute(
                "UPDATE vsphere_connections SET password = %s, updated_at = NOW() WHERE id = %s",
                (new_encrypted_pass, conn_id)
            )
            db_conn.commit()
            logging.info("✅ Password updated in DB for vsphere_connection id=%s", conn_id)

        # 4) 同步更新到 Vault
        vault_manager = VaultManager()
        vault_success, vault_message = vault_manager.store_vsphere_credentials(
            environment=conn_info_for_vault['environment'],
            host=conn_info_for_vault['host'],
            user=conn_info_for_vault['user'],
            password=new_password_plain # 使用新的明文密碼
        )
        if not vault_success:
            # DB 已更新成功，Vault 失敗僅記錄警告
            logging.warning(f"DB password updated, but Vault sync failed for conn_id {conn_id}: {vault_message}")
            return (True, "Password updated in DB, but failed to sync to Vault.")

        return (True, "Password updated successfully in DB and Vault.")

    except Error as e:
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        logging.error("❌ DB error during password update for conn_id %s: %s", conn_id, e)
        return (False, "Database error occurred.")
    except Exception as e:
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        logging.error("❌ Unexpected error during password update for conn_id %s: %s", conn_id, e)
        return (False, "An unexpected error occurred.")
    finally:
        if db_conn:
            db_conn.close()

def delete_vsphere_connection_by_id(conn_id):
    """
    根據 ID 刪除 vSphere 連線設定，並同步從 Vault 刪除。
    """
    db_conn = get_db_connection()
    try:
        # 1. 先從資料庫讀取連線資訊，以便知道要刪除哪個 Vault 路徑
        conn_info = get_vsphere_connection_by_id(conn_id)
        if not conn_info:
            logging.warning(f"Attempted to delete non-existent connection with id={conn_id}")
            return 0 # 找不到紀錄，回傳 0

        # 2. 從 Vault 刪除
        vault_manager = VaultManager()
        vault_success, vault_message = vault_manager.delete_vsphere_credentials(
            environment=conn_info['environment'],
            host=conn_info['host']
        )
        if not vault_success:
            # Vault 刪除失敗，記錄錯誤但繼續往下刪除 DB
            logging.error(f"Failed to delete from Vault for conn_id {conn_id}, but proceeding with DB deletion. Error: {vault_message}")

        # 3. 從資料庫刪除
        with db_conn.cursor() as cursor:
            cursor.execute("DELETE FROM vsphere_connections WHERE id = %s", (conn_id,))
            affected = cursor.rowcount
        db_conn.commit()
        
        logging.info(f"Successfully deleted connection id={conn_id} from database.")
        return affected

    except Error as e:
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        logging.error(f"[delete_vsphere_connection_by_id] DB error: {e}")
        raise
    except Exception as e:
        if db_conn and db_conn.is_connected(): db_conn.rollback()
        logging.error(f"[delete_vsphere_connection_by_id] Unexpected error: {e}")
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
            return [r[0] for r in rows]
    finally:
        if db_conn:
            db_conn.close()

def get_vsphere_connection_by_host(host, require_active=True):
    """
    用 host 取得單一 vSphere 連線設定（包含解密的密碼）。
    預設僅允許 is_active = 1；如需忽略啟用狀態，require_active=False。
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