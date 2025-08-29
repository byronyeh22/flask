from pyVim.connect import SmartConnect, Disconnect
import ssl
from flask import current_app

def test_vsphere_connection(host, user, password):
    """
    Attempts to connect to a vSphere server to validate credentials.
    Returns a mock success response if API_MODE is 'local'.
    """
    # 修正：將 API_MODE 的檢查放在函式最開頭
    if current_app.config.get('API_MODE') == 'local':
        print(f"Running in local mode. Mocking connection test for host '{host}'.")
        # 直接回傳成功，不再執行後續的 SmartConnect
        return (True, "Connection successful (mock).")

    # 只有在非 local 模式下，才執行實際的連線邏輯
    context = ssl._create_unverified_context()
    si = None
    try:
        si = SmartConnect(host=host, user=user, pwd=password, sslContext=context, disableSslCertValidation=True)
        if si:
            Disconnect(si)
            return (True, "Connection successful.")
        else:
            return (False, "Connection failed: Could not connect to server.")
    except Exception as e:
        return (False, f"Connection failed: {str(e)}")