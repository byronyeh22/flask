from flask import Flask, jsonify, request
from datetime import datetime, timezone, timedelta
import random

mock_app = Flask(__name__)

# =========================
# 通用 Helper
# =========================
def utc_now_iso():
    # ISO8601 with timezone (Z)；你的 insert 會做正規化
    return datetime.now(timezone.utc).isoformat()

def _utc_now():
    return datetime.now(timezone.utc)

def iso_z(dt):
    if not isinstance(dt, datetime):
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# =========================
# In-memory (GitLab / vSphere / Jira)
# =========================
PIPELINE_SEQ = 0  # 控制 pipeline id 起始值你可自行選，確保大於現有資料
PIPELINES = {}    # pipeline_id -> state dict

# vSphere in-memory
VMS = {}          # vm_name -> {"controllers": {bus: {"key": int}}, "disks": [{...}], "next_key": int}
                  # disk: {"key": int, "controller_bus": int, "controller_key": int, "unit_number": int,
                  #        "capacity_gb": int, "provision_type": str, "label_text": str, "label_number": int,
                  #        "vmdk_path": str}

# Jira in-memory
mock_jira_tickets = {}

DEFAULT_RUN_SECONDS = 30  # GitLab：進入 running 後維持秒數

# =========================
# GitLab Mock
# =========================
def next_pipeline_id() -> int:
    """Return a process-wide monotonically increasing pipeline id."""
    global PIPELINE_SEQ
    PIPELINE_SEQ += 1
    return PIPELINE_SEQ

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/trigger/pipeline', methods=['POST'])
def trigger_gitlab_pipeline(project_id):
    """
    模擬 trigger_gitlab_pipeline.py 的回應
    對齊 insert_gitlab_pipeline_info_to_db 需求：
      - pipeline_id / id（int）
      - project_id（int）
      - ref / sha / status / web_url（str）
      - created_at / started_at / finished_at / duration
    """
    # ✅ 改為單調遞增
    pipeline_id = next_pipeline_id()

    # 產生時間
    created_dt = _utc_now()
    created_iso = iso_z(created_dt)

    # 註冊到 in-memory
    PIPELINES[pipeline_id] = {
        "id": pipeline_id,
        "pipeline_id": pipeline_id,
        "project_id": project_id,
        "ref": "main",
        "sha": "mock-sha-12345",
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",

        # 狀態時間線
        "created_at_dt": created_dt,
        "started_at_dt": created_dt,  # 觸發就開始
        "played_at_dt":  None,
        "finish_at_dt":  None,

        "pre_run_seconds": 0,
        "post_run_seconds": DEFAULT_RUN_SECONDS,

        "status": "created",  # 觸發當下給 created（你也可直接給 manual）
        "created_at": created_iso,
        "started_at": created_iso,
        "finished_at": None,
        "duration": None,

        "force_fail": False,
    }

    # 回傳 payload
    return jsonify({
        "pipeline_id": pipeline_id,
        "id": pipeline_id,
        "project_id": project_id,
        "ref": "main",
        "sha": "mock-sha-12345",
        "status": "created",
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",
        "created_at": created_iso,
        "started_at": created_iso,
        "finished_at": None,
        "duration": None,
        "variables": dict(request.form)
    })

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>', methods=['GET'])
def get_pipeline_status(project_id, pipeline_id):
    now = _utc_now()
    p = PIPELINES.get(pipeline_id)
    if not p:
        created_at = now
        PIPELINES[pipeline_id] = p = {
            "id": pipeline_id,
            "pipeline_id": pipeline_id,
            "project_id": project_id,
            "ref": "main",
            "sha": "mock-sha-12345",
            "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",

            "created_at_dt": created_at,
            "started_at_dt": created_at,
            "played_at_dt":  None,
            "finish_at_dt":  None,

            "pre_run_seconds": 0,
            "post_run_seconds": DEFAULT_RUN_SECONDS,

            "status": "manual",
            "created_at": iso_z(created_at),
            "started_at": iso_z(created_at),
            "finished_at": None,
            "duration": None,

            "force_fail": False,
        }

    # 若已 canceled
    if p.get("status") == 'canceled':
        return jsonify({
            "id": p["id"],
            "status": "canceled",
            "web_url": p["web_url"],
            "ref": p["ref"],
            "sha": p["sha"],
            "created_at": p["created_at"],
            "updated_at": iso_z(now),
            "started_at": p["started_at"],
            "finished_at": p.get("finished_at") or iso_z(now),
            "duration": p.get("duration")
        }), 200

    # 尚未 Play
    if p["played_at_dt"] is None:
        status      = "manual"
        started_at  = p["started_at"]
        finished_at = None
        duration    = None
    else:
        # 已按 Play：跑到 finish 為止
        if now < p["finish_at_dt"]:
            status      = "running"
            started_at  = p["started_at"]
            finished_at = None
            duration    = None
        else:
            status = "failed" if p["force_fail"] else "success"
            if not p["finished_at"]:
                p["finished_at"] = iso_z(p["finish_at_dt"])
                p["duration"] = int(p.get("pre_run_seconds", 0) + p.get("post_run_seconds", DEFAULT_RUN_SECONDS))
            started_at  = p["started_at"]
            finished_at = p["finished_at"]
            duration    = p["duration"]

    p["status"] = status

    return jsonify({
        "id": p["id"],
        "status": status,
        "web_url": p["web_url"],
        "ref": p["ref"],
        "sha": p["sha"],
        "created_at": p["created_at"],
        "updated_at": iso_z(now),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration": duration
    }), 200

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>/jobs', methods=['GET'])
def get_gitlab_jobs(project_id, pipeline_id):
    p = PIPELINES.get(pipeline_id)
    now_iso = iso_z(_utc_now())
    job_id_plan  = int(f"{pipeline_id}01")
    job_id_apply = int(f"{pipeline_id}02")

    if not p or p["played_at_dt"] is None:
        apply_status = "manual"
        apply_started = None
        apply_finished = None
    else:
        if _utc_now() < p["finish_at_dt"]:
            apply_status = "running"
            apply_started = iso_z(p["played_at_dt"])
            apply_finished = None
        else:
            apply_status  = "failed" if p["force_fail"] else "success"
            apply_started = iso_z(p["played_at_dt"])
            apply_finished = p["finished_at"]

    return jsonify([
        {
            "id": job_id_plan,
            "name": "terraform-plan",
            "stage": "plan",
            "status": "success",
            "web_url": f"http://mock-gitlab.com/projects/{project_id}/jobs/{job_id_plan}",
            "created_at": now_iso, "started_at": now_iso, "finished_at": now_iso
        },
        {
            "id": job_id_apply,
            "name": "terraform-apply",
            "stage": "apply",
            "status": apply_status,
            "web_url": f"http://mock-gitlab.com/projects/{project_id}/jobs/{job_id_apply}",
            "created_at": now_iso,
            "started_at": apply_started,
            "finished_at": apply_finished
        }
    ]), 200

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>/cancel', methods=['POST'])
def cancel_gitlab_pipeline(project_id, pipeline_id):
    pipeline = PIPELINES.get(pipeline_id)
    if not pipeline:
        return jsonify({"message": "404 Pipeline Not Found"}), 404

    pipeline['status'] = 'canceled'
    pipeline['finished_at'] = utc_now_iso()

    return jsonify({
        "id": pipeline_id,
        "project_id": project_id,
        "status": "canceled",
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",
    }), 200

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/jobs/<int:job_id>/play', methods=['POST'])
def run_manual_job(project_id, job_id):
    pipeline_id = job_id // 100
    p = PIPELINES.get(pipeline_id)
    if not p or p.get("project_id") != project_id:
        return jsonify({"error": "pipeline not found for this job"}), 404

    try:
        run_secs = int(request.form.get("RUN_SECONDS", "") or 0)
    except ValueError:
        run_secs = 0
    if run_secs <= 0:
        run_secs = p.get("post_run_seconds", DEFAULT_RUN_SECONDS)
    p["post_run_seconds"] = run_secs

    # 第一次按 Play：開始跑
    if p.get("played_at_dt") is None:
        now = _utc_now()
        p["played_at_dt"] = now
        p["finish_at_dt"] = now + timedelta(seconds=run_secs)
        p["status"] = "running"

    return jsonify({
        "id": job_id,
        "name": "terraform-apply",
        "stage": "apply",
        "status": "running",
        "started_at": iso_z(p["played_at_dt"]),
        "finished_at": None,
        "duration": None
    }), 200


