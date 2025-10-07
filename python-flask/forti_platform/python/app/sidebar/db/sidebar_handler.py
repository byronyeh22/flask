from app.db.mysql import get_db_connection

def get_sidebar_structure():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id, name, icon_class, identifier
        FROM sidebar_sections
        ORDER BY sort_order, id
    """)
    sections = cursor.fetchall()

    sidebar = []
    for section in sections:
        cursor.execute("""
            SELECT name, permission_key, endpoint
            FROM sidebar_items
            WHERE section_id = %s
            ORDER BY sort_order, id
        """, (section['id'],))
        items = cursor.fetchall()
        sidebar.append({
            'identifier': section['identifier'],
            'name': section['name'],
            'icon': section['icon_class'],
            'items': items
        })

    cursor.close()
    conn.close()
    return sidebar


def get_role_permissions(role_name):
    """相容舊用法：單一角色 -> 權限"""
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
    cursor.close()
    conn.close()
    return permissions


# === 新增：多角色 -> 權限聯集 ===
def get_permissions_for_roles(role_names):
    """
    以多個角色名稱回傳權限聯集；role_names 可為 list/tuple/set。
    """
    names = list({n for n in (role_names or []) if n})
    if not names:
        return []
    conn = get_db_connection()
    cursor = conn.cursor()
    fmt = ",".join(["%s"] * len(names))
    cursor.execute(f"""
        SELECT DISTINCT p.permission_key
        FROM permissions p
        JOIN role_permissions rp ON p.id = rp.permission_id
        JOIN roles r ON r.id = rp.role_id
        WHERE r.role_name IN ({fmt})
    """, names)
    perms = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return perms


def get_all_roles():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT r.role_name, p.category
        FROM roles r
        LEFT JOIN role_permissions rp ON r.id = rp.role_id
        LEFT JOIN permissions p ON rp.permission_id = p.id
        ORDER BY r.role_name
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    roles = {}
    for row in rows:
        role = row["role_name"]
        category = row["category"]
        if role not in roles:
            roles[role] = []
        if category and category not in roles[role]:
            roles[role].append(category)
    return roles

