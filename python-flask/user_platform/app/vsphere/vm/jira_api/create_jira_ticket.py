import requests
import json
from flask import current_app # 導入 current_app

# --- 輔助函式，用於產生不同的 Jira 內容 ---

def _generate_create_summary(data):
    """為 Create 操作產生 Jira 標題"""
    env = data.get('environment', 'N/A')
    action = data.get('action_type', 'create')
    prefix = data.get('vm_name_prefix', 'N/A')
    os_type = data.get('os_type')
    instance = data.get('vm_instance_type', 'N/A')
    return f"[VM Provisioning] {env} - {action} {prefix} - {os_type} ({instance})"

def _generate_update_summary(data):
    """為 Update 操作產生 Jira 標題"""
    # print(f"DEBUG: _generate_update_summary received data keys: {list(data.keys())}")
    # print(f"DEBUG: data structure: {data}")

    original_config = data.get('original_config', {})
    new_config = data.get('new_config', {})

    # print(f"DEBUG: original_config keys: {list(original_config.keys())}")
    # print(f"DEBUG: new_config keys: {list(new_config.keys())}")

    # 標準化磁碟相關欄位的格式，確保它們始終是列表
    disk_fields = [
        'update_disk_db_id[]', 'update_disk_label[]', 'update_vm_disk_size[]',
        'update_vm_disk_provisioning[]', 'update_vm_disk_eagerly_scrub[]',
        'update_vm_disk_thin_provisioned[]'
    ]

    for field in disk_fields:
        if field in new_config:
            # 如果欄位存在但不是列表，則將其轉換為列表
            if not isinstance(new_config[field], list):
                new_config[field] = [new_config[field]]

    env = original_config.get('environment', 'N/A')
    action = data.get('action_type', 'update')
    prefix = original_config.get('vm_name_prefix', 'N/A')
    os_type = original_config.get('os_type', 'N/A')
    instance = original_config.get('vm_instance_type', 'N/A')

    # print(f"DEBUG: Final values - env: {env}, prefix: {prefix}, os_type: {os_type}, instance: {instance}")

    return f"[VM Provisioning] {env} - {action} {prefix} - {os_type} ({instance})"

def _generate_delete_summary(data):
    """為 Delete 操作產生 Jira 標題"""
    # Delete 的 payload 結構：{ original_config: {}, delete_config: {}, action_type: "delete" }

    original_config = data.get('original_config', {})

    # 優先從 delete_config 取，若無則從 original_config 取
    env = original_config.get('environment', 'N/A')
    action = data.get('action_type', 'delete')
    prefix = original_config.get('vm_name_prefix', 'N/A')
    os_type = original_config.get('os_type', 'N/A')
    instance = original_config.get('vm_instance_type', 'N/A')

    return f"[VM Provisioning] {env} - {action} {prefix} - {os_type} ({instance})"

def _generate_update_description(data):
    """為 Update 操作產生詳細的 Jira 描述 (使用 Jira Wiki Markup)"""
    original = data.get('original_config', {})
    new = data.get('new_config', {})

    vm_name = new.get('vm_name_prefix') or original.get('vm_name_prefix') or 'Unknown VM'

    desc_parts = [
        f"Request to update VM: *{vm_name}*",
        "---",
        "{panel:title=Configuration Changes|borderStyle=dashed|borderColor=#ccc|titleBGColor=#F7F7F7}"
    ]

    changes_found = False

    # 比較 CPU
    orig_cpu = str(original.get('vm_num_cpus', '')).strip()
    new_cpu = str(new.get('vm_num_cpus', '')).strip()
    if orig_cpu != new_cpu and new_cpu and orig_cpu:
        desc_parts.append(f"• *vCPU:* {orig_cpu} -> *{new_cpu}*")
        changes_found = True

    # 比較 Memory
    orig_mem = str(original.get('vm_memory', '')).strip()
    new_mem = str(new.get('vm_memory', '')).strip()
    if orig_mem != new_mem and new_mem and orig_mem:
        desc_parts.append(f"• *Memory (MB):* {orig_mem} -> *{new_mem}*")
        changes_found = True

    # 比較磁碟 - 正確處理磁碟變更
    disk_changes = _compare_disk_changes(original, new)
    if disk_changes:
        desc_parts.extend(disk_changes)
        changes_found = True

    # 如果沒有發現任何變更，添加一個說明
    if not changes_found:
        desc_parts.append("• *No configuration changes detected*")

    desc_parts.append("{panel}")

    return "\n".join(desc_parts)

