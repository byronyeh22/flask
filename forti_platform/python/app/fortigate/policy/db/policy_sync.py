# app/fortigate/policy/db/policy_sync.py
from __future__ import annotations
from typing import Any, Dict, List
import json
import requests

from app.db.mysql import get_db_connection

# 如需靜音 verify_ssl=False 的警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========= 工具 =========

def _row_list_names(objs: Any) -> List[str]:
    if not objs:
        return []
    if isinstance(objs, list):
        out: List[str] = []
        for o in objs:
            if isinstance(o, dict) and o.get("name"):
                out.append(str(o["name"]))
            elif isinstance(o, str) and o:
                out.append(o)
        return out
    if isinstance(objs, dict) and objs.get("name"):
        return [str(objs["name"])]
    if isinstance(objs, str) and objs:
        return [objs]
    return []

def _json_if_any(seq: List[str]) -> str | None:
    return json.dumps(seq, ensure_ascii=False) if seq else None

def _as_bool(v: Any) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return v != 0
    return str(v or "").strip().lower() in {"1","true","yes","on","enable","enabled"}

def _load_device(device_id: int) -> Dict[str, Any] | None:
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, name, host, port, api_token, verify_ssl FROM forti_devices WHERE id=%s",
        (device_id,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def _fg_list_policies(base_url: str, token: str, vdom: str, verify_ssl: bool) -> List[Dict[str, Any]]:
    url = f"{base_url}/api/v2/cmdb/firewall/policy"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {"vdom": vdom}
    r = requests.get(url, headers=headers, params=params, timeout=30, verify=verify_ssl)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        res = data.get("results") or data.get("data") or data.get("list")
        return res if isinstance(res, list) else []
    if isinstance(data, list):
        return data
    return []

def _fg_get(base_url: str, token: str, path: str, params: Dict[str, Any], verify_ssl: bool) -> List[Dict[str, Any]]:
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, params=params, timeout=30, verify=verify_ssl)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        val = data.get("results") or data.get("data") or data.get("list")
        return val if isinstance(val, list) else []
    if isinstance(data, list):
        return data
    return []

def _uniq_sorted(xs: List[str]) -> List[str]:
    seen = set(); out: List[str] = []
    for s in xs:
        if s and s not in seen:
            seen.add(s); out.append(s)
    return sorted(out, key=lambda s: s.lower())

def _collect_sdwan_zone_names(base_url: str, token: str, params: Dict[str, Any],
                              verify_ssl: bool, headers: Dict[str, str]) -> List[str]:
    names: List[str] = []
    try:
        url = f"{base_url}/api/v2/cmdb/system/sdwan"
        r = requests.get(url, headers=headers, params=params, timeout=30, verify=verify_ssl)
        r.raise_for_status()
        raw = r.json()
        def _extract_from_obj(obj: Dict[str, Any]):
            zs = obj.get("zone") or obj.get("zones")
            if isinstance(zs, list):
                for z in zs:
                    nm = (z or {}).get("name")
                    if nm:
                        names.append(str(nm))
        if isinstance(raw, list):
            for it in raw:
                if isinstance(it, dict):
                    _extract_from_obj(it)
        elif isinstance(raw, dict):
            arr = raw.get("results") or raw.get("data")
            if isinstance(arr, list):
                for it in arr:
                    if isinstance(it, dict):
                        _extract_from_obj(it)
            else:
                _extract_from_obj(raw)
    except Exception:
        pass

    if not names:
        try:
            zs = _fg_get(base_url, token, "/api/v2/cmdb/system/sdwan/zone", params, verify_ssl)
            for z in zs:
                nm = (z or {}).get("name")
                if nm:
                    names.append(str(nm))
        except Exception:
            pass

    if "virtual-wan-link" not in names:
        names.append("virtual-wan-link")
    return _uniq_sorted([n for n in names if n])

def _fetch_interface_sources(base_url: str, token: str, vdom: str, verify: bool) -> Dict[str, List[str]]:
    params = {"vdom": vdom}
    ifs = _fg_get(base_url, token, "/api/v2/cmdb/system/interface", params, verify)
    interface_names = [x.get("name") for x in ifs if isinstance(x, dict) and x.get("name")]
    zones = _fg_get(base_url, token, "/api/v2/cmdb/system/zone", params, verify)
    zone_names = [x.get("name") for x in zones if isinstance(x, dict) and x.get("name")]
    vsw = _fg_get(base_url, token, "/api/v2/cmdb/system/virtual-switch", params, verify)
    vsw_names = [x.get("name") for x in vsw if isinstance(x, dict) and x.get("name")]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    sdwan_zone_names = _collect_sdwan_zone_names(base_url, token, params, verify, headers)
    return {
        "interface":      _uniq_sorted([n for n in interface_names if n]),
        "zone":           _uniq_sorted([n for n in zone_names if n]),
        "virtual-switch": _uniq_sorted([n for n in vsw_names if n]),
        "sdwan-zone":     _uniq_sorted([n for n in sdwan_zone_names if n]),
    }

