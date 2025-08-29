# app/vsphere/vm/vsphere_api/test_connection.py
from pyVim.connect import SmartConnect, Disconnect
import ssl
import socket
from flask import current_app

def test_vsphere_connection(host, user, password, port=443, thumbprint=None, timeout=10):
    """
    測試連線 vCenter/ESXi：
    - API_MODE == 'local' 時直接回傳成功（mock）
    - 非 local 時實際連線，提供更清楚的錯誤訊息
    """
    if current_app.config.get('API_MODE') == 'local':
        print(f"Running in local mode. Mocking connection test for host '{host}'.")
        return (True, "Connection successful (mock).")

    # 先做基礎網路檢查，避免 SSL/驗證前就卡住
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except socket.timeout:
        return (False, f"Network timeout: cannot reach {host}:{port}")
    except OSError as e:
        return (False, f"Network error: cannot reach {host}:{port} ({e})")

    # SSL context：不驗證憑證（如果你使用自簽憑證，這樣較寬鬆；若要嚴格驗證可改成載入 CA）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # 注意：某些舊文章會提 SmartConnectNoSSL；新版本已不建議。用 sslContext 才是正解。
    # 另外，很多人會寫 disableSslCertValidation=True，但在多數 pyVmomi 版本這不是有效參數，請不要帶。
    si = None
    try:
        # thumbprint：若要用指紋驗證，可改走 pyVmomi 的 ThumbprintVerifier
        #（這段示範保留接口；實務上要引入 SmartConnect 的 thumbprint verifier）
        if thumbprint:
            # 若要嚴格以 thumbprint 驗證，改採用：SmartConnect(host=..., user=..., pwd=..., port=..., b64encodedThumbprint=..., connectionPoolTimeout=timeout)
            # 但不同版 pyVmomi 參數略有差異；若不確定版本，先不要用 thumbprint。
            pass

        si = SmartConnect(
            host=host,
            user=user,
            pwd=password,
            port=port,
            sslContext=ctx,
            # connectionPoolTimeout 不是所有版本都有；若你的版本支援可打開：
            # connectionPoolTimeout=timeout
        )

        if si:
            Disconnect(si)
            return (True, "Connection successful.")
        else:
            return (False, "Connection failed: SmartConnect returned no session (si is None).")

    except ssl.SSLError as e:
        return (False, f"SSL error during handshake: {e}")
    except Exception as e:
        # 回傳更完整訊息以利判讀
        return (False, f"Connection failed: {type(e).__name__}: {e!s}")