def _compare_disk_changes(original_config, new_config):
    """比較磁碟變更並產生變更描述"""
    changes = []

    # 從 original_config 提取現有磁碟
    original_disks = {}
    if 'additional_disks' in original_config:
        for disk in original_config['additional_disks']:
            disk_id = disk.get('id')
            if disk_id:
                original_disks[str(disk_id)] = {
                    'size': disk.get('size'),
                    'label': disk.get('label'),
                    'provisioning': disk.get('disk_provisioning'),
                    'ui_number': disk.get('ui_disk_number')
                }

    # 從 new_config 提取更新後的磁碟
    updated_disks = {}
    disk_ids = new_config.get('update_disk_db_id[]', [])
    disk_labels = new_config.get('update_disk_label[]', [])
    disk_sizes = new_config.get('update_vm_disk_size[]', [])
    disk_provisionings = new_config.get('update_vm_disk_provisioning[]', [])

    for i, disk_id in enumerate(disk_ids):
        # 處理空字串的 disk_id (新增的磁碟)
        # 將其轉換為唯一識別符
        if not disk_id:
            disk_id = f"new_{i}"

        updated_disks[str(disk_id)] = {
            'size': int(disk_sizes[i]) if i < len(disk_sizes) and disk_sizes[i] else None,
            'label': disk_labels[i] if i < len(disk_labels) else None,
            'provisioning': disk_provisionings[i] if i < len(disk_provisionings) else None
        }

    # 找出被移除的磁碟
    removed_disks = set(original_disks.keys()) - set(updated_disks.keys())
    for disk_id in removed_disks:
        disk_info = original_disks[disk_id]
        changes.append(f"• *Removed Disk {disk_info['label']}:* {disk_info['size']} GB ({disk_info['provisioning']})")

    # 找出新增的磁碟（如果有的話）
    added_disks = [key for key in updated_disks.keys() if key.startswith('new_') or key not in original_disks]
    for disk_id in added_disks:
        disk_info = updated_disks[disk_id]
        if disk_info['label'] and disk_info['size'] and disk_info['provisioning']:
            changes.append(f"• *Added Disk:* {disk_info['label']} - {disk_info['size']} GB ({disk_info['provisioning']})")

    # 找出修改的磁碟
    common_disks = [key for key in original_disks.keys() if key in updated_disks and not key.startswith('new_')]
    for disk_id in common_disks:
        orig = original_disks[disk_id]
        new = updated_disks[disk_id]

        disk_changes = []
        if orig['size'] != new['size']:
            disk_changes.append(f"size: {orig['size']} -> {new['size']} GB")
        if orig['provisioning'] != new['provisioning']:
            disk_changes.append(f"provisioning: {orig['provisioning']} -> {new['provisioning']}")

        if disk_changes:
            changes.append(f"• *Modified Disk {orig['label']}:* {', '.join(disk_changes)}")

    return changes


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


