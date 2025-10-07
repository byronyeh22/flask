# app/fortigate/drafts/db/drafts_handler.py
import json
from typing import Optional, Tuple, List
from collections import Counter
from datetime import datetime
from app.db.mysql import get_db_connection
from ...workflow import DraftStatus

def _dc(conn):
    return conn.cursor(dictionary=True)
# === Time Helper ===
def _norm_dt(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec='seconds')  # 'YYYY-MM-DDTHH:MM:SS'
    if isinstance(v, str) and v:
        s = v.strip()
        # 去掉 MySQL 回傳的微秒 '.000000'
        if '.' in s:
            s = s.split('.')[0]
        return s.replace(' ', 'T')
    return ''

# === 取裝置名稱（用於 title 顯示 forti_devices.name） ===
def _get_device_name(cur, device_id) -> str:
    try:
        cur.execute("SELECT name FROM forti_devices WHERE id=%s", (device_id,))
        r = cur.fetchone()
        if r and r.get("name"):
            return r["name"]
    except Exception:
        pass
    # fallback：沒查到就回傳 id 字串
    return str(device_id) if device_id is not None else "-"

# === 行為驗證 ===
VALID_ACTIONS = {"create", "update", "delete"}

def _policy_exists(cur, device_id, vdom, res_id) -> bool:
    if device_id is None or vdom is None or res_id is None:
        return False
    cur.execute("""
        SELECT 1
          FROM forti_policies_current
         WHERE device_id=%s AND vdom=%s AND fg_policy_id=%s
         LIMIT 1
    """, (int(device_id), str(vdom), int(res_id)))
    return cur.fetchone() is not None

def _validate_action_types(draft_action: dict) -> tuple[bool, list]:
    """
    規則：
      - action_type 必填且必須在 {create, update, delete}
      - update/delete 必須帶 resource_id，且 DB 存在該 policy
      - 若不存在，則只能選 create（直接報錯）
    回傳：(ok, violations)
    violations 內含 dict，方便前端顯示。
    """
    violations = []
    plans = []
    try:
        obj = draft_action or {}
        if isinstance(obj, str):
            obj = json.loads(obj) if obj else {}
        plans = obj.get("action_plan") or []
    except Exception:
        pass

    if not isinstance(plans, list):
        flash("Invalid action_plan format.", "danger")
        return False, [{"error": "invalid_plan_format"}]

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        for idx, p in enumerate(plans):
            if not isinstance(p, dict):
                violations.append({"index": idx, "error": "invalid_plan_item"})
                continue

            aid   = p.get("action_id")
            atype = (p.get("action_type") or "").strip().lower()
            dev   = p.get("device_id")
            vdom  = p.get("vdom")
            rid   = p.get("resource_id")

            if atype not in VALID_ACTIONS:
                msg = f"Plan {aid or idx}: Action is required and must be one of create/update/delete."
                violations.append({"index": idx, "action_id": aid, "error": "invalid_action_type", "message": msg})
                continue

            if atype in ("update", "delete"):
                if rid in (None, ""):
                    msg = f"Plan {aid or idx}: {atype} requires resource_id."
                    violations.append({"index": idx, "action_id": aid, "error": "missing_resource_id", "message": msg})
                    continue
                exists = _policy_exists(cur, dev, vdom, rid)
                if not exists:
                    msg = (
                        f"Plan {aid or idx}: policy not found (device={dev}, vdom={vdom}, id={rid}). "
                        f"Only 'create' is allowed for a non-existing policy."
                    )
                    violations.append({"index": idx, "action_id": aid, "error": "target_not_found", "message": msg})
                    continue

            # atype == create 不限制（可視需求再加：若要禁止 create 指向已存在，也可在這裡補查）

        ok = (len(violations) == 0)
        return ok, violations
    finally:
        cur.close()
        conn.close()