# =========================
# vSphere Mock (HTTP 版)
# =========================

# SCSI 規則
MAX_SCSI_BUS = 3          # 0..3
MAX_UNITS_PER_BUS = 16    # 0..15
RESERVED_UNIT = 7
OS_BOOT_SLOT = (0, 0)

def _vm_ensure(vm_name: str):
    vm = VMS.get(vm_name)
    if vm:
        return vm
    # 初始：controller bus=0, key=100，OS disk scsi(0:0)
    vm = {
        "controllers": {0: {"key": 100}},  # bus -> controller
        "disks": [],
        "next_key": 101
    }
    # OS disk
    os_disk = {
        "key": vm["next_key"],
        "controller_bus": 0,
        "controller_key": vm["controllers"][0]["key"],
        "unit_number": 0,
        "capacity_gb": 40,
        "provision_type": "thick_lazy",
        "label_text": "Hard disk 1",
        "label_number": 1,
        "vmdk_path": f"[mock-ds] {vm_name}/{vm_name}_0-0.vmdk"
    }
    vm["next_key"] += 1
    vm["disks"].append(os_disk)
    VMS[vm_name] = vm
    return vm

def _next_key(vm):
    k = vm["next_key"]
    vm["next_key"] += 1
    return k

def _ensure_controller(vm, bus: int):
    if bus in vm["controllers"]:
        return vm["controllers"][bus]
    if bus < 0 or bus > MAX_SCSI_BUS:
        raise ValueError(f"Invalid SCSI bus: {bus}")
    vm["controllers"][bus] = {"key": _next_key(vm)}
    return vm["controllers"][bus]