def _generate_delete_description(data: dict) -> str:
    """
    將 Delete VM 表單轉成多段 Panel + Table 的 Jira Wiki Markup。
    處理 Delete 操作的相關欄位資訊。
    """
    # 從 original_config 中取得 VM 資訊
    original = data.get('original_config', {})

    # === Summary 區塊 ===
    summary_tbl = _render_table(
        ["Field", "Value"],
        [
            ("Action",         data.get('action_type', 'Delete')),
            ("Resource",       original.get('resource', 'vm')),
            ("Environment",    original.get('environment', '-')),
            ("vSphere Host",   original.get('vsphere_host', '-')),
            ("VM Name Prefix", original.get('vm_name_prefix', '-')),
        ]
    )

    # === VM Information 區塊 ===
    vm_info_tbl = _render_table(
        ["Field", "Value"],
        [
            ("OS Type",         original.get('os_type', '-')),
            ("Instance Type",   original.get('vm_instance_type', '-')),
            ("vCPU",            original.get('vm_num_cpus', '-')),
            ("Memory (MB)",     original.get('vm_memory', '-')),
        ]
    )

    # === vSphere 區塊 ===
    vsphere_tbl = _render_table(
        ["Field", "Value"],
        [
            ("Datacenter",  original.get('vsphere_datacenter', '-')),
            ("Cluster",     original.get('vsphere_cluster', '-')),
            ("ESXi Host",   original.get('vsphere_esxi_host', '-')),
            ("Template",    original.get('vsphere_template', '-')),
            ("Datastore",   original.get('vsphere_datastore', '-')),
            ("Network",     original.get('vsphere_network', '-')),
        ]
    )

    # === Disks Information 區塊 ===
    disk_rows = []
    if 'additional_disks' in original:
        for i, disk in enumerate(original['additional_disks']):
            disk_rows.append([
                i + 1,
                disk.get('label', '-'),
                disk.get('size', '-'),
                disk.get('disk_provisioning', '-')
            ])
    disks_tbl = _render_table(
        ["Num", "Label", "Size (GB)", "Provisioning"], 
        disk_rows
    ) if disk_rows else "_No additional disks_"

    # === 組成描述(用 Panel 包住每個區塊,比較清楚) ===
    parts = []
    parts.append("*Request to delete the following VM:*")
    parts.append("")

    parts.append("{panel:title=Summary|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(summary_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=VM Information|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(vm_info_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=vSphere Configuration|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(vsphere_tbl)
    parts.append("{panel}")

    parts.append("{panel:title=Disks to be Removed|borderStyle=solid|borderColor=#dfe1e6|titleBGColor=#F4F5F7}")
    parts.append(disks_tbl)
    parts.append("{panel}")

    parts.append("")
    parts.append("⚠️ *Warning:* This operation will permanently delete the VM and all associated data.")

    return "\n".join(parts)

# --- 主要函式 ---
def create_jira_ticket(ticket_data: dict, db_conn=None, workflow_id=None, check_existing=True) -> str:
    # 初始化設置
    jira_base = current_app.config['JIRA_BASE_URL']
    auth = (current_app.config['JIRA_USER'], current_app.config['JIRA_API_TOKEN'])

    # 獲取並標準化 action_type
    action_type = (ticket_data.get("action_type", "") or "").strip().lower()
    print(f"Creating Jira ticket with action_type: {action_type}, data keys: {list(ticket_data.keys())}")

    # 根據 action_type 生成內容
    if action_type == "update":
        summary = _generate_update_summary(ticket_data)
        description = _generate_update_description(ticket_data)
        print("Using UPDATE template for Jira ticket")
    elif action_type == "delete":
        summary = _generate_delete_summary(ticket_data)
        description = _generate_delete_description(ticket_data)
        print("Using DELETE template for Jira ticket")
    else:
        # 預設走 create
        summary = _generate_create_summary(ticket_data)
        description = _generate_create_description(ticket_data)
        print("Using CREATE template for Jira ticket")

    # 建立 payload 並發送請求
    payload = {
        "fields": {
            "project": {"key": "SJT"},
            "issuetype": {"name": "Task"},
            "summary": summary,
            "description": description,
        }
    }

    # 發送請求
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
    except Exception as e:
        raise RuntimeError(f"Failed to create Jira ticket: {e}")