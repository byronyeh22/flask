import hvac
import logging
from flask import current_app
from urllib.parse import urlparse # 匯入 urlparse 函式

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