# === NEW: 依 draft_action 與 forti_policies_current 產生簡易 diff 報告 ===
def compute_draft_check_report(draft_action: dict) -> dict:
    """
    產出兩層：
      1) actions[]：給前端 renderDiffFromCheckReport() 直接使用（每筆含 diff 明細）
      2) details[] + impacts：保留舊版摘要（相容前端列表/其它地方）

    diff 規則：
      - current == draft  → result: "No Change"
      - current is empty, draft 有值 → "Added"
      - current 有值, draft 為空 → "Removed"
      - 其餘 → "Changed"
    NAT/Status 正規化為 enable/disable；陣列欄位以 ', ' 展示。
    """

    def _parse_arr(v):
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        if isinstance(v, str):
            try:
                j = json.loads(v)
                if isinstance(j, list):
                    return [str(x) for x in j if str(x).strip()]
            except Exception:
                pass
            return [s.strip() for s in v.split(",") if s.strip()]
        return [str(v)]

    def _arr_from_aliases(p: dict, *aliases) -> List[str]:
        """若 payload 內有任一 alias，回傳解析後的陣列；否則回空陣列。"""
        for k in aliases:
            if k in p:
                return _parse_arr(p.get(k))
        return []

    def _get_first(p: dict, *aliases) -> str:
        """依序取第一個存在且非空的欄位值（字串或標量），找不到回空字串。"""
        for k in aliases:
            if k in p:
                v = p.get(k)
                if v is None or v == "":
                    continue
                return v
        return ""

    def _any_key(p: dict, *keys) -> bool:
        return any(k in p for k in keys)

    def _is_enabled_like(v) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        s = str(v).strip().lower()
        return s in ("1", "true", "yes", "on", "enable", "enabled")

    def _nat_db_display(v, has_current: bool) -> str:
        # DB 端：只有「有取到 current」才顯示；否則保持空字串，避免誤導
        if not has_current:
            return ""
        return "enable" if _is_enabled_like(v) else "disable"

    def _nat_draft_display(v) -> str:
        if v is None or v == "":
            return ""
        return "enable" if _is_enabled_like(v) else "disable"

    def _status_db_display(v, has_current: bool) -> str:
        if not has_current:
            return ""
        return "enable" if _is_enabled_like(v) else "disable"

    def _status_draft_display(v, present: bool) -> str:
        if not present:
            return ""
        return "enable" if _is_enabled_like(v) else "disable"

    def _to_display(v):
        if isinstance(v, list):
            return ", ".join([str(x) for x in v])
        if v is None:
            return ""
        return str(v)

    def _cmp(current, draft):
        cur = _to_display(current)
        dr = _to_display(draft)
        if cur == dr:
            tag = "No Change"
        elif (not cur) and dr:
            tag = "Added"
        elif cur and (not dr):
            tag = "Removed"
        else:
            tag = "Changed"
        return cur, dr, tag

    # 欄位順序與顯示名稱
    FIELD_ORDER = [
        ("name",       "Name"),
        ("action",     "Action"),
        ("status",     "Status"),
        ("nat",        "NAT"),
        ("webfilter",  "Web Filter"),
        ("src_addrs",  "Src Addresses"),
        ("dst_addrs",  "Dst Addresses"),
        ("services",   "Services"),
        ("src_intfs",  "Src Interfaces"),
        ("dst_intfs",  "Dst Interfaces"),
        ("comment",    "Comment"),
    ]

    # 解析 draft_action
    try:
        da = draft_action or {}
        if isinstance(da, str):
            da = json.loads(da) if da else {}
        plans = da.get("action_plan") or []
    except Exception:
        plans = []

    conn = get_db_connection()
    cur = _dc(conn)
    try:
        def _get_policy(device_id, vdom, pid):
            cur.execute("""
                SELECT *
                  FROM forti_policies_current
                 WHERE device_id=%s AND vdom=%s AND fg_policy_id=%s
                 LIMIT 1
            """, (int(device_id), str(vdom), int(pid)))
            return cur.fetchone() or {}

        impacts = {"new": 0, "update": 0, "delete": 0}
        details_legacy = []
        actions_rich = []
        errors = []
        warnings = []

        # 依 action_order 排序，確保顯示順序穩定
        sorted_plans = sorted(plans, key=lambda a: a.get("action_order", 0))

        for a in sorted_plans:
            if (a.get("kind") or "").lower() != "policy":
                continue

            at = (a.get("action_type") or "").lower()
            dev_id = a.get("device_id")
            vdom = a.get("vdom")
            res_id = a.get("resource_id")
            payload = a.get("payload") or {}
            action_id = a.get("action_id")
            action_order = int(a.get("action_order") or 0)

            # --- create ---
            if at == "create":
                impacts["new"] += 1

                # create 沒有 current，只把 payload 畫為 Added
                diff_rows = []
                has_current = False

                # 特殊處理 NAT/Status 顯示與陣列欄位＋別名
                def _val_for_field(key):
                    if key == "src_addrs":
                        return _arr_from_aliases(payload, "src_addrs", "src_addresses")
                    if key == "dst_addrs":
                        return _arr_from_aliases(payload, "dst_addrs", "dst_addresses")
                    if key == "services":
                        return _arr_from_aliases(payload, "services", "service", "service_name")
                    if key == "src_intfs":
                        return _arr_from_aliases(payload, "src_intfs", "src_if", "src_iface", "src_interface", "src_interfaces")
                    if key == "dst_intfs":
                        return _arr_from_aliases(payload, "dst_intfs", "dst_if", "dst_iface", "dst_interface", "dst_interfaces")
                    if key == "nat":
                        return _nat_draft_display(_get_first(payload, "nat"))
                    if key == "status":
                        present = "status" in payload
                        return _status_draft_display(payload.get("status"), present)
                    if key == "comment":
                        return _get_first(payload, "comment", "comments")
                    if key == "webfilter":
                        return _get_first(payload, "webfilter", "web-filter", "web_filter")
                    # 其它欄位維持原值
                    return payload.get(key)

                for k, label in FIELD_ORDER:
                    dr = _val_for_field(k)
                    curS, drS, tag = _cmp("" if k not in ("nat", "status") else (_nat_db_display(None, has_current) if k == "nat" else _status_db_display(None, has_current)), dr)
                    # create：只顯示有填的欄位，避免噪音
                    if drS:
                        diff_rows.append({
                            "field": label,
                            "current": curS or "",
                            "draft": drS or "",
                            "result": tag if tag != "No Change" else "Added"
                        })

                # 若真的沒有任何有效欄位，拋出警告方便 UI / 審批看見
                if not diff_rows:
                    warnings.append({
                        "action_id": action_id,
                        "message": "Create payload has no effective fields (no diff). Please provide at least one field such as name/addresses/services/interfaces/action/status."
                    })

                actions_rich.append({
                    "action_id": action_id,
                    "action_type": "create",
                    "device_id": dev_id,
                    "vdom": vdom,
                    "action_order": action_order,
                    "diff": diff_rows,
                    "summary": {"fields": list(payload.keys())}
                })
                details_legacy.append({
                    "action_id": action_id,
                    "action_type": "create",
                    "device_id": dev_id,
                    "vdom": vdom,
                    "summary": {"fields": list(payload.keys())}
                })
                continue

            # --- update / delete 需要查 current ---
            before = None
            exists = False
            if res_id is not None:
                try:
                    before = _get_policy(dev_id, vdom, res_id)
                    exists = bool(before)
                except Exception:
                    exists = False

            if at == "delete":
                impacts["delete"] += 1
                if not exists:
                    errors.append({
                        "action_id": action_id,
                        "message": f"Target policy not found for delete: device={dev_id}, vdom={vdom}, id={res_id}"
                    })

                # 用單列提示刪除
                diff_rows = [{
                    "field": "Policy",
                    "current": f"#{res_id}" if res_id is not None else "",
                    "draft": "",
                    "result": "Removed"
                }]

                actions_rich.append({
                    "action_id": action_id,
                    "action_type": "delete",
                    "device_id": dev_id,
                    "vdom": vdom,
                    "resource_id": res_id,
                    "action_order": action_order,
                    "diff": diff_rows,
                    "summary": {"exists": exists}
                })
                details_legacy.append({
                    "action_id": action_id,
                    "action_type": "delete",
                    "device_id": dev_id,
                    "vdom": vdom,
                    "resource_id": res_id,
                    "summary": {"exists": exists}
                })
                continue

            # --- update ---
            if at == "update":
                impacts["update"] += 1
                if not exists:
                    errors.append({
                        "action_id": action_id,
                        "message": f"Target policy not found for update: device={dev_id}, vdom={vdom}, id={res_id}"
                    })

                # 把 current 正規化
                cur_name      = (before or {}).get("name")
                cur_action    = (before or {}).get("action")
                cur_status    = _status_db_display((before or {}).get("status"), has_current=exists)
                cur_nat       = _nat_db_display((before or {}).get("nat"), has_current=exists)
                cur_webfilter = (before or {}).get("webfilter") or (before or {}).get("web-filter") or (before or {}).get("web_filter") or ""
                cur_src_addrs = _parse_arr((before or {}).get("src_addrs"))
                cur_dst_addrs = _parse_arr((before or {}).get("dst_addrs"))
                cur_services  = _parse_arr((before or {}).get("services"))
                cur_src_intfs = _parse_arr((before or {}).get("src_intfs"))
                cur_dst_intfs = _parse_arr((before or {}).get("dst_intfs"))
                cur_comment   = (before or {}).get("comments") or (before or {}).get("comment") or ""

                # 拿 draft 值（沒出現在 payload 就當空，代表不改），NAT/Status/array 特殊處理＋別名
                def _draft_val(key):
                    if key == "src_addrs":
                        return _arr_from_aliases(payload, "src_addrs", "src_addresses") if _any_key(payload, "src_addrs", "src_addresses") else ""
                    if key == "dst_addrs":
                        return _arr_from_aliases(payload, "dst_addrs", "dst_addresses") if _any_key(payload, "dst_addrs", "dst_addresses") else ""
                    if key == "services":
                        return _arr_from_aliases(payload, "services", "service", "service_name") if _any_key(payload, "services", "service", "service_name") else ""
                    if key == "src_intfs":
                        return _arr_from_aliases(payload, "src_intfs", "src_if", "src_iface", "src_interface", "src_interfaces") if _any_key(payload, "src_intfs", "src_if", "src_iface", "src_interface", "src_interfaces") else ""
                    if key == "dst_intfs":
                        return _arr_from_aliases(payload, "dst_intfs", "dst_if", "dst_iface", "dst_interface", "dst_interfaces") if _any_key(payload, "dst_intfs", "dst_if", "dst_iface", "dst_interface", "dst_interfaces") else ""
                    if key == "nat":
                        # 只要 draft 裡有出現 nat，就用 enable/disable 顯示；沒有就空字串（當不改）
                        return _nat_draft_display(payload.get("nat")) if "nat" in payload else ""
                    if key == "status":
                        return _status_draft_display(payload.get("status"), "status" in payload)
                    if key == "webfilter":
                        if _any_key(payload, "webfilter", "web-filter", "web_filter"):
                            return _get_first(payload, "webfilter", "web-filter", "web_filter")
                        return ""
                    if key == "comment":
                        if _any_key(payload, "comment", "comments"):
                            return _get_first(payload, "comment", "comments")
                        return ""
                    # 其它標量欄位只有在 key 存在時才取值
                    return payload.get(key, "") if key in payload else ""

                cur_map = {
                    "name": cur_name, "action": cur_action, "status": cur_status, "nat": cur_nat,
                    "webfilter": cur_webfilter,
                    "src_addrs": cur_src_addrs, "dst_addrs": cur_dst_addrs, "services": cur_services,
                    "src_intfs": cur_src_intfs, "dst_intfs": cur_dst_intfs, "comment": cur_comment,
                }

                diff_rows = []
                changed_keys = []
                for k, label in FIELD_ORDER:
                    dr = _draft_val(k)
                    # update：只有當 draft 有提供該欄位，才列進 diff，避免噪音
                    if dr == "" or (isinstance(dr, list) and not dr):
                        continue
                    curS, drS, tag = _cmp(cur_map[k], dr)
                    diff_rows.append({
                        "field": label,
                        "current": curS or "",
                        "draft": drS or "",
                        "result": tag
                    })
                    if tag != "No Change":
                        changed_keys.append(k)

                actions_rich.append({
                    "action_id": action_id,
                    "action_type": "update",
                    "device_id": dev_id,
                    "vdom": vdom,
                    "resource_id": res_id,
                    "action_order": action_order,
                    "diff": diff_rows,
                    "summary": {"changed_keys": changed_keys, "change_count": len(changed_keys), "exists": bool(exists)}
                })
                details_legacy.append({
                    "action_id": action_id,
                    "action_type": "update",
                    "device_id": dev_id,
                    "vdom": vdom,
                    "resource_id": res_id,
                    "summary": {"changed_keys": changed_keys, "exists": bool(exists)}
                })
                continue

        return {
            "checked_actions": len(actions_rich),
            "impacts": impacts,
            "warnings": warnings,
            "errors": errors,            # 若有 exists:false，這裡會帶出一筆描述
            "actions": actions_rich,     # ★ 新增：前端可直接渲染
            "details": details_legacy,   # ★ 保留舊版欄位（兼容）
        }
    finally:
        cur.close()
        conn.close()