def _used_units_on_controller(vm, controller_key: int) -> set:
    return {d["unit_number"] for d in vm["disks"] if d["controller_key"] == controller_key}

def _find_next_slot_auto(vm) -> (int, int, int):
    """
    回傳 (controller_bus, controller_key, unit_number)
      - 先塞 bus=0，unit=1..15（跳 7；跳 OS 0）
      - 再 bus=1..3，unit=0..15（跳 7）
      - 不存在的 bus 自動建立
    """
    # 確保 bus0 存在
    c0 = _ensure_controller(vm, 0)

    # bus 0：unit 1..15，跳 7 與 0
    used0 = _used_units_on_controller(vm, c0["key"])
    for unit in range(MAX_UNITS_PER_BUS):
        if unit in (0, RESERVED_UNIT):  # 跳 OS 與 7
            continue
        if unit not in used0:
            return (0, c0["key"], unit)

    # bus 1..3
    for bus in range(1, MAX_SCSI_BUS + 1):
        c = vm["controllers"].get(bus)
        if not c:
            c = _ensure_controller(vm, bus)
            return (bus, c["key"], 0)  # 新 controller 第一顆用 0

        used = _used_units_on_controller(vm, c["key"])
        for unit in range(MAX_UNITS_PER_BUS):
            if unit == RESERVED_UNIT:
                continue
            if unit not in used:
                return (bus, c["key"], unit)

    raise RuntimeError("No available SCSI slot")

def _next_label_number(vm) -> int:
    """取現有磁碟數量 + 1 作為下一個編號"""
    return len(vm["disks"]) + 1

def _build_disk_json(d):
    return {
        "key": d["key"],
        "controller_bus": d["controller_bus"],
        "controller_key": d["controller_key"],
        "unit_number": d["unit_number"],
        "capacity_gb": d["capacity_gb"],
        "provision_type": d["provision_type"],
        "label_text": d["label_text"],
        "label_number": d["label_number"],
        "vmdk_path": d["vmdk_path"]
    }