def _extract_webfilter(item: Dict[str, Any]) -> str | None:
    wf = item.get("webfilter-profile") or item.get("webfilter_profile") or item.get("web_filter")
    if isinstance(wf, dict) and wf.get("name"):
        return str(wf["name"]).strip() or None
    if isinstance(wf, str) and wf.strip():
        return wf.strip()
    return None

# ========= 差異比對輔助 =========

def _norm_str(s): 
    return (str(s) if s is not None else "").strip()

def _canon_json_list(j: str | None) -> str | None:
    """
    把 JSON 陣列欄位做 canonical 化（排序、去重、lower 比較），
    避免順序不同造成誤判。
    """
    if not j:
        return None
    try:
        arr = json.loads(j)
        if not isinstance(arr, list):
            return None
        seen = set(); out = []
        for x in arr:
            sx = _norm_str(x)
            key = sx.lower()
            if key and key not in seen:
                seen.add(key); out.append(sx)
        out.sort(key=str.lower)
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return j  # 解析失敗就原樣比

def _row_tuple_for_compare(row: dict) -> tuple:
    """
    把一筆『DB 或 FortiGate 轉出的 row 值』做成可比較的 tuple。
    欄位順序要與 INSERT/UPDATE 欄位一致（除了 device_id/vdom/fg_policy_id）。
    """
    return (
        int(row.get("seq_num")) if row.get("seq_num") is not None else None,
        _norm_str(row.get("name")),
        _norm_str(row.get("action")),
        _norm_str(row.get("status")),
        _norm_str(row.get("schedule")),
        1 if _as_bool(row.get("nat")) else 0,
        _norm_str(row.get("comments")),
        _canon_json_list(row.get("src_addrs")),
        _canon_json_list(row.get("dst_addrs")),
        _canon_json_list(row.get("services")),
        _canon_json_list(row.get("src_intfs")),
        _canon_json_list(row.get("dst_intfs")),
        _norm_str(row.get("web_filter")),
    )

def _make_db_row_from_fg_item(device_id: int, vdom: str, it: dict) -> dict:
    """
    沿用原本欄位萃取與正規化，輸出一筆『準備寫 DB』的 dict。
    """
    policy_id = it.get("policyid")
    name = it.get("name") or ""
    action = it.get("action") or ""
    status = it.get("status") or ""
    schedule = it.get("schedule") or ""
    nat = 1 if _as_bool(it.get("nat")) else 0
    comments = it.get("comments") or it.get("comment") or ""

    return {
        "device_id": device_id,
        "vdom": vdom,
        "fg_policy_id": policy_id,
        "seq_num": int(policy_id) if policy_id is not None else None,
        "name": name,
        "action": action,
        "status": status,
        "schedule": schedule,
        "nat": nat,
        "comments": comments,
        "src_addrs": _json_if_any(_row_list_names(it.get("srcaddr"))),
        "dst_addrs": _json_if_any(_row_list_names(it.get("dstaddr"))),
        "services":  _json_if_any(_row_list_names(it.get("service"))),
        "src_intfs": _json_if_any(_row_list_names(it.get("srcintf"))),
        "dst_intfs": _json_if_any(_row_list_names(it.get("dstintf"))),
        "web_filter": _extract_webfilter(it),
    }

# ========= 同步（僅差異更新；修正交易啟動） =========

