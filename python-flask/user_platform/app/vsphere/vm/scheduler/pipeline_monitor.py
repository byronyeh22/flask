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


# ---------- Utilities ----------
def _normalize_status(s: str) -> str:
    """
    Normalize status string:
      - Treat hyphen as whitespace
      - Collapse multiple whitespaces
      - Lowercase
    Examples:
      "To Do" -> "to do"
      "To-Do" -> "to do"
      "To  Do" -> "to do"
    """
    if not s:
        return ""
    return " ".join(s.replace("-", " ").split()).lower()


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


# ---------- Check Jira status ----------
def monitor_jira_for_workflow(db_conn, workflow_id: int) -> bool:
    """
    Return True if jira_tickets has this workflow_id and status is 'To Do' (normalized).
    Otherwise return False, and record the reason in workflow_runs.failed_message.
    """
    cur = db_conn.cursor(dictionary=True)
    try:
        # Use id DESC to avoid NULL created_at picking an older row
        cur.execute(
            """
            SELECT status
              FROM jira_tickets
             WHERE workflow_id = %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (workflow_id,),
        )
        row = cur.fetchone()

        if not row:
            set_failed_message(db_conn, workflow_id, "JIRA", "Jira ticket not created or not found")
            return False

        raw = (row.get("status") or "")
        norm = _normalize_status(raw)
        if norm != "to do":
            set_failed_message(db_conn, workflow_id, "JIRA", f"Unexpected Jira status: {raw}")
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


# ---------- Advance to PENDING_APPROVAL if both Jira and GitLab conditions met ----------
def maybe_advance_to_pending_approval(db_conn, workflow_id: int) -> bool:
    """
    Conditions:
      1) Jira status normalized == 'to do'
      2) Latest pipeline status == 'manual'
    Only when both satisfied, set workflow_runs.status -> PENDING_APPROVAL.
    """
    try:
        jira_ok = monitor_jira_for_workflow(db_conn, workflow_id)
        if not jira_ok:
            return False

        manual_ok = is_pipeline_manual_for_workflow(db_conn, workflow_id)
        if not manual_ok:
            return False

        update_request_status(db_conn, workflow_id, "PENDING_APPROVAL")
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
                cursor.execute(
                    """
                    SELECT pipeline_id, workflow_id
                      FROM gitlab_pipelines
                     WHERE pipeline_id IS NOT NULL
                       AND status NOT IN ('success', 'failed', 'canceled')
                       AND started_at >= NOW() - INTERVAL 1 DAY
                    """
                )
                pipelines = cursor.fetchall()

                for pipeline in pipelines:
                    pipeline_id = pipeline["pipeline_id"]
                    workflow_id = pipeline.get("workflow_id")
                    print(f"🔍 Checking pipeline {pipeline_id}")

                    gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)

                    if gitlab_result["success"]:
                        # Update gitlab_pipelines with latest detail
                        update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)

                        # If now failed/canceled, record message
                        status = (gitlab_result.get("status") or "").lower()
                        if status in ("failed", "canceled"):
                            set_failed_message(
                                db_conn, workflow_id, "GITLAB", f"Pipeline {pipeline_id} status is {status}"
                            )

                        # Try advancing workflow status
                        if workflow_id:
                            maybe_advance_to_pending_approval(db_conn, workflow_id)

                    else:
                        # Record API error (avoid "None" wording)
                        if workflow_id:
                            set_failed_message(
                                db_conn,
                                workflow_id,
                                "GITLAB_API",
                                f"Pipeline query failed: {gitlab_result.get('error')}",
                            )

            except Exception as e:
                print(f"❌ Pipeline monitoring error: {e}")

            finally:
                cursor.close()
                db_conn.close()

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