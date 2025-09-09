from .. import vm_bp
from flask import render_template
import json
import logging

# --- DB 函式 ---
from ..db.vsphere_connections_manager import get_active_vsphere_connections
from ..db.get_jira_tickets_and_stats import get_jira_tickets_and_stats
from ..db.get_gitlab_pipeline_detail_and_stats import get_gitlab_pipeline_detail_and_stats

# --- API 函式 ---
from ..jira_api.create_jira_ticket import _generate_create_summary

@vm_bp.route("/vsphere/vm")
def vm_index():
    """
    Render the main VM management page.
    """
    environments = []
    try:
        active_connections = get_active_vsphere_connections()

        raw_envs = [
            (c.get("environment") or "").strip()
            for c in active_connections
            if c.get("environment")
        ]
        seen = set()
        for e in raw_envs:
            key = e.lower()
            if key not in seen:
                seen.add(key)
                environments.append(e)

    except Exception as e:
        logging.error(f"Error in vm_index: {e}")
        environments = []

    return render_template(
        "vm_index.html",
        environment=environments,
        datacenters=[],
        clusters=[],
        esxi_hosts=[],
        templates=[],
        networks=[],
        datastores=[],
        vm_name=[],
    )

@vm_bp.route("/vsphere/overview")
def overview_index():
    def to_iso8601(value):
        try:
            return value.isoformat()
        except Exception:
            return str(value) if value else "1970-01-01T00:00:00Z"

    try:
        # 1) 取回原始資料
        jira_ticket_rows = get_jira_tickets_and_stats() or []
        pipeline_rows = get_gitlab_pipeline_detail_and_stats() or []
        pipeline_rows = [
            {
                **dict(row),
                "created_at": to_iso8601(row.get("created_at") or row.get("started_at")),
            }
            for row in pipeline_rows
        ]

        # 2) 全部 workflow（包含 DRAFT）
        from ..db.workflow_manager import get_all_workflow_runs
        all_workflows = get_all_workflow_runs()
        workflow_status_map = {row["workflow_id"]: row["status"] for row in all_workflows}
        workflow_created_by_map = {row["workflow_id"]: row.get("created_by") for row in all_workflows}

        # A) 先把已有的 JIRA tickets 收成 map（key=workflow_id）
        jira_by_workflow_id = {
            row["workflow_id"]: dict(row) for row in jira_ticket_rows if row.get("workflow_id")
        }

        # B) 目前 pipeline_rows 中已經出現過的 workflow_id
        pipeline_existing_workflow_ids = {
            row.get("workflow_id") for row in pipeline_rows if row.get("workflow_id")
        }

        # C) 用 payload 補齊「尚未建立 Jira」的 workflow（不分狀態）
        for workflow in all_workflows:
            workflow_id = workflow["workflow_id"]

            # 已有 Jira 的 workflow 不需要 fallback
            if workflow_id in jira_by_workflow_id:
                continue

            # 產生 fallback 的 summary（沿用 Draft 的做法）
            try:
                request_payload = json.loads(workflow.get("request_payload") or "{}")
                summary_text = _generate_create_summary(request_payload) if request_payload else "-"
            except Exception:
                summary_text = "-"

            jira_by_workflow_id[workflow_id] = {
                "workflow_id": workflow_id,
                "ticket_id": None,                # 尚未建立 Jira → Ticket ID 留空
                "project_key": None,
                "summary": summary_text,          # 讓 Environment / Summary 能顯示
                "description": None,
                "status": None,
                "url": None,
                "created_at": to_iso8601(workflow.get("created_at")),
            }

        # D) 確保每個 workflow 都會出現在 pipeline_rows（不再只限 DRAFT）
        for workflow in all_workflows:
            workflow_id = workflow["workflow_id"]
            if workflow_id not in pipeline_existing_workflow_ids:
                pipeline_rows.insert(0, {
                    "workflow_id": workflow_id,
                    "pipeline_id": None,
                    "status": workflow.get("status"),             # 使用實際 workflow 狀態
                    "created_at": to_iso8601(workflow.get("created_at")),
                    "finished_at": None,
                    "duration": None,
                    "created_by": workflow.get("created_by"),
                })
                pipeline_existing_workflow_ids.add(workflow_id)

        # E) 用 workflow 的狀態/建立者覆蓋 pipeline_rows 的顯示欄位（保持欄位一致）
        for row in pipeline_rows:
            workflow_id = row.get("workflow_id")
            if workflow_id in workflow_status_map:
                row["status"] = workflow_status_map[workflow_id]
            row["created_by"] = workflow_created_by_map.get(workflow_id, row.get("created_by"))

        # 最後把 Jira map 轉回 list 給模板
        jira_ticket_rows = list(jira_by_workflow_id.values())

    except Exception as e:
        logging.error(f"overview_index error: {e}")
        jira_ticket_rows, pipeline_rows = [], []

    return render_template(
        "overview_index.html",
        jira_tickets=jira_ticket_rows,
        pipeline_data=pipeline_rows,
    )
