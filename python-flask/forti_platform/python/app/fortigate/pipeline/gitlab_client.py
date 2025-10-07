# app/fortigate/pipeline/gitlab_client.py
from typing import Optional, Dict, Any, List
import requests
import json
from app.db.mysql import get_db_connection
from typing import Set

# -----------------------------
# Helpers: GitLab config (DB only)
# -----------------------------
def _load_gitlab_info() -> dict:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        try:
            cur.execute(
                "SELECT url, token, project_id, ref, verify_ssl "
                "FROM gitlab_info ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        except Exception:
            cur.execute("SELECT url, token, project_id FROM gitlab_info ORDER BY id DESC LIMIT 1")
            row = cur.fetchone() or {}
            row["ref"] = "main"
            row["verify_ssl"] = True
        return row or {}
    finally:
        cur.close()
        conn.close()

def _bool(x) -> bool:
    if isinstance(x, bool): return x
    if x is None: return True
    return str(x).lower() not in ("0", "false", "no", "off")

def _raise_with_gitlab_message(resp: requests.Response):
    try:
        msg = resp.json()
    except Exception:
        msg = resp.text
    raise RuntimeError(f"GitLab API error {resp.status_code}: {msg}")

# -----------------------------
# Helpers: DB forti_tasks
# -----------------------------
def _get_task_row_by(task_id: int = 0, draft_id: int = 0) -> Optional[dict]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        row = None
        if task_id:
            cur.execute("SELECT * FROM forti_tasks WHERE id=%s LIMIT 1", (task_id,))
            row = cur.fetchone()
        if (not row) and draft_id:
            cur.execute(
                "SELECT * FROM forti_tasks WHERE draft_id=%s ORDER BY id DESC LIMIT 1",
                (draft_id,),
            )
            row = cur.fetchone()
        return row
    finally:
        cur.close()
        conn.close()

# -----------------------------
# Helpers: GitLab Jobs
# -----------------------------
def _get_jobs(base: str, project_id: int, pipeline_id: int, token: str, verify_ssl: bool) -> List[dict]:
    url = f"{base}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs?per_page=100"
    r = requests.get(url, headers={"PRIVATE-TOKEN": token}, timeout=20, verify=verify_ssl)
    if r.status_code // 100 != 2:
        _raise_with_gitlab_message(r)
    return r.json() or []

def _pick_job_by_name(jobs: List[dict], name: str) -> Optional[dict]:
    for j in jobs or []:
        if j.get("name") == name:
            return j
    return None

# -----------------------------
# Helpers: Get Device Information
# -----------------------------
def _load_devices_for_actions(action_plan: list) -> dict:
    """
    依 action_plan 內出現的 device_id 生成:
    {
      "2": {"host": "10.0.0.5:4443", "token": "xxxxx", "verify": False},
      "3": {"host": "fg.example.com", "token": "yyyyy", "verify": True},
    }
    """
    device_ids: Set[int] = set()
    for a in (action_plan or []):
        did = a.get("device_id")
        if did:
            try:
                device_ids.add(int(did))
            except Exception:
                pass
    if not device_ids:
        return {}

    sql = (
        "SELECT id, host, port, api_token, verify_ssl "
        "FROM forti_devices WHERE id IN (" +
        ",".join(["%s"] * len(device_ids)) + ")"
    )
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, tuple(sorted(device_ids)))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()

    devmap = {}
    for r in rows:
        host = str(r["host"] or "").strip()
        port = int(r.get("port") or 443)
        host_port = f"{host}:{port}" if port and port != 443 else host
        token = (r.get("api_token") or "").strip()
        verify = bool(r.get("verify_ssl", True))
        if host_port and token:
            devmap[str(int(r["id"]))] = {
                "host": host_port,
                "token": token,
                "verify": verify,
            }
    return devmap

