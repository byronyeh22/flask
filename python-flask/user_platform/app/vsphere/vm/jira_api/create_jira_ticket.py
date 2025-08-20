import requests
import json

# --- 輔助函式，用於產生不同的 Jira 內容 ---

def _generate_create_summary(data):
    """為 Create 操作產生 Jira 標題"""
    env = data.get('environment', 'N/A')
    action = data.get('action_type', 'Create')
    prefix = data.get('vm_name_prefix', 'N/A')
    os_type = data.get('os_type', data.get('vm_os_type', 'N/A')).capitalize()
    instance = data.get('vm_instance_type', 'N/A')
    return f"[VM Provisioning] {env} - {action} {prefix} - {os_type} ({instance})"

def _generate_update_summary(data):
    """為 Update 操作產生 Jira 標題"""
    config = data.get('new_config', {})
    env = config.get('environment', 'N/A')
    action = config.get('action_type', 'Update')
    prefix = config.get('vm_name_prefix', 'N/A')
    return f"[VM Provisioning] {env} - {action} {prefix}"

def _generate_update_description(data):
    """為 Update 操作產生詳細的 Jira 描述 (使用 Jira Wiki Markup)"""
    original = data.get('original_config', {})
    new = data.get('new_config', {})
    desc_parts = [
        f"Request to update VM: *{new.get('vm_name_prefix')}*",
        "---",
        "{panel:title=Configuration Changes|borderStyle=dashed|borderColor=#ccc|titleBGColor=#F7F7F7}"
    ]

    # 比較 CPU
    if str(original.get('vm_num_cpus')) != str(new.get('vm_num_cpus')):
        desc_parts.append(f"• *vCPU:* {original.get('vm_num_cpus', 'N/A')} -> *{new.get('vm_num_cpus', 'N/A')}*")

    # 比較 Memory
    if str(original.get('vm_memory')) != str(new.get('vm_memory')):
        desc_parts.append(f"• *Memory (MB):* {original.get('vm_memory', 'N/A')} -> *{new.get('vm_memory', 'N/A')}*")

    # 比較 Disk
    # 原始磁碟資料可能是 JSON 字串，新資料是列表，需要統一格式再比較
    try:
        original_disks = json.loads(original.get('vm_disk_size', '[]'))
    except (json.JSONDecodeError, TypeError):
        original_disks = original.get('vm_disk_size', [])
    new_disks = new.get('vm_disk_size', [])
    
    if original_disks != new_disks:
         desc_parts.append(f"• *Disks (GB):* {original_disks} -> *{new_disks}*")

    desc_parts.append("{panel}")
    return "\n".join(desc_parts)


# --- 主要函式 ---

def create_jira_ticket(ticket_data):
    """
    建立 Jira 工單。
    此函式現在可以處理 Create (扁平字典) 和 Update (巢狀字典) 兩種請求。
    """
    jira_base = "https://sanbox888twuat.atlassian.net"
    # 提醒：建議將敏感資訊 (如 API Token) 移至環境變數或設定檔中
    auth = (
        "srv.sra@sanbox888.tw",
        "ATATT3xFfGF0R9x--AYy2vPdYkG25_w52yHTrGG4wGfBwMbnsyxDMoFmSPL54MtfecWNeoLQR2_0hY73MhBh0m1njA057j8b-9qdFX4TPVlngRu9mkYq1p9TVdXei1_a0FcSt_GgaK2Ae7f8fU8v-PiDSfljnMr63Ce1TuiFApMSdxeFih-_WUE=346474B9"
    )

    # 【關鍵修正】
    # 透過檢查 'new_config' 鍵是否存在，來判斷是 Create 還是 Update
    is_update = 'new_config' in ticket_data

    if is_update:
        # --- 處理 Update 請求 ---
        summary = _generate_update_summary(ticket_data)
        description = _generate_update_description(ticket_data)
    else:
        # --- 處理 Create 請求 (沿用您原本的邏輯) ---
        summary = _generate_create_summary(ticket_data)
        description = "Auto-generated VM creation request."

    # --- 準備並發送 Jira API 請求 (共通邏輯) ---
    payload = {
        "fields": {
            "project": {"key": "SJT"},
            "issuetype": {"name": "vsphere_vm"},
            "summary": summary,
            "description": description,
        }
    }

    try:
        response = requests.post(
            f"{jira_base}/rest/api/2/issue/",
            json=payload,
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()  # 如果請求失敗 (e.g., 400, 401, 404), 會拋出異常

        data = response.json()
        ticket_id = data["key"]
        print(f"Successfully created Jira ticket: {ticket_id}")
        return ticket_id

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred while creating Jira ticket: {http_err} - {response.text}")
        # 重新拋出異常，讓外層的 try...except 可以捕捉到並顯示 flash message
        raise
    except requests.exceptions.RequestException as req_err:
        print(f"Request error occurred while creating Jira ticket: {req_err}")
        raise
    except KeyError as key_err:
        print(f"Key error after creating ticket (likely parsing response): {key_err} - Response: {response.text}")
        raise