def sync_policies_from_fortigate(device_id: int, vdom: str) -> int:
    """
    僅差異更新：
    - 新出現：INSERT
    - 有變更：UPDATE
    - FortiGate 不在了：DELETE
    回傳『實際變動筆數』（insert + update + delete）。
    """
    dev = _load_device(device_id)
    if not dev:
        raise ValueError("device not found")

    host = dev["host"]
    port = int(dev.get("port") or 443)
    base_url = f"https://{host}:{port}"
    token = str(dev.get("api_token") or "")
    verify_ssl = bool(dev.get("verify_ssl", 1))

    # 1) 取 FortiGate 現況
    fg_items = _fg_list_policies(base_url, token, vdom, verify_ssl)

    # 2) 轉成 {fg_policy_id: row_dict}
    fg_map: dict[int, dict] = {}
    for it in fg_items:
        pid = it.get("policyid")
        if pid is None:
            continue
        row = _make_db_row_from_fg_item(device_id, vdom, it)
        fg_map[int(pid)] = row

    # 3) 讀 DB 現況（同一 device/vdom）
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT device_id, vdom, fg_policy_id, seq_num, name, action, status, schedule, nat, comments,
               src_addrs, dst_addrs, services, src_intfs, dst_intfs, web_filter
          FROM forti_policies_current
         WHERE device_id=%s AND vdom=%s
    """, (device_id, vdom))
    db_rows = cur.fetchall()
    cur.close()

    db_map: dict[int, dict] = {}
    for r in db_rows:
        if r["fg_policy_id"] is None:
            continue
        r["src_addrs"] = _canon_json_list(r["src_addrs"])
        r["dst_addrs"] = _canon_json_list(r["dst_addrs"])
        r["services"]  = _canon_json_list(r["services"])
        r["src_intfs"] = _canon_json_list(r["src_intfs"])
        r["dst_intfs"] = _canon_json_list(r["dst_intfs"])
        db_map[int(r["fg_policy_id"])] = r

    to_insert: list[tuple] = []
    to_update: list[tuple] = []
    to_delete: list[int]   = []

    # 4) 找出要 INSERT / UPDATE
    for pid, new_row in fg_map.items():
        old_row = db_map.get(pid)
        if not old_row:
            to_insert.append((
                device_id, vdom, pid,
                new_row["seq_num"], new_row["name"], new_row["action"], new_row["status"],
                new_row["schedule"], new_row["nat"], new_row["comments"],
                _canon_json_list(new_row["src_addrs"]),
                _canon_json_list(new_row["dst_addrs"]),
                _canon_json_list(new_row["services"]),
                _canon_json_list(new_row["src_intfs"]),
                _canon_json_list(new_row["dst_intfs"]),
                new_row["web_filter"],
            ))
        else:
            new_tuple = _row_tuple_for_compare(new_row)
            old_tuple = _row_tuple_for_compare(old_row)
            if new_tuple != old_tuple:
                to_update.append((
                    new_row["seq_num"], new_row["name"], new_row["action"], new_row["status"],
                    new_row["schedule"], new_row["nat"], new_row["comments"],
                    _canon_json_list(new_row["src_addrs"]),
                    _canon_json_list(new_row["dst_addrs"]),
                    _canon_json_list(new_row["services"]),
                    _canon_json_list(new_row["src_intfs"]),
                    _canon_json_list(new_row["dst_intfs"]),
                    new_row["web_filter"],
                    device_id, vdom, pid
                ))

    # 5) 找出要 DELETE（DB 有但 FG 沒了）
    for pid in db_map.keys():
        if pid not in fg_map:
            to_delete.append(pid)

    # 6) 執行變更（單一交易；不呼叫 start_transaction 避免已在交易中）
    changed = 0
    original_autocommit = getattr(conn, "autocommit", True)
    try:
        conn.autocommit = False  # 進入交易狀態
        cur2 = conn.cursor()

        if to_insert:
            cur2.executemany("""
                INSERT INTO forti_policies_current
                  (device_id, vdom, fg_policy_id, seq_num, name, action, status, schedule, nat, comments,
                   src_addrs, dst_addrs, services, src_intfs, dst_intfs, web_filter)
                VALUES
                  (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, to_insert)
            changed += cur2.rowcount

        if to_update:
            cur2.executemany("""
                UPDATE forti_policies_current
                   SET seq_num=%s, name=%s, action=%s, status=%s, schedule=%s, nat=%s, comments=%s,
                       src_addrs=%s, dst_addrs=%s, services=%s, src_intfs=%s, dst_intfs=%s, web_filter=%s
                 WHERE device_id=%s AND vdom=%s AND fg_policy_id=%s
            """, to_update)
            changed += cur2.rowcount

        if to_delete:
            cur2.executemany("""
                DELETE FROM forti_policies_current
                 WHERE device_id=%s AND vdom=%s AND fg_policy_id=%s
            """, [ (device_id, vdom, pid) for pid in to_delete ])
            changed += cur2.rowcount

        conn.commit()
        cur2.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.autocommit = original_autocommit
        except Exception:
            pass
        conn.close()

    return changed

# ========= 物件/下拉與 typed meta =========

