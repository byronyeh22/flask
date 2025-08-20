# app/vsphere/vm/db/insert_gitlab_pipeline_info_to_db.py
import json
from mysql.connector import Error
import datetime

def _sanitize(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value

def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]

def _to_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return default

def _extract_disks_snapshot_from_form(form_data):
    """
    從表單陣列收一份最小快照（若真的沒有 additional_disks 時才用）。
    不推算 scsi_controller/unit_number；如果前端沒帶這兩個，就留 None。
    """
    ids    = _as_list(form_data.get('update_disk_db_id[]') or form_data.get('disk_db_id[]'))
    sizes  = _as_list(form_data.get('update_vm_disk_size[]') or form_data.get('vm_disk_size[]') or form_data.get('create_vm_disk_size[]'))
    provs  = _as_list(form_data.get('update_vm_disk_provisioning[]') or form_data.get('vm_disk_provisioning[]') or form_data.get('create_vm_disk_provisioning[]'))
    thins  = _as_list(form_data.get('update_vm_disk_thin_provisioned[]') or form_data.get('vm_disk_thin_provisioned[]') or form_data.get('create_vm_disk_thin_provisioned[]'))
    eagers = _as_list(form_data.get('update_vm_disk_eagerly_scrub[]') or form_data.get('vm_disk_eagerly_scrub[]') or form_data.get('create_vm_disk_eagerly_scrub[]'))

    buses  = _as_list(form_data.get('update_scsi_controller[]') or form_data.get('scsi_controller[]'))
    units  = _as_list(form_data.get('update_unit_number[]') or form_data.get('unit_number[]'))
    uinos  = _as_list(form_data.get('update_ui_disk_number[]') or form_data.get('ui_disk_number[]'))

    n = max(len(sizes), len(provs), len(thins), len(eagers), len(ids), len(buses), len(units), len(uinos))
    disks = []
    for i in range(n):
        d = {
            "id": None,
            "scsi_controller": None,
            "unit_number": None,
            "ui_disk_number": None,
            "size": None,
            "disk_provisioning": None,
            "thin_provisioned": None,
            "eagerly_scrub": None,
        }
        if i < len(ids):
            try:
                d["id"] = int(ids[i]) if str(ids[i]).strip() != "" else None
            except Exception:
                d["id"] = None
        if i < len(buses):
            try:
                d["scsi_controller"] = int(buses[i])
            except Exception:
                d["scsi_controller"] = None
        if i < len(units):
            try:
                d["unit_number"] = int(units[i])
            except Exception:
                d["unit_number"] = None
        if i < len(uinos):
            try:
                d["ui_disk_number"] = int(uinos[i])
            except Exception:
                d["ui_disk_number"] = None
        if i < len(sizes):
            try:
                d["size"] = int(sizes[i])
            except Exception:
                d["size"] = None
        if i < len(provs):
            d["disk_provisioning"] = provs[i] or None
        if i < len(thins):
            d["thin_provisioned"] = _to_bool(thins[i], default=(d["disk_provisioning"] == "thin"))
        if i < len(eagers):
            d["eagerly_scrub"] = _to_bool(eagers[i], default=(d["disk_provisioning"] == "thick_eager"))

        # 篩掉完全空白
        if any([
            d["id"] is not None,
            d["scsi_controller"] is not None,
            d["unit_number"] is not None,
            d["ui_disk_number"] is not None,
            d["size"] not in (None, ""),
            (d["disk_provisioning"] is not None and str(d["disk_provisioning"]).strip() != "")
        ]):
            disks.append(d)
    return disks

def _clamp(n, lo, hi):
    try:
        n = int(n)
    except Exception:
        return lo
    return max(lo, min(hi, n))

def _compute_scsi_controller_count_from_disks(disks):
    """
    只依 DB 磁碟算出需要的 SCSI 控制器數：max(scsi_controller) + 1，範圍 1..4。
    若沒有任何磁碟資料，回傳 1。
    """
    buses = [d.get("scsi_controller") for d in (disks or []) if isinstance(d, dict) and d.get("scsi_controller") is not None]
    if not buses:
        return 1
    return _clamp(max(buses) + 1, 1, 4)