@mock_app.route('/mock/vsphere/objects', methods=['GET'])
def get_vsphere_objects():
    """模擬 get_vsphere_objects.py 的回應"""
    return jsonify({
        "datacenters": ["mock-dc-1", "mock-dc-2"],
        "clusters": ["mock-cluster-a", "mock-cluster-b"],
        "templates": ["mock-template-win", "mock-template-linux"],
        "networks": ["mock-network-1", "mock-network-2"],
        "datastores": ["mock-datastore-1", "mock-datastore-2"],
        "vm_name": ["mock-vm-1", "mock-vm-2"],
    })

# ---- vSphere Mock: VM & Disk APIs ----
def _renumber_disk_labels(vm):
    """
    模擬 vSphere 刪除磁碟後自動重新編號 label 的行為
    按照 (controller_bus, unit_number) 排序後重新編號
    """
    # 排序所有磁碟（按位置排序）
    sorted_disks = sorted(vm["disks"], key=lambda d: (d["controller_bus"], d["unit_number"]))
    
    # 重新編號（從 1 開始）
    for i, disk in enumerate(sorted_disks, 1):
        disk["label_number"] = i
        disk["label_text"] = f"Hard disk {i}"

@mock_app.route('/mock/vsphere/vms/<string:vm_name>/ensure', methods=['POST'])
def vsphere_vm_ensure(vm_name):
    vm = _vm_ensure(vm_name)
    return jsonify({
        "vm": vm_name,
        "controllers": {str(bus): c["key"] for bus, c in vm["controllers"].items()},
        "disks": [_build_disk_json(d) for d in sorted(vm["disks"], key=lambda x: (x["controller_bus"], x["unit_number"]))]
    })

@mock_app.route('/mock/vsphere/vms/<string:vm_name>/disks', methods=['GET'])
def vsphere_list_disks(vm_name):
    vm = _vm_ensure(vm_name)
    disks = sorted(vm["disks"], key=lambda x: (x["controller_bus"], x["unit_number"]))
    # 跳過 OS 盤 0:0（與真實查詢一致）
    disks = [d for d in disks if not (d["controller_bus"] == 0 and d["unit_number"] == 0)]
    return jsonify([_build_disk_json(d) for d in disks])

@mock_app.route('/mock/vsphere/vms/<string:vm_name>/disks', methods=['POST'])
def vsphere_add_disk(vm_name):
    """
    body JSON:
    {
      "size_gb": 50,
      "provision_type": "thin" | "thick_lazy" | "thick_eager",
      "controller_id": 0   # optional：指定 bus；不給則自動分配
    }
    """
    body = request.get_json(silent=True) or {}
    size_gb = int(body.get("size_gb") or 0)
    if size_gb <= 0:
        return jsonify({"error": "size_gb must be > 0"}), 400
    provision_type = (body.get("provision_type") or "thin").lower().strip()
    if provision_type not in ("thin", "thick_lazy", "thick_eager"):
        provision_type = "thin"

    vm = _vm_ensure(vm_name)

    # 找 slot
    if body.get("controller_id") is not None:
        target_bus = int(body["controller_id"])
        ctrl = _ensure_controller(vm, target_bus)
        used = _used_units_on_controller(vm, ctrl["key"])
        start_unit = 1 if target_bus == 0 else 0
        unit = None
        for u in range(start_unit, MAX_UNITS_PER_BUS):
            if u == RESERVED_UNIT:
                continue
            if u not in used:
                unit = u
                break
        if unit is None:
            return jsonify({"error": f"No free unit on SCSI bus {target_bus}"}), 409
        controller_bus, controller_key, unit_number = target_bus, ctrl["key"], unit
    else:
        controller_bus, controller_key, unit_number = _find_next_slot_auto(vm)

    # 產生 label 與 key / 路徑
    label_number = _next_label_number(vm)
    label_text   = f"Hard disk {label_number}"
    disk_key     = _next_key(vm)
    vmdk_path    = f"[mock-ds] {vm_name}/{vm_name}_{controller_bus}-{unit_number}.vmdk"

    disk = {
        "key": disk_key,
        "controller_bus": controller_bus,
        "controller_key": controller_key,
        "unit_number": unit_number,
        "capacity_gb": size_gb,
        "provision_type": provision_type,
        "label_text": label_text,
        "label_number": label_number,
        "vmdk_path": vmdk_path
    }
    vm["disks"].append(disk)

    result = {
        "controller_bus": controller_bus,
        "unit_number": unit_number,
        "label_text": label_text,
        "label_number": label_number,
        "vmdk_path": vmdk_path,
        "size_gb": size_gb,
        "provision_type": provision_type,
        "disk_key": disk_key
    }
    return jsonify(result), 201