def _autogen_title(cur, draft_action: dict) -> str:
    try:
        da = draft_action or {}
        if isinstance(da, str):
            da = json.loads(da) if da else {}
        plans = da.get("action_plan") or []
        if not isinstance(plans, list) or not plans:
            return "[Fortigate Request] Policy Change"

        # 依 action_order 排序，避免順序亂
        plans = sorted(plans, key=lambda a: a.get("action_order", 0))

        if len(plans) == 1:
            p = plans[0] or {}
            action_type = (p.get("action_type") or "").strip().lower()  # create / update / delete
            kind = (p.get("kind") or "policy").strip().lower()
            dev_name = _get_device_name(cur, p.get("device_id"))
            vdom = p.get("vdom") or ""
            payload = p.get("payload") or {}
            # 標籤：優先 name，否則用 #resource_id
            label = ""
            if action_type in ("update", "delete") and p.get("resource_id"):
                label = f"#{p['resource_id']}"
            if (not label) and isinstance(payload, dict) and payload.get("name"):
                label = payload["name"]

            action_title = action_type.capitalize() if action_type else "Change"
            at_dev = f"{dev_name}/{vdom}" if vdom else str(dev_name)
            label = label or kind.capitalize()
            return f"[Fortigate Request] Policy {action_title} - {label} @ {at_dev}"

        # 多個 plans：顯示總數＋各類型數量
        cnt = Counter([(p.get("action_type") or "change").lower() for p in plans])
        kinds = []
        for k in ("create", "update", "delete", "change"):
            if cnt.get(k):
                kinds.append(f"{cnt[k]} {k}")
        kinds_str = ", ".join(kinds) if kinds else ""
        return f"[Fortigate Request] Policy Change - {len(plans)} plans" + (f" ({kinds_str})" if kinds_str else "")

    except Exception:
        # 解析失敗時保底
        return "[Fortigate Request] Policy Change"

