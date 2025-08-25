import requests
import json
import math
from flask import current_app  # 導入 current_app

# 每個 SCSI 控制器可掛的「額外碟」數量上限（unit 1..15 排除 7 => 14 顆）
_CAPACITY_PER_CONTROLLER = 14

def _clamp(n, lo, hi):
    try:
        n = int(n)
    except Exception:
        return lo
    return max(lo, min(hi, n))

def _compute_scsi_count_from_disks(disks):
    """
    估算需要的 SCSI 控制器數：
    優先：若磁碟有帶 scsi_controller，回傳 max(bus)+1（1..4）。
    否則：把所有非系統碟視為掛在 bus0，按 14 顆/控制器做 ceil。
    另外：若出現 unit_number == 15，且算出為 1，強制提升到 2（避免 provider 的 1 控制器限制）。
    """
    try:
        buses = []
        has_any_bus = False
        non_system_count = 0
        has_unit_15 = False

        for d in (disks or []):
            if not isinstance(d, dict):
                continue

            # 計數非系統碟
            try:
                bus = int(d.get("scsi_controller")) if d.get("scsi_controller") is not None else None
            except Exception:
                bus = None
            try:
                unit = int(d.get("unit_number")) if d.get("unit_number") is not None else None
            except Exception:
                unit = None

            if bus is not None:
                has_any_bus = True
                buses.append(bus)

            # 排除 scsi(0:0)
            if not (bus == 0 and unit == 0):
                # 只要是有效 row 就視為一顆「額外碟」
                if unit is not None:
                    non_system_count += 1
                    if unit == 15:
                        has_unit_15 = True

        if has_any_bus and buses:
            return _clamp(max(buses) + 1, 1, 4)

        # 無 bus 資訊 → 視為都掛在 bus0，用容量規則估算
        import math
        cnt = max(1, math.ceil(non_system_count / _CAPACITY_PER_CONTROLLER))
        if has_unit_15 and cnt < 2:
            cnt = 2
        return _clamp(cnt, 1, 4)
    except Exception:
        return 1

def _sanitize_disks_for_tf(disks):
    """
    Terraform 用的 disks 陣列清理：
    - 保留所有合法 slot（含 scsi(1:0), scsi(2:0) 等），只有 scsi(0:0) 視為系統碟予以排除
    - 去除 UI/內部欄位（ui_disk_number, scsi_controller）後輸出
    - 以 (bus, unit) 去重：同一 bus 上同一 unit 若重複，保留第一個
    - 依 (bus, unit, id) 穩定排序後輸出，避免「看起來隨機」的變動
    """
    # 先把 (bus, unit) 算出來用於排序/去重；輸出前再移除 bus
    norm = []
    for d in (disks or []):
        if not isinstance(d, dict):
            continue
        try:
            bus = int(d.get("scsi_controller")) if d.get("scsi_controller") is not None else 0
        except Exception:
            bus = 0
        try:
            unit = int(d.get("unit_number"))
        except Exception:
            continue  # 沒 unit 就沒法放

        # 排除系統碟：僅限 scsi(0:0)
        if bus == 0 and unit == 0:
            continue

        norm.append((bus, unit, d))

    # 依 (bus, unit, id) 穩定排序
    norm.sort(key=lambda x: (x[0], x[1], x[2].get("id") or 0))

    # 以 (bus, unit) 去重：同一控制器同一 unit 若重複，保留第一個
    seen = set()
    out = []
    for bus, unit, d in norm:
        key = (bus, unit)
        if key in seen:
            continue
        seen.add(key)

        clean = dict(d)
        # clean.pop("ui_disk_number", None)
        clean.pop("scsi_controller", None)  # TF 不吃 bus，仍移除
        out.append(clean)

    return out

