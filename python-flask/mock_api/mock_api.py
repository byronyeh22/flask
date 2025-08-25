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

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>', methods=['GET'])
def get_pipeline_status(project_id, pipeline_id):
    """
    模擬 get_pipeline_status_from_gitlab.py 的回應
    回傳 manual 以讓 workflow_monitor 在條件成立時把 workflow_runs.status -> PENDING_APPROVAL
    """
    now = datetime.now(timezone.utc)
    payload = {
        "id": pipeline_id,
        "status": "manual",
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",
        "ref": "main",
        "sha": "mock-sha-12345",
        "created_at": (now - timedelta(minutes=5)).isoformat(),
        "updated_at": now.isoformat(),
        "finished_at": None,
        "duration": None
    }
    return jsonify(payload)

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>/jobs', methods=['GET'])
def get_gitlab_jobs(project_id, pipeline_id):
    """模擬 get_pipeline_jobs 的回應（含 manual 的 apply job）"""
    return jsonify([
        {
            "id": 1234,
            "name": "terraform-plan",
            "stage": "plan",
            "status": "success",
            "web_url": f"http://mock-gitlab.com/projects/{project_id}/jobs/1234"
        },
        {
            "id": 1235,
            "name": "terraform-apply",
            "stage": "apply",
            "status": "manual",
            "web_url": f"http://mock-gitlab.com/projects/{project_id}/jobs/1235"
        }
    ])

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/jobs/<int:job_id>/play', methods=['POST'])
def run_manual_job(project_id, job_id):
    """模擬 run_manual_job.py 的回應"""
    return jsonify({
        "id": job_id,
        "name": "terraform-apply",
        "stage": "apply",
        "status": "running"
    })

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