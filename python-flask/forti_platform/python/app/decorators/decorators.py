# app/decorators/decorators.py
from functools import wraps
from flask import session, redirect, url_for, flash, request, jsonify, g

# ——相容保留：單一角色 -> 權限（其他地方若還有用得上）——
from app.db.mysql import get_db_connection
def get_role_permissions(role_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.permission_key
        FROM permissions p
        JOIN role_permissions rp ON p.id = rp.permission_id
        JOIN roles r ON r.id = rp.role_id
        WHERE r.role_name = %s
    """, (role_name,))
    permissions = [row[0] for row in cursor.fetchall()]
    cursor.close(); conn.close()
    return permissions


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 以 user_id/username 任一為準；較新流程會設置 user_id
        if "user_id" not in session and "username" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return wrapper


def permission_required(*required_permissions):
    """
    檢查使用者是否擁有 required_permissions（可傳 1~N 個）。
    以 session["permissions"] 為主（登入時計算好的多角色聯集）。
    若 session 沒有，則用 session["roles"] 從 DB 回填一次後再判斷。
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            permissions = session.get("permissions")

            if permissions is None:
                # 後備：用多角色回填一次
                roles = session.get("roles") or []
                if roles:
                    # 延遲載入，避免模組循環
                    from app.auth.db.auth_handler import get_permissions_for_roles
                    permissions = get_permissions_for_roles(roles)
                    session["permissions"] = permissions  # 快取到 session
                else:
                    permissions = []

            ok = all(p in permissions for p in required_permissions) if required_permissions else True
            if not ok:
                # AJAX / API 回 403；一般頁面導回首頁並提示
                if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return jsonify({"error": "permission denied"}), 403
                flash("Permission denied", "danger")
                return redirect(request.referrer or url_for("main.index"))

            return f(*args, **kwargs)
        return wrapper
    return decorator

def login_or_callback_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 已登入就放行
        if "user_id" in session or "username" in session:
            return f(*args, **kwargs)

        # 只接受 Header: X-Auth-Token
        token = request.headers.get("X-Auth-Token")
        draft_id = kwargs.get("draft_id") or (getattr(request, "view_args", {}) or {}).get("draft_id")

        if token and draft_id:
            try:
                from app.fortigate.policy.db.policy_handler import verify_runner_token_by_draft
                if verify_runner_token_by_draft(int(draft_id), str(token)):
                    from flask import g
                    g.user = getattr(g, "user", None) or type("RunnerUser", (), {"id": 0, "username": "runner"})()
                    return f(*args, **kwargs)
            except Exception:
                pass

        # API 一律 JSON 401；僅網頁請求才導向登入
        if request.is_json or request.accept_mimetypes.accept_json or request.path.startswith("/fortigate/"):
            return jsonify({"ok": False, "error": "login required"}), 401
        return redirect(url_for("auth.login"))
    return wrapper