def _build_timeline(cur, drow):
    evts = []

    def add(t, at, by=None, note=None, status=None, rng=None):
        # 統一把時間轉成 'YYYY-MM-DDTHH:MM:SS' 字串；空值就不加事件
        at_s = _norm_dt(at)
        if not at_s:
            return
        e = {"type": t, "at": at_s}
        if by:
            e["by"] = by
        if note:
            e["note"] = note
        if status:
            e["status"] = status
        if isinstance(rng, dict):
            start_s = _norm_dt(rng.get("start"))
            end_s = _norm_dt(rng.get("end"))
            r = {}
            if start_s:
                r["start"] = start_s
            if end_s:
                r["end"] = end_s
            if r:
                e["range"] = r
        evts.append(e)

    # submit
    add("submit", drow.get("submitted_at"), by=drow.get("submitted_by_name") or drow.get("created_by_name"))

    # === 审批節點：分開取決策（時間一律用 approvals.decided_at）===
    # approved
    cur.execute("""
        SELECT decided_at, u.username AS name
          FROM forti_draft_approvals a
          LEFT JOIN users u ON u.id = a.approver_id
         WHERE a.draft_id=%s AND a.decision='approved'
         ORDER BY decided_at DESC LIMIT 1
    """, (drow["id"],))
    ap_ok = cur.fetchone()
    if ap_ok:
        add("approved", ap_ok["decided_at"], by=ap_ok["name"])

    # rejected
    cur.execute("""
        SELECT decided_at, a.comment, u.username AS name
          FROM forti_draft_approvals a
          LEFT JOIN users u ON u.id = a.approver_id
         WHERE a.draft_id=%s AND a.decision='rejected'
         ORDER BY decided_at DESC LIMIT 1
    """, (drow["id"],))
    ap_rj = cur.fetchone()
    if ap_rj:
        add("rejected", ap_rj["decided_at"], by=ap_rj["name"], note=ap_rj.get("comment"))

    # canceled（若無資料、且狀態是 Canceled，最後保底用 updated_at  建立者名）
    cur.execute("""
        SELECT decided_at, a.comment, u.username AS name
          FROM forti_draft_approvals a
          LEFT JOIN users u ON u.id = a.approver_id
         WHERE a.draft_id=%s AND a.decision='canceled'
         ORDER BY decided_at DESC LIMIT 1
    """, (drow["id"],))
    ap_cz = cur.fetchone()
    if ap_cz:
        add("canceled", ap_cz["decided_at"], by=ap_cz["name"], note=ap_cz.get("comment"))
    elif drow.get("status") == "Canceled":
        # 舊資料相容：早期未寫入 approvals 時，保底仍顯示
        add("canceled", drow.get("updated_at"), by=drow.get("created_by_name"))
    # deploy（最新一個 task）
    cur.execute("""
        SELECT t.id, t.status, t.created_at,
               MIN(r.started_at)  AS started_at,
               MAX(r.finished_at) AS finished_at,
               SUM(r.status='ok')      AS ok_cnt,
               SUM(r.status='error')   AS err_cnt,
               SUM(r.status='skipped') AS skipped_cnt
          FROM forti_tasks t
          LEFT JOIN forti_task_action_results r ON r.task_id = t.id
         WHERE t.draft_id=%s
         GROUP BY t.id
         ORDER BY t.created_at DESC
         LIMIT 1
    """, (drow["id"],))
    t = cur.fetchone()
    if t:
        if t["status"] == "success" and (t["err_cnt"] or 0) == 0:
            outcome = "success"
        elif t["status"] == "failed" or (t["err_cnt"] or 0) > 0 and (t["ok_cnt"] or 0) == 0:
            outcome = "failed"
        elif (t["err_cnt"] or 0) > 0 and (t["ok_cnt"] or 0) > 0:
            outcome = "partial"
        elif t["status"] == "canceled":
            outcome = "canceled"
        elif t["status"] == "running":
            outcome = "running"
        else:
            outcome = "pending"

        add(
            "deploy",
            t.get("finished_at") or t.get("started_at") or t.get("created_at"),
            status=outcome,
            rng={"start": t.get("started_at") or t.get("created_at"), "end": t.get("finished_at")},
        )

    # 直接用字串排序（ISO8601 字典序 == 時間序）
    evts.sort(key=lambda e: e["at"])
    return evts