def _read_disks_from_db(db_conn, env, vm_name_prefix):
    """
    從 DB 補撈 additional_disks（防守用；正常情況下 form_data 會帶 additional_disks）。
    """
    try:
        cur = db_conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id FROM vm_configurations WHERE environment=%s AND vm_name_prefix=%s",
            (env, vm_name_prefix)
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return []
        vm_id = row["id"]
        cur.execute("""
            SELECT id, scsi_controller, unit_number, ui_disk_number,
                   size, disk_provisioning, thin_provisioned, eagerly_scrub
            FROM vm_disks
            WHERE vm_configuration_id=%s
            ORDER BY scsi_controller ASC, unit_number ASC
        """, (vm_id,))
        disks = cur.fetchall()
        cur.close()
        return disks or []
    except Exception as e:
        print(f"[WARN] _read_disks_from_db failed: {e}")
        return []

def insert_gitlab_pipeline_info_to_db(db_conn, workflow_id, pipeline_data, form_data):
    """
    寫入 gitlab_pipelines，並把「實際傳給 pipeline 的 vm_additional_disks_json（含 scsi_controller_count）」完整記錄。
    - 優先用 form_data['additional_disks']（建議你在 submit 前已 get_vm_config 取得最新 DB 狀態）。
    - 若無，回讀 DB（防守）。
    - 若還無，再退而求其次用表單陣列快照。
    """
    cursor = db_conn.cursor()

    # 1) 取 additional_disks
    additional_disks = None
    if isinstance(form_data.get('additional_disks'), list):
        additional_disks = form_data['additional_disks']
    if not additional_disks:
        env = form_data.get('environment')
        prefix = form_data.get('vm_name_prefix')
        if env and prefix:
            additional_disks = _read_disks_from_db(db_conn, env, prefix)
    if not additional_disks:
        additional_disks = _extract_disks_snapshot_from_form(form_data)

    # 2) 由磁碟資料「推算」 scsi_controller_count（完全不吃表單）
    scsi_controller_count = _compute_scsi_controller_count_from_disks(additional_disks)

    # 3) 組成 pipeline 要記錄/傳遞的 JSON（移除 ui_disk_number）
    sanitized_disks = []
    for d in (additional_disks or []):
        if not isinstance(d, dict):
            continue
        clean = d.copy()
        clean.pop("ui_disk_number", None)  # 移除 UI 專用欄位
        sanitized_disks.append(clean)

    payload_for_pipeline = {
        "scsi_controller_count": scsi_controller_count,
        "disks": sanitized_disks
    }
    vm_additional_disks_json = json.dumps(payload_for_pipeline, ensure_ascii=False)

    params = (
        workflow_id,
        pipeline_data.get("pipeline_id") or 0,
        pipeline_data.get("job_id"),
        pipeline_data.get("project_name") or f"project-{pipeline_data.get('project_id')}",
        pipeline_data.get("ref") or "",
        pipeline_data.get("sha") or "",
        pipeline_data.get("status") or "",
        "webform",
        pipeline_data.get("web_url") or "",
        form_data.get('action_type') or "",
        form_data.get('environment') or "",
        form_data.get('resource') or form_data.get('vsphere_resource') or "",
        form_data.get('os_type') or form_data.get('vm_os_type') or "",
        form_data.get('vsphere_datacenter') or "",
        form_data.get('vsphere_cluster') or "",
        form_data.get('vsphere_network') or "",
        form_data.get('vsphere_template') or "",
        form_data.get('vsphere_datastore') or "",
        form_data.get('vm_name_prefix') or "",
        form_data.get('vm_instance_type') or "",
        form_data.get('vm_num_cpus') or 0,
        form_data.get('vm_memory') or 0,
        vm_additional_disks_json,
        form_data.get('vm_ipv4_gateway') or "",
        form_data.get('netbox_prefix') or "",
        form_data.get('netbox_tenant') or ""
    )
    safe_params = tuple(_sanitize(v) for v in params)

    try:
        cursor.execute("""
            INSERT INTO gitlab_pipelines (
                workflow_id, pipeline_id, job_id, project_name, branch,
                commit_sha, status, triggered_by, web_url, action_type,
                environment, resource, os_type, vsphere_datacenter, vsphere_cluster,
                vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
                vm_instance_type, vm_num_cpus, vm_memory, vm_additional_disks_json,
                vm_ipv4_gateway, netbox_prefix, netbox_tenant
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
        """, safe_params)

        db_conn.commit()
        print(f"GitLab pipeline info inserted to DB successfully. Pipeline ID: {pipeline_data.get('pipeline_id')}")

    except Error as e:
        db_conn.rollback()
        print(f"Error inserting GitLab pipeline info to DB: {e}")
        raise
    finally:
        cursor.close()