# -----------------------------
# Public: kickoff / cancel / play
# -----------------------------
def kickoff_pipeline(
    *,
    task_id: int,
    draft_id: int,
    callback_token: str,
    draft_action_json: str,
    callback_url_validate: str,
    callback_url_apply_start: str,
    callback_url_apply: str,
    callback_url_canceled: str,
    device_map: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    觸發 GitLab Pipeline，並回傳 pipeline/job/commit 資訊。
    只投兩個變數：FORTI_PAYLOAD + FORTI_CALLBACK_TOKEN
    """
    info = _load_gitlab_info()
    base = (info.get("url") or "").rstrip("/")
    token = info.get("token")
    project_id = info.get("project_id")
    ref = info.get("ref") or "main"
    verify_ssl = _bool(info.get("verify_ssl"))

    if not base or not token or not project_id:
        raise RuntimeError("gitlab_info not configured (url/token/project_id missing)")

    # 1) kickoff URL：由 validate 路徑換成 /pipeline/kickoff/
    if not callback_url_validate:
        raise RuntimeError("callback_url_validate is required")
    callback_url_kickoff = callback_url_validate.replace(
        "/callbacks/validate/", "/callbacks/pipeline/kickoff/"
    )

    if callback_url_apply and "/callbacks/pipeline/apply/" not in callback_url_apply:
        callback_url_apply = callback_url_apply.replace(
            "/callbacks/apply/", "/callbacks/pipeline/apply/"
        )

    # 2) 從 draft_action_json 拆出 action_plan / options
    try:
        da = json.loads(draft_action_json or "{}")
    except Exception:
        da = {}
    action_plan = da.get("action_plan") or []
    options = da.get("options") or {}
    device_map = _load_devices_for_actions(action_plan)

    # 3) 組 FORTI_PAYLOAD（Runner 只讀這一包）
    forti_payload = {
        "version": 1,
        "task_id": task_id,
        "draft_id": draft_id,
        "git_ref": ref,
        "callback_url_kickoff": callback_url_kickoff,
        "callback_url_validate": callback_url_validate,
        "callback_url_apply_start": callback_url_apply_start,
        "callback_url_apply": callback_url_apply,
        "callback_url_apply": callback_url_apply,
        "callback_url_canceled": callback_url_canceled,
        "action_plan": action_plan,
        "options": options,
        "device_map": device_map,
    }
    forti_payload_str = json.dumps(forti_payload, ensure_ascii=False)

    url = f"{base}/api/v4/projects/{project_id}/pipeline"
    headers = {"PRIVATE-TOKEN": token}

    # ✅ 單一路徑：只投這兩個變數
    variables = [
        {"key": "FORTI_PAYLOAD", "value": forti_payload_str},
        {"key": "FORTI_CALLBACK_TOKEN", "value": callback_token},
        {"key": "FORTI_DEVICE_MAP", "value": json.dumps(device_map, ensure_ascii=False)},
    ]
    data = {"ref": ref, "variables": variables}

    r = requests.post(url, headers=headers, json=data, timeout=30, verify=verify_ssl)
    if r.status_code // 100 != 2:
        _raise_with_gitlab_message(r)
    pj = r.json() if r.content else {}

    # ★ 先取得 pipeline_id，再處理 web_url
    pipeline_id = pj.get("id") or (pj.get("pipeline") or {}).get("id")
    commit_sha  = pj.get("sha")
    pipeline_web_url = pj.get("web_url")
    if not pipeline_web_url and pipeline_id:
        # GitLab 的人類可見網址
        pipeline_web_url = f"{base}/-/pipelines/{pipeline_id}"

    # 取 validate / apply job 資訊（如有）
    job_id = None
    job_url = None
    if pipeline_id:
        try:
            jobs = _get_jobs(base, int(project_id), int(pipeline_id), token, verify=verify_ssl)
            chosen = _pick_job_by_name(jobs, "validate") or _pick_job_by_name(jobs, "apply")
            if chosen:
                job_id  = chosen.get("id")
                job_url = chosen.get("web_url")
        except Exception:
            pass

    return {
        "gitlab_pipeline_id": pipeline_id,
        "gitlab_pipeline_url": pipeline_web_url,
        "gitlab_job_id": job_id,
        "gitlab_job_url": job_url,
        "git_commit_sha": commit_sha,
    }

def cancel_pipeline(*, task_id: int, draft_id: int) -> bool:
    info = _load_gitlab_info()
    base = (info.get("url") or "").rstrip("/")
    token = info.get("token")
    project_id = info.get("project_id")
    verify_ssl = _bool(info.get("verify_ssl"))
    if not base or not token or not project_id:
        return False

    row = _get_task_row_by(task_id=task_id, draft_id=draft_id)
    if not row:
        return False

    headers = {"PRIVATE-TOKEN": token}
    ok = False

    job_id = row.get("gitlab_job_id")
    if job_id:
        u = f"{base}/api/v4/projects/{project_id}/jobs/{job_id}/cancel"
        try:
            r = requests.post(u, headers=headers, timeout=20, verify=verify_ssl)
            if 200 <= r.status_code < 300:
                ok = True or ok
            elif r.status_code in (404, 409, 422):
                ok = True or ok
        except Exception:
            pass

    pipeline_id = row.get("gitlab_pipeline_id")
    if pipeline_id:
        u = f"{base}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/cancel"
        try:
            r = requests.post(u, headers=headers, timeout=20, verify=verify_ssl)
            if 200 <= r.status_code < 300:
                ok = True or ok
        except Exception:
            pass

    return bool(ok)

def play_apply_job_for_pipeline(*, task_id: int, draft_id: int) -> Optional[Dict[str, Any]]:
    info = _load_gitlab_info()
    base = (info.get("url") or "").rstrip("/")
    token = info.get("token")
    project_id = info.get("project_id")
    verify_ssl = _bool(info.get("verify_ssl"))

    if not base or not token or not project_id:
        raise RuntimeError("gitlab_info not configured (url/token/project_id missing)")

    row = _get_task_row_by(task_id=task_id, draft_id=draft_id)
    if not row or not row.get("gitlab_pipeline_id"):
        return None

    pipeline_id = int(row["gitlab_pipeline_id"])
    headers = {"PRIVATE-TOKEN": token}

    jobs = _get_jobs(base, int(project_id), pipeline_id, token, verify_ssl)
    apply_job = _pick_job_by_name(jobs, "apply")
    if not apply_job:
        return None

    status = (apply_job.get("status") or "").lower()
    if status not in ("manual", "waiting_for_resource"):
        return {
            "job_id": apply_job.get("id"),
            "job_url": apply_job.get("web_url"),
            "status": status,
        }

    play_url = f"{base}/api/v4/projects/{project_id}/jobs/{apply_job['id']}/play"
    r = requests.post(play_url, headers=headers, timeout=20, verify=verify_ssl)
    if r.status_code // 100 != 2:
        _raise_with_gitlab_message(r)
    played = r.json() or {}
    return {
        "job_id": played.get("id") or apply_job.get("id"),
        "job_url": played.get("web_url") or apply_job.get("web_url"),
        "status": (played.get("status") or status),
    }