def create_draft(draft_action: dict, created_by: int, title: str = "") -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        # 自動補 title
        auto_title = title or _autogen_title(cur, draft_action)

        cur.execute("""
          INSERT INTO forti_drafts (title, draft_action, created_by, status, created_at, updated_at)
          VALUES (%s, %s, %s, %s, NOW(), NOW())
        """, (auto_title, json.dumps(draft_action), created_by, DraftStatus.Pending_Submit.value))
        conn.commit()
        return cur.lastrowid
    finally:
        cur.close(); conn.close()

def get_draft(draft_id: int) -> Optional[dict]:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("""
            SELECT d.*, u.username AS created_by_name
              FROM forti_drafts d
              LEFT JOIN users u ON u.id = d.created_by
             WHERE d.id=%s
        """, (draft_id,))
        row = cur.fetchone()

        if not row:
            return None

        row['created_at']  = _norm_dt(row.get('created_at'))
        row['updated_at']  = _norm_dt(row.get('updated_at'))
        if 'submitted_at' in row:  # 欄位存在就正常化
            row['submitted_at'] = _norm_dt(row.get('submitted_at'))
        if 'approved_at' in row:
            row['approved_at']  = _norm_dt(row.get('approved_at'))
        if 'executed_at' in row:
            row['executed_at']  = _norm_dt(row.get('executed_at'))
        if 'completed_at' in row:
            row['completed_at'] = _norm_dt(row.get('completed_at'))

        # 最新一筆「核准」
        cur.execute("""
            SELECT a.decided_at, u.username AS approver_name
              FROM forti_draft_approvals a
              LEFT JOIN users u ON u.id = a.approver_id
             WHERE a.draft_id=%s AND a.decision='approved'
             ORDER BY a.decided_at DESC LIMIT 1
        """, (draft_id,))
        ap = cur.fetchone()
        if ap:
            row['approved_at'] = _norm_dt(row.get('approved_at') or ap.get('decided_at'))
            row['approved_by_name'] = ap.get('approver_name')

        # 最新一筆「拒絕/取消」
        cur.execute("""
            SELECT a.decided_at, a.comment, u.username AS rejected_by_name
              FROM forti_draft_approvals a
              LEFT JOIN users u ON u.id = a.approver_id
             WHERE a.draft_id=%s AND a.decision='rejected'
             ORDER BY a.decided_at DESC LIMIT 1
        """, (draft_id,))
        rj = cur.fetchone()
        if rj:
            row['rejected_at'] = _norm_dt(rj.get('decided_at'))
            row['rejected_by_name'] = rj.get('rejected_by_name')
            row['reject_reason'] = rj.get('comment')
        
        # 最新一筆「取消」
        cur.execute("""
            SELECT a.decided_at, a.comment, u.username AS canceled_by_name
              FROM forti_draft_approvals a
              LEFT JOIN users u ON u.id = a.approver_id
             WHERE a.draft_id=%s AND a.decision='canceled'
             ORDER BY a.decided_at DESC LIMIT 1
        """, (draft_id,))
        cz = cur.fetchone()
        if cz:
            row['canceled_at'] = _norm_dt(cz.get('decided_at'))
            row['canceled_by_name'] = cz.get('canceled_by_name')
            row['cancel_reason'] = cz.get('comment')

        # 為了前端時間軸：若已提交，就把提交者也帶回（用建立者當 submitted_by）
        if row.get('submitted_at'):
            row['submitted_by_name'] = row.get('created_by_name')

        row["timeline"] = _build_timeline(cur, row)

        return row
    finally:
        cur.close(); conn.close()

