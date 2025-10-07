from flask import render_template, request, redirect, url_for, flash, session, jsonify
from . import auth_bp

from app.auth.db.auth_handler import (
    get_user_by_username,          # 原 get_user_db
    verify_password,
    update_user_password_db,
    get_user_roles,
    get_permissions_for_roles,
    ensure_ad_user_with_roles,
)
from app.decorators.decorators import login_required
from app.utils.utils import log_operation

# === AD 設定 ===
from ldap3 import Server, Connection, ALL
import re

AD_SERVER = "172.26.1.20"      # 依你的環境調整
AD_DOMAIN = "sanbox888.tw"     # 可用 email 直接登入；若傳 sAMAccountName 會自動加 domain
AD_BASE_DN = "DC=sanbox888,DC=tw"  # 依你的實際 Base DN

def _ad_memberof_to_cns(memberof):
    """memberOf 轉 CN 名稱清單（去重）。"""
    if not memberof:
        return []
    lines = memberof if isinstance(memberof, (list, tuple)) else [memberof]
    cns, seen = [], set()
    for dn in lines:
        m = re.search(r"CN=([^,]+)", str(dn), re.IGNORECASE)
        if m:
            cn = m.group(1)
            if cn not in seen:
                seen.add(cn)
                cns.append(cn)
    return cns

def authenticate_ad_and_profile(username: str, password: str):
    """
    綁定 AD 並取必要屬性。成功回 (True, profile:dict)；失敗 (False, None)。
    """
    try:
        user_dn = username if "@" in username else f"{username}@{AD_DOMAIN}"
        server = Server(AD_SERVER, get_info=ALL)
        conn = Connection(server, user=user_dn, password=password, auto_bind=True)

        search_filter = f"(userPrincipalName={username})" if "@" in username else f"(sAMAccountName={username})"
        conn.search(
            search_base=AD_BASE_DN,
            search_filter=search_filter,
            attributes=["displayName", "memberOf", "userAccountControl"],
            size_limit=1,
        )
        if not conn.entries:
            return False, None

        e = conn.entries[0]
        profile = {
            "displayName": str(e.displayName) if "displayName" in e else "",
            "memberOf": list(e.memberOf) if "memberOf" in e else [],
            "userAccountControl": int(e.userAccountControl.value) if "userAccountControl" in e else None,
        }
        return True, profile
    except Exception:
        return False, None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        auth_mode = request.form.get("auth_mode", "").strip()  # 'local' or 'ad'
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password or auth_mode not in ("local", "ad"):
            flash("Please enter username and password, and choose a sign-in mode.", "danger")
            log_operation(username or "unknown", "LOGIN_FAIL", "Missing credentials or mode")
            return render_template("login.html", error="Missing credentials or mode")

        # ---- Local：只做 DB 驗證 ----
        if auth_mode == "local":
            user = get_user_by_username(username)
            if user and verify_password(password, user.get("password_hash")):
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                roles = get_user_roles(user["id"])
                session["roles"] = roles
                session["permissions"] = get_permissions_for_roles(roles)
                session["auth_source"] = "local"

                log_operation(username, "LOGIN", "Local login successful")
                return redirect(url_for("main.index"))

            flash("Invalid local credentials", "danger")
            log_operation(username, "LOGIN_FAIL", "Local login failed")
            return render_template("login.html")

        # ---- AD：只做 AD 驗證 ----
        ok, profile = authenticate_ad_and_profile(username, password)
        if not ok:
            flash("Active Directory sign-in failed", "danger")
            log_operation(username, "LOGIN_FAIL", "AD login failed")
            return render_template("login.html")

        # 可選：停用帳號阻擋 (UAC 0x2)
        uac = profile.get("userAccountControl")
        if uac is not None and (uac & 0x2) == 0x2:
            flash("Your AD account is disabled. Please contact IT.", "danger")
            log_operation(username, "LOGIN_FAIL", f"AD account disabled (UAC={uac})")
            return render_template("login.html", error="AD account disabled")

        # 取群組 CN；映射為角色清單
        group_cns = _ad_memberof_to_cns(profile.get("memberOf", []))

        # 方式一：直接將群組 CN 當角色名（最省事）
        roles_from_ad = group_cns or ["ad_user"]

        # 方式二（可選）：若你有 ad_group_role_map 映射表，改為：
        # roles_from_ad = map_ad_groups_to_roles(group_cns)

        user = ensure_ad_user_with_roles(username, roles_from_ad)

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        roles = get_user_roles(user["id"])
        session["roles"] = roles
        session["ad_groups"] = group_cns
        session["permissions"] = get_permissions_for_roles(roles)
        session["auth_source"] = "ad"

        log_operation(username, "LOGIN", f"AD login successful; roles={roles} via groups={group_cns}")
        return redirect(url_for("main.index"))

    # GET
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    user = session.get("username")
    session.clear()
    if user:
        log_operation(user, "LOGOUT", "Logout successful")
    return redirect(url_for("auth.login"))


@auth_bp.route('/change_password', methods=['POST'])
@login_required
def change_password():
    username = session.get('username')
    old_pw = request.form['old_password']
    new_pw = request.form['new_password']
    confirm_pw = request.form['confirm_password']

    if new_pw != confirm_pw:
        msg = "New passwords do not match"
        status = "error"
    elif not update_user_password_db(username, old_pw, new_pw):
        msg = "Incorrect current password"
        status = "error"
    else:
        msg = "Password updated successfully"
        status = "success"

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({'status': status, 'message': msg})

    flash(msg, "success" if status == "success" else "danger")
    return redirect(url_for('auth.change_password'))

