import hvac
import logging
from flask import current_app
from urllib.parse import urlparse # 匯入 urlparse 函式
import json

class VaultManager:
    def __init__(self):
        self.client = None
        if current_app.config.get('VAULT_ADDR') and current_app.config.get('VAULT_TOKEN'):
            try:
                self.client = hvac.Client(
                    url=current_app.config['VAULT_ADDR'],
                    token=current_app.config['VAULT_TOKEN']
                )
            except Exception as e:
                logging.error(f"Failed to initialize Vault client: {e}")

    def test_connection(self):
        """測試 Vault 連線與認證狀態"""
        if not self.client:
            return False, "Vault client is not configured in Flask app."
        try:
            if self.client.is_authenticated():
                return True, "Vault connection successful and authenticated."
            else:
                return False, "Vault connection failed: not authenticated."
        except Exception as e:
            return False, f"Vault connection failed: {str(e)}"

    def store_vsphere_credentials(self, environment, host, user, password):
        """將 vSphere 憑證存儲到 Vault KV v2"""
        if not self.client:
            return False, "Vault client is not configured."

        # 移除 http:// 開頭的 host
        sanitized_host_for_path = urlparse(host).netloc or host

        path_prefix = current_app.config.get('VAULT_PATH_PREFIX', 'secret')
        secret_path = f"{path_prefix}/{environment}/{sanitized_host_for_path}"

        # 存入 Vault 的資料使用原始的 host，確保連線正確
        secret_data = {
            "vsphere_host": host, # <-- 使用原始的、包含 http:// 的 host
            "vsphere_user": user,
            "vsphere_password": password
        }

        try:
            self.client.secrets.kv.v2.create_or_update_secret(
                path=secret_path,
                secret=secret_data,
            )
            logging.info(f"Successfully stored credentials for {host} in Vault at {secret_path}")
            return True, f"Successfully synced credentials for {host} to Vault."
        except Exception as e:
            logging.error(f"Failed to store credentials in Vault for {host}: {e}")
            return False, f"Failed to sync credentials to Vault: {str(e)}"

    def delete_vsphere_credentials(self, environment, host):
        """從 Vault 刪除 vSphere 連線 Secret"""
        if not self.client:
            return False, "Vault client is not configured."

         # 移除 http:// 開頭的 host
        sanitized_host_for_path = urlparse(host).netloc or host

        path_prefix = current_app.config.get('VAULT_PATH_PREFIX', 'secret')
        secret_path = f"{path_prefix}/{environment}/{sanitized_host_for_path}"

        try:
            self.client.secrets.kv.v2.delete_metadata_and_all_versions(path=secret_path)
            logging.info(f"Successfully deleted credentials for {host} from Vault at {secret_path}")
            return True, f"Successfully deleted secret for {host} from Vault."
        except hvac.exceptions.InvalidPath:
            logging.warning(f"Secret path {secret_path} not found in Vault, deletion skipped.")
            return True, "Secret not found in Vault, skipped deletion."
        except Exception as e:
            logging.error(f"Failed to delete credentials from Vault for {host}: {e}")
            return False, f"Failed to delete secret from Vault: {str(e)}"

    def get_vm_ipv4_ip(self, environment: str, os_type: str, vm_name_prefix: str):
        """從 Vault 取得 VM 的 IPv4 IP (路徑: {env}-{os_type}/{vm_name_prefix})"""
        if not self.client:
            logging.error("❌ [VAULT_CLIENT] Vault client is not configured.")
            return None

        # 1. 定義 Vault Mount Point 和相對路徑
        vault_mount_point = "secret"
        relative_path = f"{environment}-{os_type}/{vm_name_prefix}"
        full_secret_path = f"{vault_mount_point}/{relative_path}"

        logging.info(f"🔍 [VAULT_READ] Attempting to read from Vault:")
        logging.info(f"   - Mount point: {vault_mount_point}")
        logging.info(f"   - Relative path: {relative_path}")
        logging.info(f"   - Full path: {full_secret_path}")

        try:
            # 2. 檢查 Vault client 是否已認證
            if not self.client.is_authenticated():
                logging.error("❌ [VAULT_AUTH] Vault client is not authenticated")
                return None

            logging.info("✅ [VAULT_AUTH] Vault client is authenticated")

            # 3. 讀取 Secret
            read_response = self.client.secrets.kv.v2.read_secret_version(
                path=relative_path,
                mount_point=vault_mount_point
            )

            logging.info(f"📦 [VAULT_RESPONSE] Response received: {type(read_response)}")

            # 4. 解析 response
            if read_response and read_response.get("data") and read_response["data"].get("data"):
                secret_data = read_response["data"]["data"]
                all_keys = list(secret_data.keys())

                logging.info(f"📋 [VAULT_DATA] Secret found at {full_secret_path}")
                logging.info(f"   - Available keys: {all_keys}")
                logging.info(f"   - Secret data: {json.dumps(secret_data, indent=2)}")

                ip = secret_data.get("vm_host_ip")
                if ip:
                    logging.info(f"✅ [VAULT_IP] Successfully retrieved IP for {vm_name_prefix}: {ip}")
                    return ip
                else:
                    logging.warning(f"⚠️ [VAULT_KEY] Key 'vm_host_ip' not found in secret data")
                    logging.warning(f"   Available keys: {all_keys}")
                    logging.warning(f"   Full secret data: {secret_data}")
                    return None
            else:
                logging.warning(f"⚠️ [VAULT_EMPTY] Vault secret not found or data is empty at {full_secret_path}")
                logging.warning(f"   Response structure: {read_response}")
                return None

        except hvac.exceptions.InvalidPath as e:
            logging.error(f"❌ [VAULT_PATH] Vault path not found: {full_secret_path}")
            logging.error(f"   Error details: {str(e)}")
            return None
        except hvac.exceptions.Forbidden as e:
            logging.error(f"❌ [VAULT_PERMISSION] Permission denied reading {full_secret_path}")
            logging.error(f"   Error details: {str(e)}")
            return None
        except Exception as e:
            logging.error(f"❌ [VAULT_EXCEPTION] Failed to retrieve IP from Vault for {vm_name_prefix}")
            logging.error(f"   Path: {full_secret_path}")
            logging.error(f"   Error: {str(e)}")
            logging.error(f"   Error type: {type(e).__name__}")
            import traceback
            logging.error(f"   Full traceback:\n{traceback.format_exc()}")
            return None