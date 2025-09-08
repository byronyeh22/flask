import time
import threading
import json
from datetime import datetime

from app.mysql.db import get_db_connection

# ---------- GitLab ----------
from app.vsphere.vm.db.update_gitlab_pipeline_details import update_gitlab_pipeline_details
from app.vsphere.vm.gitlab_api.get_pipeline_status_from_gitlab import get_pipeline_status_from_gitlab

# ---------- Jira ----------
from app.vsphere.vm.db.get_jira_tickets_and_stats import get_jira_ticket_by_workflow_id
from app.vsphere.vm.db.insert_jira_info_to_db import insert_jira_info_to_db
from app.vsphere.vm.jira_api.create_jira_ticket import create_jira_ticket
from app.vsphere.vm.jira_api.get_jira_issue_detail import get_jira_issue_detail
from app.vsphere.vm.jira_api.issue_updates import jira_add_comment,jira_transition_issue

# Update workflow status
from app.vsphere.vm.db.workflow_manager import update_request_status
from mysql.connector import Error as MySQLError

PIPELINE_MANUAL_STATUS = "manual"

# ---------- Save failed_message as JSON (overwrite same source) ----------
def set_failed_message(db_conn, workflow_id: int, source: str, message: str) -> None:
    """
    Save error message into workflow_runs.failed_message as JSON.
    Example: {"JIRA": "[ts] Jira ticket not created or not found", "GITLAB": "[ts] Pipeline 123 failed"}
    Same source will be overwritten if retried.
    """
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT failed_message FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
        old_json = {}
        if row and row.get("failed_message"):
            try:
                old_json = json.loads(row["failed_message"])
            except Exception:
                old_json = {}

        old_json[source] = f"[{ts}] {message}"

        cur2 = db_conn.cursor()
        cur2.execute(
            """
            UPDATE workflow_runs
               SET failed_message = %s,
                   updated_at     = NOW()
             WHERE workflow_id   = %s
            """,
            (json.dumps(old_json), workflow_id),
        )
        db_conn.commit()
        cur2.close()
    finally:
        cur.close()


def ensure_jira_after_success(db_conn, workflow_id: int) -> None:
    """
    若該 workflow 還沒有 Jira，就在 pipeline success 後開單（idempotent）。
    建單後：
      1) 先加一則 comment（若失敗 -> 記 failed_message 並 return）
      2) 再 transition 到 Done（若失敗 -> 記 failed_message 並 return）
      3) 成功後重新抓 issue 詳細，再 insert 到 DB（最終狀態才入庫）
    """
    try:
        # 0) 已有 ticket 就跳過
        existing = get_jira_ticket_by_workflow_id(workflow_id)
        if existing and existing.get("ticket_id"):
            return

        # 1) 取 payload（從 workflow_runs.request_payload）
        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT request_payload FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        row = cur.fetchone()
        cur.close()

        if not row or not row.get("request_payload"):
            set_failed_message(db_conn, workflow_id, "JIRA", "No request_payload to create Jira.")
            return

        try:
            payload = json.loads(row["request_payload"])
        except Exception as je:
            set_failed_message(db_conn, workflow_id, "JIRA", f"Invalid request_payload JSON: {je}")
            return

        form_data = payload.get("new_config", payload)

        # 2) 建單
        jira_key = create_jira_ticket(form_data)

        # 3) 立刻加 comment（失敗就終止，不寫 DB）
        try:
            jira_add_comment(jira_key, "Auto-created by pipeline SUCCESS.")
        except Exception as e:
            set_failed_message(db_conn, workflow_id, "JIRA", f"Add comment failed: {e}")
            return

        # 4) 嘗試 transition 到 Done（名稱視你的 workflow 而定；大小寫不敏感）
        #    若你的流程需要 resolution，請改用 issue_updates 版本帶 resolution 或在專案流程上放寬
        try:
            jira_transition_issue(jira_key, "Done")
        except Exception as e:
            set_failed_message(db_conn, workflow_id, "JIRA", f"Transition to Done failed: {e}")
            return

        # 5) 重新抓詳細（此時狀態已是最終），再入庫
        try:
            ticket_data = get_jira_issue_detail(jira_key)
            insert_jira_info_to_db(workflow_id, ticket_data)
        except Exception as e:
            set_failed_message(db_conn, workflow_id, "JIRA", f"Insert ticket to DB failed: {e}")
            return

    except Exception as e:
        set_failed_message(db_conn, workflow_id, "JIRA", f"Create ticket failed: {e}")
