from app.db.mysql import get_db_connection
import bcrypt
from typing import Optional, Dict, Any, Iterable, List

# =========================
# 密碼 / 使用者
# =========================
def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    cur.close(); conn.close()
    return user

# 舊名稱相容
get_user_db = get_user_by_username

def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    if not hashed_password:
        return False
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def update_user_password_db(username: str, old_password: str, new_password: str) -> bool:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (username,))
    row = cur.fetchone()
    if not row or not verify_password(old_password, row.get("password_hash")):
        cur.close(); conn.close()
        return False
    new_hash = hash_password(new_password)
    cur2 = conn.cursor()
    cur2.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, row["id"]))
    conn.commit()
    cur2.close(); cur.close(); conn.close()
    return True


# =========================
# 角色 / 權限（多角色）
# =========================
def get_user_roles(user_id: int) -> List[str]:
    """回傳使用者擁有的角色名稱清單"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.role_name
        FROM user_roles ur
        JOIN roles r ON r.id = ur.role_id
        WHERE ur.user_id = %s
    """, (user_id,))
    roles = [row[0] for row in cur.fetchall()]
    cur.close(); conn.close()
    return roles

def set_user_roles(user_id: int, role_names: Iterable[str]) -> None:
    """覆蓋同步 user_roles：不存在就加，多餘就刪（以角色名稱對應 roles.id）。"""
    names = list(dict.fromkeys(role_names)) or []
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 先把名稱對應成 id（若沒有會自動建立）
        if names:
            fmt = ",".join(["%s"] * len(names))
            cur.execute(f"SELECT id, role_name FROM roles WHERE role_name IN ({fmt})", names)
            name2id = {r[1]: r[0] for r in cur.fetchall()}

            missing = [n for n in names if n not in name2id]
            for n in missing:
                cur.execute("INSERT IGNORE INTO roles (role_name) VALUES (%s)", (n,))
            if missing:
                fmt2 = ",".join(["%s"] * len(names))
                cur.execute(f"SELECT id, role_name FROM roles WHERE role_name IN ({fmt2})", names)
                name2id = {r[1]: r[0] for r in cur.fetchall()}
        else:
            name2id = {}

        cur.execute("SELECT role_id FROM user_roles WHERE user_id=%s", (user_id,))
        existing = {r[0] for r in cur.fetchall()}
        target = {name2id[n] for n in names} if names else set()

        to_add = target - existing
        to_del = existing - target

        for rid in to_add:
            cur.execute("INSERT IGNORE INTO user_roles (user_id, role_id) VALUES (%s,%s)", (user_id, rid))
        for rid in to_del:
            cur.execute("DELETE FROM user_roles WHERE user_id=%s AND role_id=%s", (user_id, rid))

        conn.commit()
    finally:
        cur.close(); conn.close()

def get_permissions_for_roles(role_names: Iterable[str]) -> List[str]:
    """多角色 → 權限聯集"""
    names = list(role_names)
    if not names:
        return []
    conn = get_db_connection()
    cur = conn.cursor()
    fmt = ",".join(["%s"] * len(names))
    cur.execute(f"""
        SELECT DISTINCT p.permission_key
        FROM permissions p
        JOIN role_permissions rp ON p.id = rp.permission_id
        JOIN roles r ON r.id = rp.role_id
        WHERE r.role_name IN ({fmt})
    """, names)
    perms = [row[0] for row in cur.fetchall()]
    cur.close(); conn.close()
    return perms

# 舊函式相容（單角色情境仍可用）
def get_role_permissions(role_name: str) -> List[str]:
    return get_permissions_for_roles([role_name])


# =========================
# AD 相關 helper
# =========================
def ensure_ad_user_with_roles(username: str, roles: Iterable[str]) -> Dict[str, Any]:
    """
    確保 AD 使用者存在（不存密碼），並同步多角色到 user_roles。回傳最新 user dict。
    """
    roles = list(dict.fromkeys(roles)) or ["ad_user"]

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # 1) 取得或建立使用者
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user:
            cur2 = conn.cursor()
            cur2.execute(
                "INSERT INTO users (username, password_hash, auth_source) VALUES (%s,%s,%s)",
                (username, "", "ad"),
            )
            conn.commit()
            cur2.close()
            cur.execute("SELECT * FROM users WHERE username=%s", (username,))
            user = cur.fetchone()
        else:
            if user.get("auth_source") != "ad":
                cur2 = conn.cursor()
                cur2.execute("UPDATE users SET auth_source='ad' WHERE id=%s", (user["id"],))
                conn.commit()
                cur2.close()

        # 2) 同步多角色
        set_user_roles(user["id"], roles)

        # 3) 回傳最新
        cur.execute("SELECT * FROM users WHERE id=%s", (user["id"],))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()

