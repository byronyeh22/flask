import requests
from flask import current_app
from datetime import datetime, timezone, timedelta

def _to_tw_datetime(iso_str: str):
    """
    Jira Cloud 的 fields.created 會是 UTC（例：2025-09-08T03:08:52.123+0000 或沒有毫秒）。
    轉成台北時間(+08:00)後，以 'YYYY-MM-DD HH:MM:SS' 字串回傳。
    解析失敗就原樣回傳（不擋流程）。
    """
    if not iso_str:
        return None
    try:
        # 常見：含毫秒
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            # 次常見：不含毫秒
            dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            return iso_str

    tw = dt.astimezone(timezone(timedelta(hours=8)))
    return tw.strftime("%Y-%m-%d %H:%M:%S")

def get_jira_issue_detail(ticket_id, fields=None):
    jira_base = current_app.config['JIRA_BASE_URL']
    auth = (
        current_app.config['JIRA_USER'],
        current_app.config['JIRA_API_TOKEN']
    )

    fields_param = f"?fields={','.join(fields)}" if fields else ""

    try:
        resp = requests.get(
            f"{jira_base}/rest/api/2/issue/{ticket_id}{fields_param}",
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        resp.raise_for_status()

        data = resp.json()
        fields_data = data.get("fields", {})

        return {
            "ticket_id": data.get("key"),
            "project_key": fields_data.get("project", {}).get("key", ""),
            "summary": fields_data.get("summary", ""),
            "description": fields_data.get("description", ""),
            "status": fields_data.get("status", {}).get("name", ""),
            "url": f"{jira_base}/browse/{ticket_id}",
            # ⭐ 這裡把 UTC 轉成 +08:00 的字串
            "created_at": _to_tw_datetime(fields_data.get("created")),
        }

    except requests.exceptions.RequestException as err:
        print(f"Failed to get Jira issue detail: {err}")
        raise

# 測試範例（與你原本相同）
if __name__ == "__main__":
    ticket_id = "SJT-86"
    fields = ["project", "status", "summary", "description"]
    result = get_jira_issue_detail(ticket_id, fields=fields)
    if result:
        print("=== Jira Issue Detail ===")
        print("Ticket ID:", result.get("ticket_id"))
        print("Project Key:", result.get("project_key"))
        print("Summary:", result.get("summary"))
        print("Status:", result.get("status"))
        print("Description:", result.get("description"))
        print("URL:", result.get("url"))
        print("Created_at:", result.get("created_at"))