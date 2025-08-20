
import os # 讀取你放的使用者帳號密碼檔案 user.yml
import yaml # 讀取你放的使用者帳號密碼檔案 user.yml
from flask import session, request, redirect, url_for, flash, render_template # flask 的各種功能用於處理 Web 請求、回應、session（用戶狀態）、重新導向等
from functools import wraps # 用於製作裝飾器，包裝函式時保留原函式資訊
from . import auth_bp # 在 __init__.py 定義的 Blueprint，代表「這組路由屬於 auth 功能模組」
# from app.utils.util import log_operation

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user.yml')

# 讀取使用者資料（load_users）
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return yaml.safe_load(f).get("users", {})
    return {}

users = load_users()

# 登入保護的裝飾器 — login_required
# 這是一個自訂的「登入檢查」裝飾器，用來包裝需要登入後才能訪問的頁面。
# session 是 Flask 提供用戶端（瀏覽器）和伺服器間暫存狀態的功能。
# 這段會檢查 session 裡是否有 username，如果沒有就轉向登入頁面
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return wrapper


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    users = load_users()
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if username in users and users[username].get("password") == password:
            session["username"] = username
            # session["role"] = users[username].get("role", "user")
            # log_operation(username, "LOGIN", "Login successful")
            return redirect(url_for("auth.auth_index"))
        flash("Invalid credentials", "danger")
        # log_operation(username or "unknown", "LOGIN_FAIL", "Login failed")
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

# @auth_bp.route("/")
# @login_required
# def index():
#     return render_template("auth_index.html")

# ----------main page----------
from app.mysql.db import get_db_connection
from .db.get_jira_tickets_and_stats import get_jira_tickets_and_stats

@auth_bp.route("/")
def auth_index():
    db_conn = get_db_connection()
    jira_tickets = get_jira_tickets_and_stats(db_conn)

    return render_template("auth_index.html", jira_tickets=jira_tickets)