# ---------- Check Jira ticket existence ----------
# def jira_exists_for_workflow(db_conn, workflow_id: int) -> bool:
#     """
#     Return True if jira_tickets has this workflow_id 且有 ticket_id。
#     否則 return False 並記錄 failed_message。
#     """
#     cur = db_conn.cursor(dictionary=True)
#     try:
#         cur.execute(
#             """
#             SELECT ticket_id
#               FROM jira_tickets
#              WHERE workflow_id = %s
#              ORDER BY id DESC
#              LIMIT 1
#             """,
#             (workflow_id,),
#         )
#         row = cur.fetchone()

#         if not row or not row.get("ticket_id"):
#             set_failed_message(db_conn, workflow_id, "JIRA", "Jira ticket not created or not found")
#             return False

#         return True
#     finally:
#         cur.close()


# ---------- Check if GitLab pipeline is manual ----------
def is_pipeline_manual_for_workflow(db_conn, workflow_id: int) -> bool:
    """
    Get the latest pipeline for a workflow_id, return True if status is 'manual'.
    """
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT status, pipeline_id
              FROM gitlab_pipelines
             WHERE workflow_id = %s
             ORDER BY started_at DESC, pipeline_id DESC
             LIMIT 1
            """,
            (workflow_id,),
        )
        row = cur.fetchone()
        if not row:
            set_failed_message(db_conn, workflow_id, "GITLAB", "No pipeline found for this workflow")
            return False
        return (row.get("status") or "").strip().lower() == PIPELINE_MANUAL_STATUS
    finally:
        cur.close()

# ---------- Advance to PENDING_APPROVAL when GitLab is manual ----------
def maybe_advance_to_pending_approval(db_conn, workflow_id: int) -> bool:
    """
    New rule:
      - If the latest GitLab pipeline status == 'manual' -> set workflow_runs.status = 'PENDING_APPROVAL'
      - Skip if current workflow status is 'RETURNED' (user is re-editing)
      - No longer requires a Jira ticket to exist up front
    """
    try:
        # 1) 保護 RETURNED：避免被誤推進
        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        wf_row = cur.fetchone()
        cur.close()

        if wf_row and (wf_row.get("status") or "").upper() == "RETURNED":
            return False

        # 2) 只看 GitLab pipeline 是否為 manual
        manual_ok = is_pipeline_manual_for_workflow(db_conn, workflow_id)
        if not manual_ok:
            return False

        # 3) 推進到 PENDING_APPROVAL
        update_request_status(workflow_id, "PENDING_APPROVAL")
        return True

    except MySQLError as e:
        set_failed_message(db_conn, workflow_id, "WORKFLOW", f"Failed to advance status: {e}")
        return False


# ---------- Monitor pipelines ----------
def monitor_pipelines(app):
    with app.app_context():
        while True:
            print("\n🚀 Start monitoring GitLab pipelines...")

            db_conn = get_db_connection()
            try:
                # 1) 列出所有尚未終態的 workflows（7 天內）
                cur = db_conn.cursor(dictionary=True)
                cur.execute(
                    """
                    SELECT workflow_id, status
                      FROM workflow_runs
                     WHERE status NOT IN ('SUCCESS','FAILED')
                       AND created_at >= NOW() - INTERVAL 7 DAY
                     ORDER BY workflow_id DESC
                    """
                )
                workflows_todo = cur.fetchall()
                cur.close()

                for wr in workflows_todo:
                    workflow_id = wr["workflow_id"]

                    # 2) 抓該 workflow 最新一筆 pipeline（單筆、可讀性高）
                    cur_p = db_conn.cursor(dictionary=True)
                    cur_p.execute(
                        """
                        SELECT pipeline_id, workflow_id, status, started_at
                          FROM gitlab_pipelines
                         WHERE workflow_id = %s
                         ORDER BY started_at DESC, pipeline_id DESC
                         LIMIT 1
                        """,
                        (workflow_id,),
                    )
                    latest = cur_p.fetchone()
                    cur_p.close()

                    if not latest:
                        # 沒 pipeline（可能尚未觸發）— 看需求要不要記錄訊息；這裡略過
                        continue

                    pipeline_id = latest["pipeline_id"]
                    db_status   = (latest.get("status") or "").lower()

                    # 3) 如果 DB 狀態不是終態 → 打 GitLab API 更新 DB，再依結果推進
                    if db_status not in ("success", "failed", "canceled"):
                        print(f"🔍 [poll] wf={workflow_id} pid={pipeline_id} DB={db_status} → query GitLab")

                        gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)
                        if gitlab_result.get("success"):
                            update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)
                            fresh = (gitlab_result.get("status") or "").lower()
                            print(f"⚙️  [poll] wf={workflow_id} pid={pipeline_id} GitLab={fresh}")

                            # 防護：RETURNED 直接跳過任何覆蓋
                            cur_now = db_conn.cursor(dictionary=True)
                            cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                            wr_now = cur_now.fetchone()
                            cur_now.close()
                            if wr_now and (wr_now.get("status") or "").upper() == "RETURNED":
                                continue

                            if fresh == "manual":
                                maybe_advance_to_pending_approval(db_conn, workflow_id)

                            elif fresh == "success":
                                update_request_status(workflow_id, "SUCCESS")
                                ensure_jira_after_success(db_conn, workflow_id)

                            elif fresh == "failed":
                                update_request_status(workflow_id, "FAILED")
                                set_failed_message(
                                    db_conn, workflow_id, "GITLAB",
                                    f"Pipeline {pipeline_id} status is failed"
                                )
                            # 其它（created/pending/running）不動
                        else:
                            set_failed_message(
                                db_conn, workflow_id, "GITLAB_API",
                                f"Pipeline query failed: {gitlab_result.get('error')}",
                            )

                    else:
                        # 4) DB 已是終態 → 直接用 DB 狀態補同步（避免漏單）
                        print(f"🧹 [finalize] wf={workflow_id} pid={pipeline_id} DB={db_status}")
                        # 防護：RETURNED 不覆蓋
                        cur_now = db_conn.cursor(dictionary=True)
                        cur_now.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
                        wr_now = cur_now.fetchone()
                        cur_now.close()
                        if wr_now and (wr_now.get("status") or "").upper() == "RETURNED":
                            continue

                        if db_status == "success":
                            update_request_status(workflow_id, "SUCCESS")
                            ensure_jira_after_success(db_conn, workflow_id)
                        elif db_status == "failed":
                            update_request_status(workflow_id, "FAILED")
                            set_failed_message(db_conn, workflow_id, "GITLAB",
                                               f"Pipeline {pipeline_id} status is failed")
                        elif db_status == "canceled":
                            # 僅記錄，不改 workflow 狀態
                            set_failed_message(db_conn, workflow_id, "GITLAB",
                                               f"Pipeline {pipeline_id} status is canceled")

            except Exception as e:
                print(f"❌ Pipeline monitoring error: {e}")
            finally:
                db_conn.close()

            # 輪詢間隔（開發可調短；正式可 30~60s）
            time.sleep(5)


# ---------- Scan IN_PROGRESS workflows ----------
def monitor_workflows(app):
    with app.app_context():
        while True:
            print("\n🧭 Start scanning workflows for status advancement...")

            db_conn = get_db_connection()
            cur = db_conn.cursor(dictionary=True)
            try:
                cur.execute(
                    """
                    SELECT workflow_id
                      FROM workflow_runs
                     WHERE status = 'IN_PROGRESS'
                       AND created_at >= NOW() - INTERVAL 7 DAY
                    """
                )
                items = cur.fetchall()

                for row in items:
                    wf_id = row["workflow_id"]
                    maybe_advance_to_pending_approval(db_conn, wf_id)

            except Exception as e:
                print(f"❌ Workflow scanning error: {e}")

            finally:
                cur.close()
                db_conn.close()

            time.sleep(60)


def start_monitor_thread(app):
    t1 = threading.Thread(target=monitor_pipelines, args=(app,), daemon=True)
    t1.start()
    print("✅ Pipeline Monitor Thread started")

    t2 = threading.Thread(target=monitor_workflows, args=(app,), daemon=True)
    t2.start()
    print("✅ Workflow Monitor Thread started")