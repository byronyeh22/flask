# app/vsphere/vm/gitlab_api/cancel_manual_jobs.py

import requests
from flask import current_app

def cancel_manual_jobs(pipeline_id: str):
    """
    取消指定 pipeline 底下的 manual job。
    - 先嘗試逐一取消 manual job（POST /projects/:id/jobs/:job_id/cancel）
    - 若不支援或失敗，改取消整條 pipeline（POST /projects/:id/pipelines/:pipeline_id/cancel）

    Returns:
        dict:
          success: bool
          pipeline_id: str
          canceled_job_ids: list[int]   # 若有逐一取消成功
          pipeline_canceled: bool       # 若改為取消整條 pipeline
          error: str                    # 失敗時
    """
    gitlab_url = current_app.config['GITLAB_URL']
    project_id = current_app.config['GITLAB_PROJECT_ID']
    headers = {
        "PRIVATE-TOKEN": current_app.config['GITLAB_PRIVATE_TOKEN']
    }

    try:
        # 1) 取得 pipeline 底下的所有 jobs
        resp = requests.get(
            f"{gitlab_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs",
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        jobs = resp.json()

        manual_jobs = [j for j in jobs if str(j.get("status")) == "manual"]
        if not manual_jobs:
            # 沒有 manual job，直接嘗試取消整條 pipeline（有些情況 pipeline 仍在 'created/pending' 等等）
            return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)

        canceled_ids = []
        cancel_supported = True

        # 2) 嘗試逐一取消 manual job
        for job in manual_jobs:
            job_id = job.get("id")
            try:
                r = requests.post(
                    f"{gitlab_url}/api/v4/projects/{project_id}/jobs/{job_id}/cancel",
                    headers=headers,
                    timeout=10
                )
                # 某些 GitLab 對 manual job 可能回 405/404，表示不支援單一 job cancel
                if r.status_code in (404, 405):
                    cancel_supported = False
                    break
                r.raise_for_status()
                canceled_ids.append(job_id)
            except requests.HTTPError as he:
                # 單一 job 取消失敗 → 視為整體策略改走 cancel pipeline
                cancel_supported = False
                break
            except requests.RequestException:
                cancel_supported = False
                break

        if canceled_ids and cancel_supported:
            return {
                "success": True,
                "pipeline_id": pipeline_id,
                "canceled_job_ids": canceled_ids
            }

        # 3) 若 manual job 取消不被支援或有失敗 → 取消整條 pipeline
        return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)

    except requests.HTTPError as http_err:
        return {"success": False, "error": f"HTTP error: {http_err}", "pipeline_id": pipeline_id}
    except requests.RequestException as req_err:
        return {"success": False, "error": f"Request error: {req_err}", "pipeline_id": pipeline_id}
    except Exception as e:
        return {"success": False, "error": str(e), "pipeline_id": pipeline_id}


def _cancel_pipeline(gitlab_url: str, project_id: str, headers: dict, pipeline_id: str):
    """
    呼叫 GitLab 取消整條 pipeline 的 API。
    """
    try:
        r = requests.post(
            f"{gitlab_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/cancel",
            headers=headers,
            timeout=10
        )
        r.raise_for_status()
        return {
            "success": True,
            "pipeline_id": pipeline_id,
            "pipeline_canceled": True
        }
    except requests.HTTPError as http_err:
        return {"success": False, "error": f"HTTP error: {http_err}", "pipeline_id": pipeline_id}
    except requests.RequestException as req_err:
        return {"success": False, "error": f"Request error: {req_err}", "pipeline_id": pipeline_id}
    except Exception as e:
        return {"success": False, "error": str(e), "pipeline_id": pipeline_id}


# Example usage
if __name__ == "__main__":
    # 測試 pipeline_id（請替換成實際 pipeline ID）
    pid = "1433"
    print("=== 測試取消 manual jobs（或取消整條 pipeline） ===")
    result = cancel_manual_jobs(pid)
    print(result)