def get_forti_objects(device_id: int, vdom: str) -> Dict[str, List[str]]:
    dev = _load_device(device_id)
    if not dev:
        raise ValueError("device not found")

    host = dev["host"]; port = int(dev.get("port") or 443)
    base_url = f"https://{host}:{port}"
    token = str(dev.get("api_token") or "")
    verify = bool(dev.get("verify_ssl", 1))
    params = {"vdom": vdom}
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    addr = _fg_get(base_url, token, "/api/v2/cmdb/firewall/address", params, verify)
    agrp = _fg_get(base_url, token, "/api/v2/cmdb/firewall/addrgrp",  params, verify)
    addresses = [x.get("name") for x in addr if x.get("name")] + [x.get("name") for x in agrp if x.get("name")]
    if "all" not in { (s or "").lower() for s in addresses }:
        addresses.append("all")

    svc_c = _fg_get(base_url, token, "/api/v2/cmdb/firewall.service/custom", params, verify)
    svc_g = _fg_get(base_url, token, "/api/v2/cmdb/firewall.service/group",  params, verify)
    services = [x.get("name") for x in svc_c if x.get("name")] + [x.get("name") for x in svc_g if x.get("name")]
    builtin_services = ["ALL", "HTTP", "HTTPS", "SSH", "DNS", "PING", "ALL_ICMP"]
    for b in builtin_services:
        if b not in services:
            services.append(b)

    interfaces: List[str] = []
    ifs = _fg_get(base_url, token, "/api/v2/cmdb/system/interface", params, verify)
    interfaces += [x.get("name") for x in ifs if isinstance(x, dict) and x.get("name")]
    zones = _fg_get(base_url, token, "/api/v2/cmdb/system/zone", params, verify)
    interfaces += [x.get("name") for x in zones if isinstance(x, dict) and x.get("name")]
    vsw = _fg_get(base_url, token, "/api/v2/cmdb/system/virtual-switch", params, verify)
    interfaces += [x.get("name") for x in vsw if isinstance(x, dict) and x.get("name")]
    sdwan_zone_names = _collect_sdwan_zone_names(base_url, token, params, verify, headers)
    interfaces += sdwan_zone_names
    if "any" not in { (s or "").lower() for s in interfaces }:
        interfaces.append("any")

    wf = _fg_get(base_url, token, "/api/v2/cmdb/webfilter/profile", params, verify)
    webfilters = [x.get("name") for x in wf if x.get("name")]

    return {
        "addresses":  _uniq_sorted([n for n in addresses if n]),
        "services":   _uniq_sorted([n for n in services if n]),
        "interfaces": _uniq_sorted([n for n in interfaces if n]),
        "webfilters": _uniq_sorted([n for n in webfilters if n]),
    }

def get_forti_services_meta(device_id: int, vdom: str) -> Dict[str, Any]:
    dev = _load_device(device_id)
    if not dev:
        raise ValueError("device not found")
    base_url = f"https://{dev['host']}:{int(dev.get('port') or 443)}"
    token = str(dev.get("api_token") or "")
    verify = bool(dev.get("verify_ssl", 1))
    params = {"vdom": vdom}

    svc_c = _fg_get(base_url, token, "/api/v2/cmdb/firewall.service/custom", params, verify)
    svc_g = _fg_get(base_url, token, "/api/v2/cmdb/firewall.service/group",  params, verify)

    def _norm_str(x): return (str(x or "")).strip()
    def _lc(x): return _norm_str(x).lower()

    def _infer_by_protocol(s: Dict[str, Any]) -> str:
        tcp = _norm_str(s.get("tcp-portrange") or s.get("tcp_portrange"))
        udp = _norm_str(s.get("udp-portrange") or s.get("udp_portrange"))
        if tcp and udp: return "Protocol: TCP+UDP"
        if tcp:         return "Protocol: TCP"
        if udp:         return "Protocol: UDP"
        if s.get("icmpcode") is not None or s.get("icmptype") is not None: return "Protocol: ICMP"
        prot = s.get("protocol")
        try:
            pn = int(prot)
            if pn == 6:   return "Protocol: TCP"
            if pn == 17:  return "Protocol: UDP"
            if pn == 1:   return "Protocol: ICMP"
            if pn == 132: return "Protocol: SCTP"
            return "Protocol: IP"
        except Exception:
            if isinstance(prot, str) and prot.strip():
                p = _lc(prot)
                if p in {"tcp"}:  return "Protocol: TCP"
                if p in {"udp"}:  return "Protocol: UDP"
                if p in {"icmp"}: return "Protocol: ICMP"
                if p in {"sctp"}: return "Protocol: SCTP"
        return "Uncategorized"

    typed: List[Dict[str, str]] = []
    for s in (svc_c or []):
        name = s.get("name")
        if not name:
            continue
        cat = _norm_str(s.get("category")) or _infer_by_protocol(s)
        typed.append({"name": str(name), "category": cat})

    for g in (svc_g or []):
        name = g.get("name")
        if name:
            typed.append({"name": str(name), "category": "Service Group"})

    builtin_fallback = ["ALL", "HTTP", "HTTPS", "SSH", "DNS", "PING", "ALL_ICMP"]
    have = {t["name"] for t in typed}
    for bn in builtin_fallback:
        if bn not in have:
            typed.append({"name": bn, "category": "Built-in"})

    names_union = _uniq_sorted([t["name"] for t in typed])
    typed = sorted(typed, key=lambda x: (x["category"].lower(), x["name"].lower()))
    return {"list": names_union, "typed": typed}