def update_draft_status(draft_id: int, status: str) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET status=%s, updated_at=NOW() WHERE id=%s", (status, draft_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def update_draft_check_report(draft_id: int, report: dict) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET check_report=%s, updated_at=NOW() WHERE id=%s",
                    (json.dumps(report or {}), draft_id))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def record_approval(draft_id: int, approver_id: int, decision: str, comment: Optional[str]) -> int:
    """
    forti_draft_approvals.decision: ENUM('approved','rejected','canceled')
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("""
            INSERT INTO forti_draft_approvals (draft_id, approver_id, decision, comment, decided_at)
            VALUES (%s,%s,%s,%s,NOW())
        """, (draft_id, approver_id, decision, comment))
        conn.commit()
        return cur.lastrowid
    finally:
        cur.close(); conn.close()

def list_drafts_for_request_page(
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> Tuple[List[dict], int]:
    """
    提供清單頁使用：
      - 回傳 forti_drafts 基本欄位
      - 回傳建立者名稱 created_by_name（users.username）
      - 回傳最近核准時間 approved_at（forti_draft_approvals 中 decision='approved' 的 MAX(decided_at)）
      - 連帶回傳該草稿最新的一筆 forti_tasks 狀態（若有）
      - 支援狀態與關鍵字過濾（title / id / username）
    """
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # --- where 條件 ---
        where_clauses = []
        params: List = []

        if status:
            where_clauses.append("d.status = %s")
            params.append(status)

        if q:
            like = f"%{q}%"
            where_clauses.append("(d.title LIKE %s OR CAST(d.id AS CHAR) LIKE %s OR u.username LIKE %s)")
            params.extend([like, like, like])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # --- total 計數（只要 join users 就好） ---
        cur.execute(
            f"""
            SELECT COUNT(*) AS c
              FROM forti_drafts d
              LEFT JOIN users u ON u.id = d.created_by
            {where_sql}
            """,
            params
        )
        total = cur.fetchone()["c"]

        # --- 列表查詢 ---
        cur.execute(
            f"""
            SELECT
                d.id,
                d.title,
                d.status,
                d.created_by,
                d.created_at,
                d.updated_at,
                d.submitted_at,
                u.username AS created_by_name,
                COALESCE(
                  (SELECT MAX(a.decided_at)
                     FROM forti_draft_approvals a
                    WHERE a.draft_id = d.id
                       AND a.decision IN ('approved','rejected','canceled')),
                 d.approved_at,
                  (CASE WHEN d.status = 'Canceled' THEN d.updated_at END)
                ) AS decided_at,
                d.check_report,
                d.draft_action,
                t.id     AS task_id,
                t.status AS task_status
            FROM forti_drafts d
            LEFT JOIN users u ON u.id = d.created_by
            LEFT JOIN (
                SELECT t1.*
                  FROM forti_tasks t1
                  JOIN (
                      SELECT draft_id, MAX(id) AS max_id
                        FROM forti_tasks
                       GROUP BY draft_id
                  ) x ON x.draft_id = t1.draft_id AND x.max_id = t1.id
            ) t ON t.draft_id = d.id
            {where_sql}
            ORDER BY d.id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset)
        )
        rows = cur.fetchall()

        # ← 新增：把 datetime 物件或含微秒的字串，統一成 'YYYY-MM-DDTHH:MM:SS'
        for r in rows:
            r['created_at']  = _norm_dt(r.get('created_at'))
            r['updated_at']  = _norm_dt(r.get('updated_at'))
            r['submitted_at']= _norm_dt(r.get('submitted_at'))
            r['decided_at']   = _norm_dt(r.get('decided_at'))

        # --- action_count for UI ---
        for r in rows:
            da = r.get("draft_action")
            try:
                if isinstance(da, str):
                    da_obj = json.loads(da) if da else {}
                else:
                    da_obj = da or {}
                r["action_count"] = len(da_obj.get("action_plan") or [])
            except Exception:
                r["action_count"] = None

        return rows, total
    finally:
        cur.close()
        conn.close()

