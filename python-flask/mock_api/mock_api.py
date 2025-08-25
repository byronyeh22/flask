from flask import Flask, jsonify, request
from datetime import datetime, timezone  # 👈 新增

mock_app = Flask(__name__)

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

# --- 模擬 GitLab API ---
@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/trigger/pipeline', methods=['POST'])
def trigger_gitlab_pipeline(project_id):
    """模擬 trigger_gitlab_pipeline.py 的回應"""
    return jsonify({
        "id": "1433",
        "web_url": "http://mock-gitlab.com/pipelines/1433",
        "sha": "mock-sha-12345",
        "ref": "main",
        "status": "pending",
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc).isoformat(),  # 👈 模擬當下時間
        "variables": dict(request.form)
    })

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>/jobs', methods=['GET'])
def get_gitlab_jobs(project_id, pipeline_id):
    """模擬 get_pipeline_jobs 的回應"""
    return jsonify([
        {
            "id": "1234",
            "name": "terraform-plan",
            "stage": "plan",
            "status": "success",
            "web_url": f"http://mock-gitlab.com/projects/{project_id}/jobs/1234"
        },
        {
            "id": "1235",
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

@mock_app.route('/mock/gitlab/api/v4/projects/<int:project_id>/pipelines/<int:pipeline_id>', methods=['GET'])
def get_pipeline_status(project_id, pipeline_id):
    """模擬 get_pipeline_status_from_gitlab.py 的回應"""
    return jsonify({
        "id": pipeline_id,
        "status": "manual",
        "web_url": f"http://mock-gitlab.com/pipelines/{pipeline_id}",
        "ref": "main",
        "sha": "mock-sha-12345",
        "created_at": "2025-08-22T09:00:00.000Z",
        "updated_at": "2025-08-22T09:05:00.000Z",
        "finished_at": None,
        "duration": None
    })

# --- 模擬 Jira API ---
@mock_app.route('/mock/jira/rest/api/2/issue/', methods=['POST'])
def create_jira_ticket():
    """模擬 create_jira_ticket.py 的回應"""
    return jsonify({
        "id": "10000",
        "key": "SJT-888",
        "self": "http://mock-jira.com/rest/api/2/issue/10000"
    })

@mock_app.route('/mock/jira/rest/api/2/issue/<string:issue_id>', methods=['GET'])
def get_jira_issue(issue_id):
    """模擬 get_jira_issue_detail.py 的回應"""
    return jsonify({
        "key": issue_id,
        "fields": {
            "project": {"key": "SJT"},
            "summary": f"Mock ticket for {issue_id}",
            "description": "This is a mock description.",
            "status": {"name": "In Progress"}
        }
    })

if __name__ == "__main__":
    mock_app.run(host="0.0.0.0", port=5001, debug=True)