def get_forti_addresses_meta(device_id: int, vdom: str) -> Dict[str, Any]:
    dev = _load_device(device_id)
    if not dev:
        raise ValueError("device not found")
    base_url = f"https://{dev['host']}:{int(dev.get('port') or 443)}"
    token = str(dev.get("api_token") or "")
    verify = bool(dev.get("verify_ssl", 1))
    params = {"vdom": vdom}

    addr = _fg_get(base_url, token, "/api/v2/cmdb/firewall/address", params, verify)
    agrp = _fg_get(base_url, token, "/api/v2/cmdb/firewall/addrgrp",  params, verify)

    typed: List[Dict[str, str]] = []
    for a in addr:
        n = (a or {}).get("name")
        if not n:
            continue
        atype = str((a or {}).get("type") or "").strip().lower() or "unknown"
        typed.append({"name": str(n), "type": atype})

    for g in agrp:
        n = (g or {}).get("name")
        if n:
            typed.append({"name": str(n), "type": "group"})

    if not any(t["name"].lower() == "all" for t in typed):
        typed.append({"name": "all", "type": "builtin"})

    names_union = _uniq_sorted([t["name"] for t in typed])
    typed = sorted(typed, key=lambda x: (x["type"], x["name"].lower()))
    return {"list": names_union, "typed": typed}

def get_forti_interfaces_meta(device_id: int, vdom: str) -> Dict[str, Any]:
    dev = _load_device(device_id)
    if not dev:
        raise ValueError("device not found")
    base_url = f"https://{dev['host']}:{int(dev.get('port') or 443)}"
    token = str(dev.get("api_token") or "")
    verify = bool(dev.get("verify_ssl", 1))

    srcs = _fetch_interface_sources(base_url, token, vdom, verify)

    typed: List[Dict[str, str]] = []
    for t, names in srcs.items():
        for n in names:
            typed.append({"name": n, "type": t})
    typed.append({"name": "any", "type": "meta"})

    names_union = _uniq_sorted([it["name"] for it in typed])
    typed = sorted(typed, key=lambda x: (x["type"], x["name"].lower()))
    return {"list": names_union, "typed": typed}

def query_policies_current(
    device_id: int,
    vdom: str,
    action: str = "",
    status: str = "",
    seq_min: int | None = None,
    seq_max: int | None = None,
    name: str = "",
) -> list[dict]:
    sql = [
        "SELECT device_id, vdom, fg_policy_id, seq_num, name, action, status, schedule, nat, comments,",
        "       src_addrs, dst_addrs, services, src_intfs, dst_intfs, web_filter",
        "  FROM forti_policies_current",
        " WHERE device_id=%s AND vdom=%s"
    ]
    args: list = [device_id, vdom]
    if action:
        sql.append(" AND action=%s"); args.append(action)
    if status:
        sql.append(" AND status=%s"); args.append(status)
    if seq_min is not None:
        sql.append(" AND seq_num >= %s"); args.append(seq_min)
    if seq_max is not None:
        sql.append(" AND seq_num <= %s"); args.append(seq_max)
    if name:
        sql.append(" AND name LIKE %s"); args.append(f"%{name}%")
    sql.append(" ORDER BY seq_num ASC")

    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute(" ".join(sql), tuple(args))
    items = cur.fetchall()
    cur.close(); conn.close()
    return items

# 新增：把 objects + typed meta 打包（供 API 在 verbose=1 時使用）
def get_forti_objects_with_meta(device_id: int, vdom: str) -> Dict[str, Any]:
    base = get_forti_objects(device_id, vdom)
    base["services_meta"]   = get_forti_services_meta(device_id, vdom)
    base["addresses_meta"]  = get_forti_addresses_meta(device_id, vdom)
    base["interfaces_meta"] = get_forti_interfaces_meta(device_id, vdom)
    return base

