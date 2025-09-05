import requests
import logging
from flask import current_app

def cancel_manual_jobs(pipeline_id: str):
    """
    取消指定 pipeline 底下的 manual job。
    - 先嘗試逐一取消 manual job（POST /projects/:id/jobs/:job_id/cancel）
    - 若不支援或失敗，改取消整條 pipeline（POST /projects/:id/pipelines/:pipeline_id/cancel）
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
            logging.info(f"No manual jobs found for pipeline #{pipeline_id}. Attempting to cancel the entire pipeline.")
            return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)

        canceled_ids = []

        # 2) 嘗試逐一取消 manual job
        for job in manual_jobs:
            job_id = job.get("id")
            try:
                r = requests.post(
                    f"{gitlab_url}/api/v4/projects/{project_id}/jobs/{job_id}/cancel",
                    headers=headers,
                    timeout=10
                )
                if r.status_code in (404, 405):
                    logging.warning(f"Canceling individual job #{job_id} is not supported (status: {r.status_code}). Falling back to cancel the entire pipeline.")
                    return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)

                r.raise_for_status()
                canceled_ids.append(job_id)

            except requests.RequestException as e:
                logging.warning(f"Failed to cancel job #{job_id} due to a request error: {e}. Falling back to cancel the entire pipeline.")
                return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)

        if canceled_ids:
            return {
                "success": True,
                "pipeline_id": pipeline_id,
                "canceled_job_ids": canceled_ids
            }

        # 3) 如果迴圈跑完但沒有任何 job 被取消 (例如都失敗了)，也執行後備方案
        return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)

    except requests.RequestException as req_err:
        logging.warning(f"Could not fetch jobs for pipeline #{pipeline_id}: {req_err}. Attempting to cancel the entire pipeline as a fallback.")
        return _cancel_pipeline(gitlab_url, project_id, headers, pipeline_id)
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
        # GitLab 在 pipeline 已經結束 (如 success/failed) 時取消會回 400 (Bad Request)，這不算是一個真正的錯誤
        if http_err.response.status_code == 400:
            logging.warning(f"Pipeline #{pipeline_id} could not be canceled (it may have already completed).")
            return {"success": True, "pipeline_id": pipeline_id, "pipeline_canceled": False, "message": "Pipeline already completed."}
        return {"success": False, "error": f"HTTP error: {http_err}", "pipeline_id": pipeline_id}
    except requests.RequestException as req_err:
        return {"success": False, "error": f"Request error: {req_err}", "pipeline_id": pipeline_id}
    except Exception as e:
        return {"success": False, "error": str(e), "pipeline_id": pipeline_id}