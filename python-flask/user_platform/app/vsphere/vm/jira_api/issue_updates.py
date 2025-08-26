# app/vsphere/vm/jira_api/issue_updates.py
import requests
from flask import current_app

def _jira_base_and_auth():
    """
    與 get_jira_issue_detail 相同的取得方式：
    - jira_base 來自 config['JIRA_BASE_URL']
    - auth 使用 (JIRA_USER, JIRA_API_TOKEN)
    """
    jira_base = current_app.config['JIRA_BASE_URL'].rstrip('/')
    auth = (
        current_app.config['JIRA_USER'],
        current_app.config['JIRA_API_TOKEN']
    )
    return jira_base, auth

def jira_add_comment(ticket_id: str, body: str) -> None:
    """
    在 Jira issue 加上一則 comment。
    連線與錯誤處理風格與 get_jira_issue_detail 一致。
    """
    jira_base, auth = _jira_base_and_auth()
    url = f"{jira_base}/rest/api/2/issue/{ticket_id}/comment"

    try:
        response = requests.post(
            url,
            json={"body": body},
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as err:
        # 與 get_jira_issue_detail 相同：列印錯誤並往外拋
        print(f"Failed to add Jira comment: {err}")
        raise

def jira_transition_issue(ticket_id: str, transition_name: str) -> None:
    """
    將 issue 轉換到指定 transition 名稱（忽略大小寫比對）。
    - 先 GET transitions
    - 找到對應名稱的 id
    - 再 POST transitions 執行轉換
    """
    jira_base, auth = _jira_base_and_auth()

    try:
        # 1) 取得所有可用 transition
        list_url = f"{jira_base}/rest/api/2/issue/{ticket_id}/transitions"
        list_resp = requests.get(
            list_url,
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        list_resp.raise_for_status()
        data = list_resp.json() or {}
        transitions = data.get("transitions", [])

        target = None
        tname_lower = transition_name.strip().lower()
        for t in transitions:
            if t.get("name", "").strip().lower() == tname_lower:
                target = t
                break

        if not target:
            raise RuntimeError(f"Transition '{transition_name}' not available for {ticket_id}")

        # 2) 執行 transition
        do_url = f"{jira_base}/rest/api/2/issue/{ticket_id}/transitions"
        do_resp = requests.post(
            do_url,
            json={"transition": {"id": target["id"]}},
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        do_resp.raise_for_status()

    except requests.exceptions.RequestException as err:
        print(f"Failed to transition Jira issue: {err}")
        raise

def jira_return_issue(ticket_id: str, reason: str, transition_name: str | None = None) -> None:
    """
    通用「退件」操作：
    1) 加上一則 comment（含退件原因）
    2) 若有給 transition_name，則執行狀態轉換
    """
    # 先加 comment（採用與 get_jira_issue_detail 相同的錯誤處理）
    jira_add_comment(ticket_id, f"Request returned. Reason: {reason}")

    # 再做 transition（可選）
    if transition_name:
        jira_transition_issue(ticket_id, transition_name)