def trigger_gitlab_pipeline(jira_key, form_data):
    """
    觸發 GitLab CI pipeline (正規化版本)。
    - VM_SCSI_CONTROLLER_COUNT：新增傳給 Terraform 的控制器數量變數
    - VM_ADDITIONAL_DISKS_JSON：以 DB/後端整理的 additional_disks 為主，並移除 ui_disk_number/scsi_controller
    """
    gitlab_url = current_app.config['GITLAB_URL']
    project_id = current_app.config['GITLAB_PROJECT_ID']
    token = current_app.config['GITLAB_TRIGGER_TOKEN']
    branch = current_app.config['GITLAB_BRANCH']
    trigger_url = f"{gitlab_url}/api/v4/projects/{project_id}/trigger/pipeline"

    # 以 DB / 後端整理好的 additional_disks 為主
    additional_disks = form_data.get('additional_disks', [])
    disks_for_tf = _sanitize_disks_for_tf(additional_disks)

    # 1) 先尊重上游直接提供的 vm_scsi_controller_count
    scsi_cnt = form_data.get('vm_scsi_controller_count')

    # 2) 若沒有提供或空字串，就用我們的推估（依 14 顆/控制器計算）
    if scsi_cnt is None or str(scsi_cnt).strip() == "":
        scsi_cnt = _compute_scsi_count_from_disks(additional_disks)

    # Terraform 端用 jsondecode() 解析
    vm_additional_disks_json = json.dumps(disks_for_tf, ensure_ascii=False)

    variables = {
        "JIRA_TICKET_NUM": jira_key,
        "ACTION_TYPE": form_data.get('action_type', ''),
        "ENVIRONMENT": form_data.get('environment', ''),
        "RESOURCE": form_data.get('resource', ''),
        "OS_TYPE": form_data.get('os_type', ''),
        "VSPHERE_DATACENTER": form_data.get('vsphere_datacenter', ''),
        "VSPHERE_CLUSTER": form_data.get('vsphere_cluster', ''),
        "VSPHERE_NETWORK": form_data.get('vsphere_network', ''),
        "VSPHERE_TEMPLATE": form_data.get('vsphere_template', ''),
        "VSPHERE_DATASTORE": form_data.get('vsphere_datastore', ''),
        "VM_NAME_PREFIX": form_data.get('vm_name_prefix', ''),
        "VM_INSTANCE_TYPE": form_data.get('vm_instance_type', ''),
        "VM_NUM_CPUS": str(form_data.get('vm_num_cpus', '')),
        "VM_MEMORY": str(form_data.get('vm_memory', '')),
        "VM_IPV4_GATEWAY": form_data.get('vm_ipv4_gateway', ''),
        "NETBOX_PREFIX": form_data.get('netbox_prefix', ''),
        "NETBOX_TENANT": form_data.get('netbox_tenant', ''),
        "VM_SCSI_CONTROLLER_COUNT": str(scsi_cnt),
        "VM_ADDITIONAL_DISKS_JSON": vm_additional_disks_json,
    }

    payload = {"token": token, "ref": branch}
    for key, value in variables.items():
        payload[f"variables[{key}]"] = value

    try:
        response = requests.post(trigger_url, data=payload, timeout=30)
        response.raise_for_status()
        pipeline_data = response.json()
        return {
            "success": True,
            "pipeline_id": str(pipeline_data.get("id", "")),
            "id": str(pipeline_data.get("id", "")),
            "web_url": pipeline_data.get("web_url", ""),
            "sha": pipeline_data.get("sha", ""),
            "ref": pipeline_data.get("ref", branch),
            "status": pipeline_data.get("status", "pending"),
            "project_id": project_id,
            "variables": variables
        }
    except requests.exceptions.HTTPError as http_err:
        error_msg = f"HTTP error occurred: {http_err} - {getattr(http_err.response, 'text', '')}"
        print(error_msg)
        return {"success": False, "error": error_msg, "pipeline_id": None}
    except requests.exceptions.RequestException as req_err:
        error_msg = f"Request error occurred: {req_err}"
        print(error_msg)
        return {"success": False, "error": error_msg, "pipeline_id": None}
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        print(error_msg)
        return {"success": False, "error": error_msg, "pipeline_id": None}

# Example usage
if __name__ == "__main__":
    # 模擬：第一次就加到需要第 2 顆控制器（第 15 顆額外碟）
    sample_form_data = {
        "environment": "sandbox",
        "resource": "vm",
        "os_type": "windows",
        "vsphere_datacenter": "TPSBLAB",
        "vsphere_cluster": "RICH55688",
        "vsphere_network": "VM Network",
        "vsphere_template": "SRA-Test-Winserver",
        "vsphere_datastore": "TWSBESXI02_LD01",
        "vm_name_prefix": "sra-test",
        "vm_instance_type": "Generic",
        "vm_num_cpus": 2,
        "vm_memory": 4096,
        "vm_ipv4_gateway": "172.26.1.1",
        "netbox_prefix": "172.26.1.0/24",
        "netbox_tenant": "RCBC-34",
        "additional_disks": [
            {"unit_number": u, "size": 10, "disk_provisioning": "thin", "thin_provisioned": True}
            for u in ([1,2,3,4,5,6,8,9,10,11,12,13,14,15] + [1])  # 15 顆「額外碟」→ 需要 2 顆控制器
        ],
        # 上游若已提供就會覆蓋推估：
        # "vm_scsi_controller_count": 2,
    }
    res = trigger_gitlab_pipeline("Jira-123", sample_form_data)
    print(res)