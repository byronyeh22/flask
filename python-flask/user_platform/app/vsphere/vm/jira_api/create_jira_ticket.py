import requests
import json
from flask import current_app # 導入 current_app

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


# --- [NEW] Create 動作的 Jira 描述（使用 Jira Wiki Markup） ---

def _as_list(v):
    """把單值或 list 都轉成 list，None -> []"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]

def _render_table(headers, rows):
    """
    Jira Wiki Markup 表格產生器
    headers: ["Col1", "Col2"]
    rows:    [["a","b"], ["c","d"]]
    """
    out = []
    out.append("|| " + " || ".join(str(h) for h in headers) + " ||")
    for r in rows:
        out.append("| " + " | ".join("" if (x is None) else str(x) for x in r) + " |")
    return "\n".join(out)

def _generate_create_description(data: dict) -> str:
    """
    將 Create VM 表單轉成多段 Panel + Table 的 Jira Wiki Markup。
    會處理你表單的常見欄位，以及 additional disks（create_vm_disk_size[] / create_vm_disk_provisioning[]）。
    """
    # 先把磁碟欄位標準化（你的表單 key 可能是 'create_vm_disk_size[]' 與 'create_vm_disk_provisioning[]'）
    sizes = _as_list(data.get('create_vm_disk_size[]', data.get('vm_disk_size', [])))
    provs = _as_list(data.get('create_vm_disk_provisioning[]', data.get('vm_disk_provisioning', [])))

    # === Summary 區塊 ===
    summary_tbl = _render_table(
        ["Field", "Value"],
        [
            ("Action",         data.get('action_type', 'Create')),
            ("Resource",       data.get('resource', 'vm')),
            ("Environment",    data.get('environment', '-')),
            ("vSphere Host",    data.get('vsphere_host', '-')),
            ("VM Name Prefix", data.get('vm_name_prefix', '-')),
        ]
    )

    # === Core 區塊 ===
    core_tbl = _render_table(
        ["Field", "Value"],
        [
            ("OS Type",         (data.get('os_type') or data.get('vm_os_type') or '-')),
            ("Instance Type",   data.get('vm_instance_type', '-')),
            ("vCPU",            data.get('vm_num_cpus', '-')),
            ("Memory (MB)",     data.get('vm_memory', '-')),
        ]
    )

    # === vSphere 區塊 ===
    vsphere_tbl = _render_table(
        ["Field", "Value"],
        [
            ("Datacenter",  data.get('vsphere_datacenter','-')),
            ("Cluster",     data.get('vsphere_cluster','-')),
            ("ESXi Host",   data.get('vsphere_esxi_host','-')),
            ("Template",    data.get('vsphere_template','-')),
            ("Datastore",   data.get('vsphere_datastore','-')),
            ("Network",     data.get('vsphere_network','-')),
            ("IPv4 Gateway",data.get('vm_ipv4_gateway','-')),
        ]
    )

    # === NetBox 區塊 ===
    netbox_tbl = _render_table(
        ["Field", "Value"],
        [
            ("Prefix", data.get('netbox_prefix','-')),
            ("Tenant", data.get('netbox_tenant','-')),
        ]
    )

    # === Additional Disks 區塊 ===
    disk_rows = []
    for i, size in enumerate(sizes):
        prov = provs[i] if i < len(provs) else "thin"
        disk_rows.append([i+1, size, prov])
    disks_tbl = _render_table(["Num", "Size (GB)", "Provisioning"], disk_rows) if disk_rows else "_No additional disks_"

    # === 組描述（用 Panel 包住每個區塊，比較清楚） ===
    parts = []
    parts.append("{panel:title=Summary|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(summary_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=Core|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(core_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=vSphere|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(vsphere_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=NetBox|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(netbox_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=Additional Disks|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(disks_tbl)
    parts.append("{panel}")

    return "\n".join(parts)


# --- 主要函式 ---
def create_jira_ticket(ticket_data):
    """
    建立 Jira 工單。
    可處理 Create(扁平) / Update(巢狀 new_config) / Delete(可選)。
    """
    jira_base = current_app.config['JIRA_BASE_URL']
    auth = (
        current_app.config['JIRA_USER'],
        current_app.config['JIRA_API_TOKEN']
    )

    # 判斷 action_type（update/delete/create）
    action_type = (ticket_data.get("action_type")
                   or ticket_data.get("new_config", {}).get("action_type")
                   or "").lower()

    if action_type == 'create' or 'new_config' not in ticket_data:
        # --- Create ---
        summary = _generate_create_summary(ticket_data)
        description = _generate_create_description(ticket_data)

    elif ('new_config' in ticket_data) or (action_type == 'update'):
        # --- Update ---
        summary = _generate_update_summary(ticket_data)
        description = _generate_update_description(ticket_data)

    elif action_type == 'delete':
        # --- Delete ---
        try:
            summary = _generate_delete_summary(ticket_data)
            description = _generate_delete_description(ticket_data)
        except NameError:
            summary = f"[VM Provisioning] {ticket_data.get('environment','N/A')} - Delete {ticket_data.get('vm_name_prefix','N/A')}"
            description = "Delete request (details TODO)."

    else:
        raise ValueError(f"Unsupported action_type: {action_type}")

    payload = {
        "fields": {
            "project": {"key": "SJT"},
            "issuetype": {"name": "Task"},
            "summary": summary,
            "description": description,
        }
    }

    try:
        resp = requests.post(
            f"{jira_base}/rest/api/2/issue/",
            json=payload,
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return data["key"]

    except requests.exceptions.HTTPError as http_err:
        # 確保不會引用未定義的 response 物件
        text = getattr(http_err.response, "text", "")
        print(f"HTTP error occurred while creating Jira ticket: {http_err} - {text}")
        raise
    except requests.exceptions.RequestException as req_err:
        print(f"Request error occurred while creating Jira ticket: {req_err}")
        raise
    except KeyError as key_err:
        # 防呆：resp 可能不存在
        body = ""
        try:
            body = resp.text  # 若前面有成功拿到 resp
        except Exception:
            pass
        print(f"Key error after creating ticket (likely parsing response): {key_err} - Response: {body}")
        raise