@mock_app.route('/mock/vsphere/vms/<string:vm_name>/disks/<int:disk_key>', methods=['DELETE'])
def vsphere_remove_disk(vm_name, disk_key):
    vm = _vm_ensure(vm_name)
    before = len(vm["disks"])
    vm["disks"] = [d for d in vm["disks"] if d["key"] != disk_key]
    if len(vm["disks"]) == before:
        return jsonify({"error": "disk not found"}), 404
    
    # ✅ 新增：重新編號所有磁碟的 label
    _renumber_disk_labels(vm)
    
    return jsonify({"message": "removed", "disk_key": disk_key})

@mock_app.route('/mock/vsphere/vms/<string:vm_name>/disks/<int:disk_key>', methods=['PATCH'])
def vsphere_resize_disk(vm_name, disk_key):
    body = request.get_json(silent=True) or {}
    try:
        new_size_gb = int(body.get("new_size_gb"))
    except Exception:
        return jsonify({"error": "new_size_gb required and must be int"}), 400
    if new_size_gb <= 0:
        return jsonify({"error": "new_size_gb must be > 0"}), 400

    vm = _vm_ensure(vm_name)
    target = None
    for d in vm["disks"]:
        if d["key"] == disk_key:
            target = d
            break
    if not target:
        return jsonify({"error": "disk not found"}), 404

    if new_size_gb < target["capacity_gb"]:
        return jsonify({"error": "cannot shrink disk"}), 400
    if new_size_gb == target["capacity_gb"]:
        return jsonify({"message": "no change", "capacity_gb": new_size_gb})

    target["capacity_gb"] = new_size_gb
    return jsonify({"message": "resized", "disk_key": disk_key, "capacity_gb": new_size_gb})

# =========================
# Mock Jira API
# =========================
@mock_app.route('/mock/jira/rest/api/2/issue/', methods=['POST'])
def create_jira_ticket():
    """
    模擬 create_jira_ticket.py 的回應
    建立隨機 ticket key，狀態預設 "To Do"
    """
    ticket_num  = random.randint(100, 999)
    ticket_key  = f"SJT-{ticket_num}"

    data        = request.get_json(silent=True) or {}
    fields      = data.get("fields", {})
    summary     = fields.get("summary", "No summary provided")
    description = fields.get("description", "No description provided")

    mock_jira_tickets[ticket_key] = {
        "summary": summary,
        "description": description,
        "status": "To Do"
    }

    return jsonify({
        "id": str(random.randint(10000, 19999)),
        "key": ticket_key,
        "self": f"http://mock-jira.com/rest/api/2/issue/{ticket_key}"
    })

@mock_app.route('/mock/jira/rest/api/2/issue/<string:issue_id>', methods=['GET'])
def get_jira_issue(issue_id):
    """
    模擬 get_jira_issue_detail.py 的回應
    """
    ticket_info = mock_jira_tickets.get(issue_id, {
        "summary": f"Mock ticket for {issue_id}",
        "description": "This is a default mock description.",
        "status": "To Do"
    })
    return jsonify({
        "key": issue_id,
        "fields": {
            "project": {"key": "SJT"},
            "summary": ticket_info["summary"],
            "description": ticket_info["description"],
            "status": {"name": ticket_info["status"]}
        }
    })

# =========================
# 入口
# =========================
if __name__ == "__main__":
    mock_app.run(host="0.0.0.0", port=5001, debug=True)