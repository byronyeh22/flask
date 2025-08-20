# app/vsphere/vm/db/insert_vm_configuration_to_db.py
from mysql.connector import Error
import re

# ====== 小工具 ======
def _get_next_available_unit_number(used_numbers):
    """兼容舊邏輯：回傳最小未使用的正整數 unit_number（已不再單獨使用）。"""
    if not used_numbers:
        return 1
    used = set(int(x) for x in used_numbers if x is not None)
    n = 1
    while True:
        if n not in used:
            return n
        n += 1

def _first_scalar(v, default=None):
    """Flatten values coming from request.form.to_dict(flat=False) to a scalar."""
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default

def _as_list(v):
    """確保以 list 回傳（支援 None/純量/list）。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]

def _to_int(v, default=0):
    v = _first_scalar(v, None)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

_label_unit_re = re.compile(r'(\d+)\s*$')

def _unit_from_label(label):
    """
    Parse unit_number from label like 'Hard Disk 5' -> 4 (因 unit 從 0 起算；系統碟為 0，不在此列)。
    若無法解析則回傳 None。此函式僅保留相容性，現以 SCSI slot 為主。
    """
    if not label:
        return None
    m = _label_unit_re.search(str(label))
    if not m:
        return None
    try:
        n = int(m.group(1))
        return max(1, n - 1)
    except Exception:
        return None

# ====== SCSI 規則與分配器 ======
# 允許的 controller(bus)：0..3（共 4 個）
_SCSI_CONTROLLERS = range(0, 4)
# 允許的 unit：0..15 跳過 7（每個 bus 15 個 slot，7 保留）
_VALID_UNITS = [0,1,2,3,4,5,6,8,9,10,11,12,13,14,15]
# 系統碟固定保留在 scsi(0:0)
_SYSTEM_SLOT = (0, 0)

def _valid_slot(controller, unit):
    """檢查 slot 是否有效（controller 在 0..3；unit 在允許清單；且允許 1:0/2:0/3:0，但排除 0:0）。"""
    try:
        c = int(controller)
        u = int(unit)
    except Exception:
        return False
    if c not in _SCSI_CONTROLLERS:
        return False
    if u not in _VALID_UNITS:
        return False
    if (c, u) == _SYSTEM_SLOT:
        return False  # 系統碟
    return True

def _next_free_scsi_slot(used):
    """
    取得下一個可用 (controller, unit)：
    - controller 從 0..3
    - unit 從 _VALID_UNITS 順序掃描，跳過 (0,0)
    - 可回傳 (1:0)/(2:0)/(3:0)
    """
    used = set((int(c), int(u)) for (c, u) in used if c is not None and u is not None)
    for c in _SCSI_CONTROLLERS:
        for u in _VALID_UNITS:
            if (c, u) == _SYSTEM_SLOT:
                continue
            if (c, u) not in used:
                return (c, u)
    raise Exception("No free SCSI slot available (controllers 0..3 exhausted)")

def _compute_controller_count_from_used(used):
    """
    由已使用的 slots 計算 vm_scsi_controller_count = max(controller) + 1，
    範圍限制 1..4，至少 1。
    """
    used = [(c, u) for (c, u) in used if c is not None and u is not None]
    if not used:
        return 1
    max_c = max(c for c, _ in used)
    return max(1, min(max_c + 1, 4))

def _relabel_existing_and_get_next(cursor, vm_id):
    """
    只針對「目前 DB 已存在的磁碟」依 (controller, unit) 排序後重排 ui_disk_number（2..N），
    並回傳下一個可用的 label 號碼 (N+1)。
    """
    cursor.execute(
        """
        SELECT id, scsi_controller, unit_number
        FROM vm_disks
        WHERE vm_configuration_id=%s
        ORDER BY scsi_controller ASC, unit_number ASC
        """,
        (vm_id,)
    )
    ordered = cursor.fetchall() or []
    next_label_no = 2  # Hard Disk 1 = system disk
    for r in ordered:
        if r.get('id'):
            cursor.execute(
                "UPDATE vm_disks SET ui_disk_number=%s WHERE id=%s",
                (next_label_no, r['id'])
            )
            next_label_no += 1
    return next_label_no

# ====== 主流程 ======
def insert_vm_configuration_to_db(db_conn, form_data):
    """
    Upsert into vm_configurations, then sync vm_disks（以 SCSI slot 為主）：
    - Update 既有磁碟（以 id 為準）：只更新 size/provisioning/flags，不改動 scsi_controller/unit_number
    - Delete 表單沒帶到的舊資料列
    - **先重排現有 ui_disk_number（2..N）**
    - Insert 新增的磁碟：
        * 若表單帶了 scsi_controller/unit_number 且合法/未占用，照用
        * 否則用 _next_free_scsi_slot() 自動分配（允許 1:0/2:0/3:0；保留 0:0；跳過 7）
        * **ui_disk_number 一律用“下一個號碼”往後編**（不回填剛釋出的號碼）
    - 更新 vm_scsi_controller_count = max(controller)+1（1..4）
    """
    cursor = db_conn.cursor(dictionary=True)

    # ---- Main configuration (flatten to scalars) ----
    env              = _first_scalar(form_data.get('environment'))
    vm_name_prefix   = _first_scalar(form_data.get('vm_name_prefix'))
    resource         = _first_scalar(form_data.get('resource') or form_data.get('vsphere_resource'))
    os_type          = _first_scalar(form_data.get('os_type') or form_data.get('vm_os_type'))
    vs_dc            = _first_scalar(form_data.get('vsphere_datacenter'))
    vs_cluster       = _first_scalar(form_data.get('vsphere_cluster'))
    vs_network       = _first_scalar(form_data.get('vsphere_network'))
    vs_template      = _first_scalar(form_data.get('vsphere_template'))
    vs_datastore     = _first_scalar(form_data.get('vsphere_datastore'))
    vm_instance_type = _first_scalar(form_data.get('vm_instance_type'))
    vm_num_cpus      = _to_int(form_data.get('vm_num_cpus'), 0)
    vm_memory        = _to_int(form_data.get('vm_memory'), 0)
    vm_ipv4_ip       = _first_scalar(form_data.get('vm_ipv4_ip'))
    vm_ipv4_gateway  = _first_scalar(form_data.get('vm_ipv4_gateway'))
    netbox_prefix    = _first_scalar(form_data.get('netbox_prefix'))
    netbox_tenant    = _first_scalar(form_data.get('netbox_tenant'))

    if not env or not vm_name_prefix:
        raise Exception("Missing required environment or vm_name_prefix")

    try:
        # 1) Upsert vm_configurations（保留你的欄位與 UNIQUE(uq_vm)）
        upsert_sql = """
            INSERT INTO vm_configurations (
                environment, resource, os_type, vsphere_datacenter, vsphere_cluster,
                vsphere_network, vsphere_template, vsphere_datastore, vm_name_prefix,
                vm_instance_type, vm_num_cpus, vm_memory, vm_ipv4_ip,
                vm_ipv4_gateway, netbox_prefix, netbox_tenant
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                resource=VALUES(resource),
                os_type=VALUES(os_type),
                vsphere_datacenter=VALUES(vsphere_datacenter),
                vsphere_cluster=VALUES(vsphere_cluster),
                vsphere_network=VALUES(vsphere_network),
                vsphere_template=VALUES(vsphere_template),
                vsphere_datastore=VALUES(vsphere_datastore),
                vm_instance_type=VALUES(vm_instance_type),
                vm_num_cpus=VALUES(vm_num_cpus),
                vm_memory=VALUES(vm_memory),
                vm_ipv4_ip=VALUES(vm_ipv4_ip),
                vm_ipv4_gateway=VALUES(vm_ipv4_gateway),
                netbox_prefix=VALUES(netbox_prefix),
                netbox_tenant=VALUES(netbox_tenant),
                updated_at=CURRENT_TIMESTAMP
        """
        cursor.execute(upsert_sql, (
            env, resource, os_type, vs_dc, vs_cluster,
            vs_network, vs_template, vs_datastore, vm_name_prefix,
            vm_instance_type, vm_num_cpus, vm_memory, vm_ipv4_ip,
            vm_ipv4_gateway, netbox_prefix, netbox_tenant
        ))

        # 2) 取得 vm_configuration id
        vm_id = cursor.lastrowid
        if not vm_id:
            cursor.execute(
                "SELECT id FROM vm_configurations WHERE environment=%s AND vm_name_prefix=%s",
                (env, vm_name_prefix)
            )
            row = cursor.fetchone()
            vm_id = row['id'] if row else None
        if not vm_id:
            raise Exception("Could not determine vm_configuration_id")

        # 3) 載入現有磁碟（以 slot 穩定對應）
        cursor.execute(
            """
            SELECT id, scsi_controller, unit_number, ui_disk_number,
                   size, disk_provisioning, thin_provisioned, eagerly_scrub
            FROM vm_disks
            WHERE vm_configuration_id=%s
            ORDER BY scsi_controller ASC, unit_number ASC
            """,
            (vm_id,)
        )
        current_rows = cursor.fetchall()
        current_by_id = {r['id']: r for r in current_rows}
        used_slots = {(r['scsi_controller'], r['unit_number']) for r in current_rows}

        # 4) 讀表單陣列（update*/create*/generic）
        form_disk_db_ids = (
            _as_list(form_data.get('update_disk_db_id[]'))
            or _as_list(form_data.get('disk_db_id[]'))
        )
        sizes = (
            _as_list(form_data.get('update_vm_disk_size[]'))
            or _as_list(form_data.get('vm_disk_size[]'))
            or _as_list(form_data.get('create_vm_disk_size[]'))
        )
        provs = (
            _as_list(form_data.get('update_vm_disk_provisioning[]'))
            or _as_list(form_data.get('vm_disk_provisioning[]'))
            or _as_list(form_data.get('create_vm_disk_provisioning[]'))
        )
        thins = (
            _as_list(form_data.get('update_vm_disk_thin_provisioned[]'))
            or _as_list(form_data.get('vm_disk_thin_provisioned[]'))
            or _as_list(form_data.get('create_vm_disk_thin_provisioned[]'))
        )
        eagers = (
            _as_list(form_data.get('update_vm_disk_eagerly_scrub[]'))
            or _as_list(form_data.get('vm_disk_eagerly_scrub[]'))
            or _as_list(form_data.get('create_vm_disk_eagerly_scrub[]'))
        )
        # SCSI 位置
        scsis = (
            _as_list(form_data.get('update_scsi_controller[]'))
            or _as_list(form_data.get('scsi_controller[]'))
            or _as_list(form_data.get('create_vm_disk_scsi_controller[]'))
        )
        units = (
            _as_list(form_data.get('update_unit_number[]'))
            or _as_list(form_data.get('unit_number[]'))
            or _as_list(form_data.get('create_vm_disk_unit_number[]'))
        )
        # UI label（如果前端帶了也會被覆蓋為我們的重排規則）
        uinos = (
            _as_list(form_data.get('update_ui_disk_number[]'))
            or _as_list(form_data.get('ui_disk_number[]'))
        )

        n = max(len(sizes), len(provs), len(thins), len(eagers), len(form_disk_db_ids), len(scsis), len(units), len(uinos))

        # === 階段 1：刪除 + 更新既有（不動 slot）===
        # 5) 刪除：以 id 為準，表單沒帶到的舊資料列刪除
        keep_ids = set()
        for i in range(n):
            if i < len(form_disk_db_ids):
                try:
                    did = int(form_disk_db_ids[i]) if str(form_disk_db_ids[i]).strip() != "" else None
                    if did:
                        keep_ids.add(did)
                except Exception:
                    pass
        to_delete = set(current_by_id.keys()) - keep_ids
        if to_delete:
            q = "DELETE FROM vm_disks WHERE id IN ({})".format(",".join(["%s"] * len(to_delete)))
            cursor.execute(q, tuple(to_delete))
            deleted_slots = {(current_by_id[i]['scsi_controller'], current_by_id[i]['unit_number']) for i in to_delete if i in current_by_id}
            used_slots = {s for s in used_slots if s not in deleted_slots}

        # 6) 更新既有（僅屬性）
        to_insert_indices = []
        for idx in range(n):
            form_id_str = form_disk_db_ids[idx] if idx < len(form_disk_db_ids) else ""
            size = sizes[idx] if idx < len(sizes) else None
            prov = provs[idx] if idx < len(provs) else "thin"

            thin = None
            eager = None
            if idx < len(thins):  thin = str(thins[idx]).lower() == 'true'
            if idx < len(eagers): eager = str(eagers[idx]).lower() == 'true'
            if thin is None:  thin = (prov == 'thin')
            if eager is None: eager = (prov == 'thick_eager')

            if form_id_str:
                try:
                    disk_id = int(form_id_str)
                except Exception:
                    disk_id = None
                if disk_id and disk_id in current_by_id:
                    cursor.execute(
                        """
                        UPDATE vm_disks
                           SET size=%s, disk_provisioning=%s, thin_provisioned=%s, eagerly_scrub=%s
                         WHERE id=%s
                        """,
                        (int(size), prov, thin, eager, disk_id)
                    )
                else:
                    # stale id 視為新碟（留到階段 2 插入）
                    to_insert_indices.append(idx)
            else:
                # 新碟（留到階段 2 插入）
                to_insert_indices.append(idx)

        # 6.5) **重排現有的 ui_disk_number，取得下一個號碼**
        next_label_no = _relabel_existing_and_get_next(cursor, vm_id)

        # === 階段 2：插入新碟（slot 依規則；label 一律用 next_label_no++）===
        for idx in to_insert_indices:
            size = sizes[idx] if idx < len(sizes) else None
            prov = provs[idx] if idx < len(provs) else "thin"

            thin = None
            eager = None
            if idx < len(thins):  thin = str(thins[idx]).lower() == 'true'
            if idx < len(eagers): eager = str(eagers[idx]).lower() == 'true'
            if thin is None:  thin = (prov == 'thin')
            if eager is None: eager = (prov == 'thick_eager')

            controller = scsis[idx] if idx < len(scsis) else None
            unit = units[idx] if idx < len(units) else None
            if _valid_slot(controller, unit) and (int(controller), int(unit)) not in used_slots:
                c, u = int(controller), int(unit)
            else:
                c, u = _next_free_scsi_slot(used_slots)

            # **關鍵：新碟的 label 直接用 next_label_no（不回填釋出的號碼）**
            ui_num = next_label_no
            next_label_no += 1

            cursor.execute(
                """
                INSERT INTO vm_disks
                  (vm_configuration_id, scsi_controller, unit_number, ui_disk_number,
                   size, disk_provisioning, thin_provisioned, eagerly_scrub)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (vm_id, c, u, ui_num, int(size), prov, thin, eager)
            )
            used_slots.add((c, u))

        # 7) 更新 vm_scsi_controller_count（= 已使用的最大 controller + 1；限制 1..4）
        controller_count = _compute_controller_count_from_used(used_slots)
        cursor.execute(
            "UPDATE vm_configurations SET vm_scsi_controller_count=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (controller_count, vm_id)
        )

        # 不再做一次全局 label 重排（避免把新碟洗回去）

        db_conn.commit()
        print(f"[vm_disks] synced: vm_id={vm_id}, final_slots={sorted(list(used_slots))}, scsi_controller_count={controller_count}")

    except Error as e:
        db_conn.rollback()
        print(f"Database error in insert_vm_configuration_to_db: {e}")
        raise
    except Exception as e:
        db_conn.rollback()
        print(f"An unexpected error occurred in insert_vm_configuration_to_db: {e}")
        raise
    finally:
        try:
            cursor.close()
        except Exception:
            pass