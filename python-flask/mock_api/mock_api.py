from flask import Flask, jsonify, request
from datetime import datetime, timezone, timedelta
import random

mock_app = Flask(__name__)

def utc_now_iso():
    # ISO8601 with timezone (Z)；你的 insert 會做正規化
    return datetime.now(timezone.utc).isoformat()

# --- 模擬 vSphere API ---
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

# =========================
# Mock GitLab API
# =========================

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
    pipeline_id = random.randint(1000, 9999)

    payload = {
        # 你 insert 會先找 pipeline_id，再退回 id
        "pipeline_id": pipeline_id,
        "id": pipeline_id,

        "project_id": project_id,                 # 用來組 project_name 或其他欄位
        "ref": "main",
        "sha": "mock-sha-12345",
        "status": "created",                      # 觸發當下先 created
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",

        # 你 insert 會優先取 started_at，其次 created_at
        "created_at": utc_now_iso(),
        "started_at": utc_now_iso(),              # 直接給，避免 None
        "finished_at": None,                      # 尚未完成
        "duration": None,                         # 尚未有 duration

        # 透傳你觸發時塞進來的 form 變數（可選）
        "variables": dict(request.form)
    }
    return jsonify(payload)


# ===== in-memory state & helpers (add these) =====
PIPELINES = {}                   # pipeline_id -> state dict

DEFAULT_RUN_SECONDS = 120          # 進入 running 後維持秒數

def _utc_now():
    return datetime.now(timezone.utc)

def iso_z(dt):
    if not isinstance(dt, datetime):
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

            # 時間線：觸發即開始；按 Play 後才會設定 finish_at
            "created_at_dt": created_at,
            "started_at_dt": created_at,                 # ★ 觸發就開始
            "played_at_dt":  None,                       # ★ 等你按 Play
            "finish_at_dt":  None,                       #   = played_at_dt + post_run_seconds

            # 執行段（不含等待）秒數：可調整（這裡預設：前段 0 秒、後段 DEFAULT_RUN_SECONDS 秒）
            "pre_run_seconds": 0,                        # 例如想模擬 job1=3s，就改成 3
            "post_run_seconds": DEFAULT_RUN_SECONDS,

            "status": "manual",
            "created_at": iso_z(created_at),
            "started_at": iso_z(created_at),
            "finished_at": None,
            "duration": None,

            "force_fail": False,
        }

    # 如果狀態已經是 'canceled'，就直接回傳，不再重新計算
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
            "finished_at": p.get("finished_at") or iso_z(now), # 如果 finished_at 不存在，就用現在的時間
            "duration": p.get("duration")
        }), 200

    # 尚未按 Play：一直 manual（但 started_at 已存在）
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
                # ★ duration 只計執行段（前段 + 後段），不含等待 Play 的時間
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
        "started_at": started_at,          # 觸發時間（Z）
        "finished_at": finished_at,        # 完成時間（Z/None）
        "duration": duration               # 只計執行段秒數
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
            apply_status = "failed" if p["force_fail"] else "success"
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
    """
    模擬 cancel_manual_jobs.py 中取消整條 pipeline 的行為。
    """
    # 從記憶體中尋找對應的 pipeline
    pipeline = PIPELINES.get(pipeline_id)
    if not pipeline:
        # 如果找不到，回傳 404 Not Found
        return jsonify({"message": "404 Pipeline Not Found"}), 404

    # 更新記憶體中 pipeline 的狀態為 'canceled'
    pipeline['status'] = 'canceled'
    pipeline['finished_at'] = utc_now_iso() # 標記一個完成時間

    # 回傳一個類似真實 GitLab API 的成功回應
    return jsonify({
        "id": pipeline_id,
        "project_id": project_id,
        "status": "canceled",
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",
    }), 200

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/jobs/<int:job_id>/play', methods=['POST'])
def run_manual_job(project_id, job_id):
    # 找這個 project_id 最新的一條 pipeline（未完成者優先）
    candidates = [x for x in PIPELINES.values() if x["project_id"] == project_id]
    if not candidates:
        return jsonify({"error": "pipeline not found"}), 404
    unfinished = [x for x in candidates if not x["finished_at"]]
    p = max(unfinished or candidates, key=lambda x: x["created_at_dt"])

    # 允許用表單參數覆蓋跑多久（秒）
    try:
        run_secs = int(request.form.get("RUN_SECONDS", "") or 0)
    except ValueError:
        run_secs = 0
    if run_secs <= 0:
        run_secs = p.get("post_run_seconds", DEFAULT_RUN_SECONDS)
    # 同步回到 state（以便 GET 時也用同樣值）
    p["post_run_seconds"] = run_secs

    # 第一次按 Play：開始跑，設定 finish_at_dt
    if p["played_at_dt"] is None:
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
# Mock Jira API
# =========================

# 用於保存 mock 的 tickets（以便 GET 時能回傳一致資料）
mock_jira_tickets = {}

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

if __name__ == "__main__":
    mock_app.run(host="0.0.0.0", port=5001, debug=True)