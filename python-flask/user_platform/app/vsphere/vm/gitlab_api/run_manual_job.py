import requests
from flask import current_app  # 導入 current_app

def run_manual_job(pipeline_id):
    """
    執行 GitLab pipeline 中的 manual job

    Args:
        pipeline_id (str): Pipeline ID

    Returns:
        dict: 執行結果
    """
    gitlab_url = current_app.config['GITLAB_URL']
    project_id = current_app.config['GITLAB_PROJECT_ID']
    headers = {
        "PRIVATE-TOKEN": current_app.config['GITLAB_PRIVATE_TOKEN']
    }

    try:
        # 取得所有 jobs
        response = requests.get(
            f"{gitlab_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        jobs = response.json()

        # 過濾出 manual 狀態的 job
        manual_jobs = [job for job in jobs if job['status'] == 'manual']
        if not manual_jobs:
            return {"success": False, "error": "No manual job found"}

        job_id = manual_jobs[0]['id']

        # 執行 manual job
        play_response = requests.post(
            f"{gitlab_url}/api/v4/projects/{project_id}/jobs/{job_id}/play",
            headers=headers,
            timeout=10
        )
        play_response.raise_for_status()

        return {
            "success": True,
            "pipeline_id": pipeline_id,
            "job_id": job_id
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# Example usage for testing
if __name__ == "__main__":
    # 測試 pipeline_id（請替換成實際 pipeline ID）
    pipeline_id = "1433"

    print("=== 測試 GitLab 手動觸發 Manual Job ===")
    result = run_manual_job(pipeline_id)

    if result["success"]:
        print("\n✅ 成功執行 Manual Job：")
        print(f"   Pipeline ID : {result['pipeline_id']}")
        print(f"   Job ID      : {result['job_id']}")
    else:
        print(f"\n❌ 執行 Manual Job 失敗：{result['error']}")