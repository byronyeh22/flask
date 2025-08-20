import requests

def get_jira_issue_detail(ticket_id, fields=None):
    jira_base = "https://sanbox888twuat.atlassian.net"
    auth = (
        "srv.sra@sanbox888.tw",
        "ATATT3xFfGF0R9x--AYy2vPdYkG25_w52yHTrGG4wGfBwMbnsyxDMoFmSPL54MtfecWNeoLQR2_0hY73MhBh0m1njA057j8b-9qdFX4TPVlngRu9mkYq1p9TVdXei1_a0FcSt_GgaK2Ae7f8fU8v-PiDSfljnMr63Ce1TuiFApMSdxeFih-_WUE=346474B9"
    )

    # 如果有指定 fields，會把欄位用逗號連接起來並加到 URL 查詢字串
    fields_param = f"?fields={','.join(fields)}" if fields else ""

    try:
        # 送出 GET 請求到 Jira API，查詢指定的 issue (ticket)
        response = requests.get(
            f"{jira_base}/rest/api/2/issue/{ticket_id}{fields_param}",
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        # 如果 HTTP 狀態碼不是 2xx，就會拋出錯誤
        response.raise_for_status()

         # 解析回傳的 JSON 資料
        data = response.json()

         # 從回傳資料中取出 fields 這個區塊（Jira issue 的詳細欄位）
        fields_data = data.get("fields", {})

         # 依需求把欄位組成一個新的 dict 回傳
         # 字串前加上 f，並在字串內用 {} 括住變數或表達式，Python 會自動把它替換成對應的值
        return {
            "ticket_id": data.get("key"),
            "project_key": fields_data.get("project", {}).get("key", ""),
            "summary": fields_data.get("summary", ""),
            "description": fields_data.get("description", ""),
            "status": fields_data.get("status", {}).get("name", ""),
            "url": f"{jira_base}/browse/{ticket_id}"
        }

    # 如果請求出錯，印出錯誤並把例外拋出
    except requests.exceptions.RequestException as err:
        print(f"Failed to get Jira issue detail: {err}")
        raise


# 測試範例
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