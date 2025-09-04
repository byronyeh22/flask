import time
import threading
import json
from datetime import datetime

from app.mysql.db import get_db_connection
from app.vsphere.vm.db.update_gitlab_pipeline_details import update_gitlab_pipeline_details
from app.vsphere.vm.gitlab_api.get_pipeline_status_from_gitlab import get_pipeline_status_from_gitlab

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


# ---------- Check Jira ticket existence ----------
def jira_exists_for_workflow(db_conn, workflow_id: int) -> bool:
    """
    Return True if jira_tickets has this workflow_id 且有 ticket_id。
    否則 return False 並記錄 failed_message。
    """
    cur = db_conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT ticket_id
              FROM jira_tickets
             WHERE workflow_id = %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (workflow_id,),
        )
        row = cur.fetchone()

        if not row or not row.get("ticket_id"):
            set_failed_message(db_conn, workflow_id, "JIRA", "Jira ticket not created or not found")
            return False

        return True
    finally:
        cur.close()


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


# ---------- Advance to PENDING_APPROVAL if Jira exists and GitLab is manual ----------
def maybe_advance_to_pending_approval(db_conn, workflow_id: int) -> bool:
    """
    Conditions:
      1) Jira ticket exists
      2) Latest pipeline status == 'manual'
      3) workflow_runs.status != 'RETURNED' (避免被退回後又誤判回 pending_approval)
    """
    try:
        # 防呆：如果已經是 RETURNED，就不要再推進
        cur = db_conn.cursor(dictionary=True)
        cur.execute("SELECT status FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))
        wf_row = cur.fetchone()
        cur.close()
        if wf_row and (wf_row.get("status") or "").upper() == "RETURNED":
            return False

        jira_ok = jira_exists_for_workflow(db_conn, workflow_id)
        if not jira_ok:
            return False

        manual_ok = is_pipeline_manual_for_workflow(db_conn, workflow_id)
        if not manual_ok:
            return False

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
            cursor = db_conn.cursor(dictionary=True)

            try:
                # ----------------------------
                # A. 進行中（非終態）的 pipelines：打 API 追狀態
                # ----------------------------
                cursor.execute(
                    """
                    SELECT pipeline_id, workflow_id, status, started_at
                    FROM gitlab_pipelines
                    WHERE workflow_id IS NOT NULL
                      AND COALESCE(status,'') NOT IN ('success','failed','canceled')
                      AND (started_at >= NOW() - INTERVAL 7 DAY OR started_at IS NULL)
                    ORDER BY updated_at DESC
                    LIMIT 100
                    """
                )
                pipelines_active = cursor.fetchall()

                # ----------------------------
                # B. 補同步：DB 已終態，但 workflow 還沒終態（不一定打 API）
                # ----------------------------
                cursor.execute(
                    """
                    SELECT gp.pipeline_id, gp.workflow_id, gp.status, gp.started_at
                    FROM gitlab_pipelines gp
                    JOIN workflow_runs wr ON wr.workflow_id = gp.workflow_id
                    WHERE gp.workflow_id IS NOT NULL
                      AND gp.status IN ('success','failed','canceled')
                      AND wr.status NOT IN ('SUCCESS','FAILED')
                      AND gp.updated_at >= NOW() - INTERVAL 7 DAY
                    ORDER BY gp.updated_at DESC
                    LIMIT 100
                    """
                )
                pipelines_finalize = cursor.fetchall()

                # ---------- A. 處理進行中 ----------
                for pipeline in pipelines_active:
                    pipeline_id = pipeline["pipeline_id"]
                    workflow_id = pipeline.get("workflow_id")
                    print(f"🔍 [active] Checking pipeline {pipeline_id} (wf={workflow_id}) DB={pipeline.get('status')}")

                    gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)

                    if gitlab_result["success"]:
                        # 先更新 gitlab_pipelines 明細
                        update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)

                        status = (gitlab_result.get("status") or "").lower()
                        print(f"⚙️  [active] pipeline {pipeline_id} status from GitLab: {status}")

                        if workflow_id:
                            try:
                                if status == "manual":
                                    # Jira OK + manual → PENDING_APPROVAL（沿用你既有規則）
                                    maybe_advance_to_pending_approval(db_conn, workflow_id)

                                elif status == "success":
                                    update_request_status(workflow_id, "SUCCESS")

                                elif status in ("failed", "canceled"):
                                    update_request_status(workflow_id, "FAILED")
                                    set_failed_message(
                                        db_conn, workflow_id, "GITLAB",
                                        f"Pipeline {pipeline_id} status is {status}"
                                    )
                                # 其它狀態（created/pending/running）不動
                                # running → DEPLOYING 交給 Approve route
                            except MySQLError as e:
                                set_failed_message(db_conn, workflow_id, "WORKFLOW",
                                                   f"Failed to sync status (mysql): {e}")
                            except Exception as e:
                                set_failed_message(db_conn, workflow_id, "WORKFLOW",
                                                   f"Failed to sync status (other): {e}")
                    else:
                        # API 失敗：先記錄錯誤，active 這批下一輪會再查
                        if workflow_id:
                            set_failed_message(
                                db_conn,
                                workflow_id,
                                "GITLAB_API",
                                f"Pipeline query failed: {gitlab_result.get('error')}",
                            )

                # ---------- B. 補同步（DB終態但 workflow 還沒終態） ----------
                for pipeline in pipelines_finalize:
                    pipeline_id = pipeline["pipeline_id"]
                    workflow_id = pipeline.get("workflow_id")
                    db_status   = (pipeline.get("status") or "").lower()
                    print(f"🧹 [finalize] pipeline {pipeline_id} DB={db_status} wf={workflow_id} → finalize workflow")

                    if not workflow_id:
                        continue

                    try:
                        if db_status == "success":
                            update_request_status(workflow_id, "SUCCESS")
                        else:
                            update_request_status(workflow_id, "FAILED")
                            set_failed_message(
                                db_conn, workflow_id, "GITLAB",
                                f"Pipeline {pipeline_id} status is {db_status}"
                            )
                    except MySQLError as e:
                        set_failed_message(db_conn, workflow_id, "WORKFLOW",
                                           f"Failed to finalize from DB state (mysql): {e}")
                    except Exception as e:
                        set_failed_message(db_conn, workflow_id, "WORKFLOW",
                                           f"Failed to finalize from DB state (other): {e}")

            except Exception as e:
                print(f"❌ Pipeline monitoring error: {e}")

            finally:
                cursor.close()
                db_conn.close()

            # 輪詢間隔（開發可調短；正式可 30~60s）
            time.sleep(60)


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