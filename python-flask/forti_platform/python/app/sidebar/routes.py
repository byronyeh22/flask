from flask import session
from app.sidebar.db.sidebar_handler import (
    get_sidebar_structure,
    get_permissions_for_roles,  # 新增：多角色→權限聯集
)

def inject_sidebar_data():
    # 1) 首選：使用登入時算好的權限（零 DB）
    permissions = session.get("permissions")

    # 2) 後備：如果 session 沒帶到（例如某些舊流程），用 roles 聯集查一次 DB
    if permissions is None:
        roles = session.get("roles") or []
        permissions = get_permissions_for_roles(roles) if roles else []

    raw_sidebar = get_sidebar_structure()

    filtered_sidebar = []
    for section in raw_sidebar:
        filtered_items = [
            item for item in section["items"]
            if (not item.get("permission_key")) or (item.get("permission_key") in permissions)
        ]
        if filtered_items:
            filtered_sidebar.append({
                "identifier": section["identifier"],
                "name": section["name"],
                "icon": section["icon"],
                "items": filtered_items
            })

    return {
        "user_permissions": permissions,
        "sidebar": filtered_sidebar
    }