def delete_draft(draft_id: int) -> int:
    """
    物理刪除一筆草稿；同時清除關聯資料：
      - forti_draft_approvals
      - forti_tasks
    回傳實際刪除的 forti_drafts 行數（0 或 1）
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        # 先刪關聯（若沒有也不會出錯）
        cur.execute("DELETE FROM forti_draft_approvals WHERE draft_id=%s", (draft_id,))
        cur.execute("DELETE FROM forti_tasks WHERE draft_id=%s", (draft_id,))
        # 再刪主檔
        cur.execute("DELETE FROM forti_drafts WHERE id=%s", (draft_id,))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

def update_draft_content(
    draft_id: int,
    *,
    title: str | None = None,
    draft_action: dict | str | None = None,
    reset_check_report: bool = False
) -> int:
    conn = get_db_connection()
    cur = _dc(conn)
    try:
        cur.execute("SELECT status FROM forti_drafts WHERE id=%s", (draft_id,))
        row = cur.fetchone()
        if not row:
            return 0
        if row["status"] not in (
           DraftStatus.Pending_Submit.value,
           DraftStatus.Rejected.value,
           DraftStatus.Verify_Failed.value,
        ):
            return 0

        fields, params = [], []

        # --- 正規化 draft_action 成 JSON 與物件 ---
        da_obj = None
        if draft_action is not None:
            if isinstance(draft_action, str):
                try:
                    da_obj = json.loads(draft_action) if draft_action else {}
                    da_json = draft_action  # 已是合法 JSON
                except Exception:
                    da_obj = draft_action    # 非 JSON 字串就當物件序列化
                    da_json = json.dumps(draft_action)
            else:
                da_obj = draft_action
                da_json = json.dumps(draft_action)

            fields.append("draft_action=%s")
            params.append(da_json)

        # --- 只依 draft_action 進行重算 ---
        if title is not None:
            # 使用者明確給了 title → 直接覆寫
            fields.append("title=%s")
            params.append(title.strip())
        elif draft_action is not None:
            # 沒帶 title 但有帶 draft_action → 依新內容自動重算
            new_title = _autogen_title(cur, da_obj or {})
            fields.append("title=%s")
            params.append(new_title)

        if reset_check_report:
            fields.append("check_report=%s")
            params.append(json.dumps({}))

        if not fields:
            return 0

        fields.append("updated_at=NOW()")
        sql = "UPDATE forti_drafts SET " + ", ".join(fields) + " WHERE id=%s"
        params.append(draft_id)
        cur.execute(sql, tuple(params))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close()
        conn.close()

def refresh_check_report_by_id(draft_id: int) -> dict:
    """
    讀取 forti_drafts.draft_action → compute_draft_check_report()
    → update_draft_check_report()，最後回傳 report。
    """
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("SELECT draft_action FROM forti_drafts WHERE id=%s", (draft_id,))
        row = cur.fetchone() or {}
        da = row.get("draft_action")

        # 兼容 str / dict
        try:
            if isinstance(da, str):
                da = json.loads(da) if da else {}
        except Exception:
            da = {}

        report = compute_draft_check_report(da or {})
        update_draft_check_report(draft_id, report)
        return report
    finally:
        cur.close(); conn.close()

# --- timestamps helpers ---
def mark_submitted(draft_id: int) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET submitted_at=COALESCE(submitted_at, NOW()), updated_at=NOW() WHERE id=%s", (draft_id,))
        conn.commit(); return cur.rowcount
    finally:
        cur.close(); conn.close()

def mark_approved(draft_id: int) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET approved_at=COALESCE(approved_at, NOW()), updated_at=NOW() WHERE id=%s", (draft_id,))
        conn.commit(); return cur.rowcount
    finally:
        cur.close(); conn.close()

def mark_executed(draft_id: int) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET executed_at=COALESCE(executed_at, NOW()), updated_at=NOW() WHERE id=%s", (draft_id,))
        conn.commit(); return cur.rowcount
    finally:
        cur.close(); conn.close()

def mark_completed(draft_id: int) -> int:
    conn = get_db_connection(); cur = _dc(conn)
    try:
        cur.execute("UPDATE forti_drafts SET completed_at=COALESCE(completed_at, NOW()), updated_at=NOW() WHERE id=%s", (draft_id,))
        conn.commit(); return cur.rowcount
    finally:
        cur.close(); conn.close()

def get_plan_results_for_draft(draft_id: int) -> List[dict]:
    """
    取得該 draft 最新 forti_task_action_results（依 action_order 排序）
    （使用現有 MySQL 連線，無需 SQLAlchemy）
    """
    conn = get_db_connection()
    cur = _dc(conn)
    try:
        # 找最新一筆 task（同一 draft 可能會多次佈署）
        cur.execute(
            "SELECT id FROM forti_tasks WHERE draft_id=%s "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (draft_id,)
        )
        row = cur.fetchone()
        if not row:
            return []
        task_id = row["id"] if isinstance(row, dict) else row[0]

        # 取 action results（依 action_order、id 排序）
        cur.execute("""
            SELECT action_id, kind, action_type, device_id, vdom, resource_id, status, action_order
            FROM forti_task_action_results
            WHERE task_id=%s
            ORDER BY action_order ASC, id ASC
        """, (task_id,))

        items = []
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            if isinstance(r, dict):
                items.append(r)
            else:
                items.append(dict(zip(cols, r)))
        return items
    finally:
        cur.close()
        conn.close()

