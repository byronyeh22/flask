from .. import vm_bp
from flask import render_template
import json
import logging

# --- DB 函式 ---
from ..db.vsphere_connections_manager import get_active_vsphere_connections
from ..db.get_jira_tickets_and_stats import get_jira_tickets_and_stats
from ..db.get_gitlab_pipeline_detail_and_stats import get_gitlab_pipeline_detail_and_stats

# --- API 函式 ---
from ..jira_api.create_jira_ticket import _generate_create_summary, _generate_update_summary, _generate_delete_summary

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

        # 3) 統一解析每個 workflow 的 payload 資訊 - 支援 create/update/delete
        workflow_details_map = {}
        for workflow in all_workflows:
            workflow_id = workflow["workflow_id"]

            try:
                request_payload = json.loads(workflow.get("request_payload") or "{}")

                # 統一處理不同 action 類型
                action_type = request_payload.get("action_type", "create")

                if action_type == "create":
                    # Create: 直接從 payload 根層取得資料
                    form_data = request_payload
                    env = form_data.get("environment", "")
                    vm_prefix = form_data.get("vm_name_prefix", "")
                    os_type = form_data.get("os_type", form_data.get("vm_os_type", ""))

                    # 使用與 Jira 相同的 summary 生成函數
                    try:
                        summary_text = _generate_create_summary(form_data) if form_data else "Create VM"
                    except Exception:
                        summary_text = f"[VM Provisioning] {env} - Create {vm_prefix}" if env and vm_prefix else "Create VM"

                elif action_type == "update" and 'new_config' in request_payload:
                    # Update: 從 new_config 中取得資料
                    form_data = request_payload.get("new_config", {})
                    env = form_data.get("environment", "")
                    vm_prefix = form_data.get("vm_name_prefix", "")
                    os_type = form_data.get("os_type", "")

                    # 使用與 Jira 相同的 summary 生成函數
                    try:
                        summary_text = _generate_update_summary(request_payload) if request_payload else "Update VM"
                    except Exception:
                        summary_text = f"[VM Provisioning] {env} - Update {vm_prefix}" if env and vm_prefix else "Update VM"

                elif action_type == "delete" and 'delete_config' in request_payload:
                    # Delete: 從 delete_config 中取得資料
                    form_data = request_payload.get("original_config", {})
                    env = form_data.get("environment", "")
                    vm_prefix = form_data.get("vm_name_prefix", "")
                    os_type = form_data.get("os_type", "")

                    # 使用與 Jira 相同的 summary 生成函數
                    try:
                        summary_text = _generate_delete_summary(request_payload) if request_payload else "Delete VM"
                    except Exception:
                        summary_text = f"[VM Provisioning] {env} - Delete {vm_prefix}" if env and vm_prefix else "Delete VM"

                else:
                    # 未知類型：使用預設處理
                    form_data = request_payload
                    env = form_data.get("environment", "")
                    vm_prefix = form_data.get("vm_name_prefix", "")
                    os_type = form_data.get("os_type", form_data.get("vm_os_type", ""))
                    summary_text = f"[VM Provisioning] {env} - {action_type.title()} {vm_prefix}" if env and vm_prefix else f"{action_type.title()} VM"

                # 解析共同欄位
                workflow_details_map[workflow_id] = {
                    "environment": env,
                    "vm_name_prefix": vm_prefix,
                    "os_type": os_type,
                    "action_type": action_type,
                    "summary": summary_text,
                    "resource": form_data.get("resource", "vm"),
                }

            except Exception as e:
                logging.error(f"Error parsing workflow {workflow_id}: {e}")
                workflow_details_map[workflow_id] = {
                    "environment": "",
                    "vm_name_prefix": "",
                    "os_type": "",
                    "action_type": "",
                    "summary": "-",
                    "resource": "vm",
                }

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

            # 使用統一解析出的 summary
            workflow_details = workflow_details_map.get(workflow_id, {})
            summary_text = workflow_details.get("summary", "-")

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
                workflow_details = workflow_details_map.get(workflow_id, {})

                pipeline_rows.insert(0, {
                    "workflow_id": workflow_id,
                    "pipeline_id": None,
                    "status": workflow.get("status"),             # 使用實際 workflow 狀態
                    "created_at": to_iso8601(workflow.get("created_at")),
                    "finished_at": None,
                    "duration": None,
                    "created_by": workflow.get("created_by"),
                    # 添加從 payload 統一解析的欄位
                    "environment": workflow_details.get("environment", ""),
                    "vm_name_prefix": workflow_details.get("vm_name_prefix", ""),
                    "os_type": workflow_details.get("os_type", ""),
                    "action_type": workflow_details.get("action_type", ""),
                    "resource": workflow_details.get("resource", "vm"),
                })
                pipeline_existing_workflow_ids.add(workflow_id)

        # E) 用 workflow 的狀態/建立者覆蓋 pipeline_rows 的顯示欄位（保持欄位一致）
        for row in pipeline_rows:
            workflow_id = row.get("workflow_id")
            if workflow_id in workflow_status_map:
                row["status"] = workflow_status_map[workflow_id]
            row["created_by"] = workflow_created_by_map.get(workflow_id, row.get("created_by"))

            # 確保所有 pipeline_rows 都有完整的 workflow 詳細資訊
            if workflow_id in workflow_details_map:
                workflow_details = workflow_details_map[workflow_id]
                row["environment"] = workflow_details.get("environment", row.get("environment", ""))
                row["vm_name_prefix"] = workflow_details.get("vm_name_prefix", row.get("vm_name_prefix", ""))
                row["os_type"] = workflow_details.get("os_type", row.get("os_type", ""))
                row["action_type"] = workflow_details.get("action_type", row.get("action_type", ""))
                row["resource"] = workflow_details.get("resource", row.get("resource", "vm"))

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