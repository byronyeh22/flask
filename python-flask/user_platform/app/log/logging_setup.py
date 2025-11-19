# app/log/logging_setup.py
# -----------------------------------------------------------
# 集中：ContextVar、欄位蒐集、Filter、Formatter、Handlers
# __init__.py 只需呼叫本檔提供的函式，不再重複寫解析邏輯
# -----------------------------------------------------------
import os
import sys
import time
import uuid
import logging
import inspect
from datetime import datetime
from contextvars import ContextVar
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from pythonjsonlogger import jsonlogger
from flask import has_request_context, request, session, current_app, g

# ==== 開關：避免與 hooks 重複偵測（預設關閉自動偵測） ====
ENABLE_LOGIN_USER_AUTO_DETECT   = os.getenv("LOG_AUTODETECT_LOGIN_USER", "0").lower() in ("1", "true", "yes", "on")
ENABLE_WORKFLOW_ID_AUTO_DETECT  = os.getenv("LOG_AUTODETECT_WORKFLOW_ID", "0").lower() in ("1", "true", "yes", "on")

# ==== 從 ContextVar 提取 Context 值 ====
trace_id_context          = ContextVar("trace_id",          default=None)
start_time_context        = ContextVar("start_time",        default=None)

endpoint_context          = ContextVar("endpoint",          default=None)
view_function_context     = ContextVar("view_function",     default=None)
view_module_context       = ContextVar("view_module",       default=None)
view_file_path_context    = ContextVar("view_file_path",    default=None)

http_method_context       = ContextVar("http_method",       default=None)
url_path_context          = ContextVar("url_path",          default=None)
client_ip_context         = ContextVar("client_ip",         default=None)

workflow_id_context       = ContextVar("workflow_id",       default=None)
component_context         = ContextVar("component",         default=None)
login_user_context        = ContextVar("login_user",        default=None)

# ==== Helper：trace 與 duration ====
def new_trace_id() -> str:
    return uuid.uuid4().hex

def set_trace_id(trace_id: str | None) -> None:
    trace_id_context.set(trace_id)

def get_current_trace_id() -> str | None:
    return trace_id_context.get()

def mark_start() -> None:
    start_time_context.set(time.time())

def get_duration_ms() -> int | None:
    start_time = start_time_context.get()
    if start_time is None:
        return None
    return int((time.time() - start_time) * 1000)

def clear_context() -> None:
    for context_var in (
        trace_id_context, start_time_context,
        endpoint_context, view_function_context, view_module_context, view_file_path_context,
        http_method_context, url_path_context, client_ip_context,
        workflow_id_context, component_context, login_user_context
    ):
        context_var.set(None)

# ==== 欄位蒐集 ====
def get_client_ip_from_request() -> str | None:
    if not has_request_context():
        return None
    x_forwarded_for_header = (request.headers.get("X-Forwarded-For") or "")
    # 取最左邊的 IP，避免拿到 proxy ip
    first_forwarded_ip = x_forwarded_for_header.split(",")[0].strip() if x_forwarded_for_header else ""
    return first_forwarded_ip or request.headers.get("X-Real-IP") or request.remote_addr

def get_workflow_id_from_request() -> str | None:
    if not has_request_context():
        return None
    header_value = request.headers.get("X-Workflow-ID")
    query_value = request.args.get("workflow_id")
    form_value = request.form.get("workflow_id") if request.method in ("POST", "PUT", "PATCH") else None
    if header_value:
        return str(header_value)
    if query_value:
        return str(query_value)
    if form_value:
        return str(form_value)
    if request.is_json:
        payload_body = request.get_json(silent=True) or {}
        json_value = payload_body.get("workflow_id")
        if json_value is not None:
            return str(json_value)
    return None

def get_login_user_from_request() -> str | None:
    if not has_request_context():
        return None
    # Flask-Login（若有）
    try:
        from flask_login import current_user
        if current_user and getattr(current_user, "is_authenticated", False):
            return (
                getattr(current_user, "username", None)
                or getattr(current_user, "email", None)
                or (str(current_user.get_id()) if hasattr(current_user, "get_id") else None)
            )
    except Exception:
        pass
    # 你的 session 鍵名慣例
    for session_key in ("user", "username", "account", "email", "uid"):
        session_value = session.get(session_key)
        if session_value:
            return str(session_value)
    # 代理/自訂 Header（可選）
    for header_key in ("X-User", "X-Authenticated-User", "X-Remote-User"):
        header_value = request.headers.get(header_key)
        if header_value:
            return header_value
    return None

def resolve_view_info_from_request():
    """回傳 (endpoint, view_function_name, view_module_name, view_file_path)"""
    if not has_request_context():
        return None, None, None, None
    endpoint_name = request.endpoint
    view_function = current_app.view_functions.get(endpoint_name) if endpoint_name else None
    view_module_name = getattr(view_function, "__module__", None) if view_function else None
    view_function_name = getattr(view_function, "__name__", None) if view_function else None
    try:
        view_file_path = inspect.getsourcefile(view_function) if view_function else None
    except Exception:
        view_file_path = None
    return endpoint_name, view_function_name, view_module_name, view_file_path

def collect_request_context(component: str | None = "web") -> dict:
    """集中一次蒐集該次 request 會用到的欄位（不寫入 ContextVar）"""
    endpoint_name, view_function_name, view_module_name, view_file_path = resolve_view_info_from_request()
    return {
        "component":   component,
        "http_method": request.method if has_request_context() else None,
        "url_path":    request.path   if has_request_context() else None,
        "client_ip":   get_client_ip_from_request(),
        "endpoint":    endpoint_name,
        "view_func":   view_function_name,
        "view_module": view_module_name,
        "view_file":   view_file_path,
        "workflow_id": get_workflow_id_from_request(),
        "login_user":  get_login_user_from_request(),
    }

# ==== 一次性綁入 ContextVar（供 hooks 呼叫） ====
def bind_context(**kwargs) -> None:
    """把傳入的欄位批次寫入 ContextVar（只寫非 None）"""
    key_to_context = {
        "trace_id": trace_id_context,
        "endpoint": endpoint_context,
        "view_func": view_function_context,
        "view_module": view_module_context,
        "view_file": view_file_path_context,
        "http_method": http_method_context,
        "url_path": url_path_context,
        "client_ip": client_ip_context,
        "workflow_id": workflow_id_context,
        "component": component_context,
        "login_user": login_user_context,
    }
    for key_name, context_var in key_to_context.items():
        value = kwargs.get(key_name, None)
        if value is not None:
            context_var.set(value)

def bind_request_context(component: str | None = "web") -> dict:
    """蒐集 → 綁入 ContextVar；回傳相同 dict（必要時可當作 extra 使用）"""
    context_dict = collect_request_context(component=component)
    bind_context(**{key: value for key, value in context_dict.items() if value is not None})
    return context_dict

# ==== Formatter：console 前綴 + JSON 負載 ====
class ConsoleWithJsonFormatter(logging.Formatter):
    """
    Console 輸出：前綴([台北時間][LEVEL] view_function - ) + 同步的 JSON 負載。
    JSON 欄位與 File handler 共用同一顆 json_formatter，以避免格式不一致。
    """
    def __init__(self, json_formatter: logging.Formatter, timezone: str = "Asia/Taipei", datefmt: str = "%Y/%m/%d %H:%M:%S"):
        super().__init__()
        self.json_formatter = json_formatter
        self.timezone = ZoneInfo(timezone)
        self.datefmt = datefmt

    def format(self, record: logging.LogRecord) -> str:
        taipei_time_str = datetime.fromtimestamp(record.created, tz=self.timezone).strftime(self.datefmt)
        view_name_for_prefix = getattr(record, "view_func", None) or getattr(record, "name", None) or "-"
        json_payload = self.json_formatter.format(record)
        return f"[{taipei_time_str}] [{record.levelname}] {view_name_for_prefix} - {json_payload}"

# ==== Filter：把 ContextVar 的欄位補進每筆記錄 ====
class ContextFilter(logging.Filter):
    def __init__(self, service: str = "user_platform") -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        # 基礎欄位
        record.trace_id  = getattr(record, "trace_id", None) or trace_id_context.get()
        record.service   = getattr(record, "service",  None) or self.service
        record.component = getattr(record, "component",None) or component_context.get()

        # 路由/來源上下文
        record.endpoint     = getattr(record, "endpoint",     None) or endpoint_context.get()
        record.view_func    = getattr(record, "view_func",    None) or view_function_context.get()
        record.view_module  = getattr(record, "view_module",  None) or view_module_context.get()
        record.view_file    = getattr(record, "view_file",    None) or view_file_path_context.get()

        # HTTP 上下文
        record.http_method  = getattr(record, "http_method",  None) or http_method_context.get()
        record.url_path     = getattr(record, "url_path",     None) or url_path_context.get()
        record.client_ip    = getattr(record, "client_ip",    None) or client_ip_context.get()

        # 業務上下文
        record.workflow_id  = getattr(record, "workflow_id",  None) or workflow_id_context.get()
        record.login_user   = (
            getattr(record, "login_user", None)
            or login_user_context.get()
            or (get_login_user_from_request() if ENABLE_LOGIN_USER_AUTO_DETECT else None)
            or "-"
        )
        return True

# ==== 統一的 http access logger ====
http_access_logger = logging.getLogger("http.access")

def log_request_start() -> None:
    """寫 request_start；stacklevel=3 讓 pathname/lineno 更靠近呼叫端"""
    http_access_logger.info("request_start", extra={"event": "request_start"}, stacklevel=3)

# ==== 設定 logging：console（可讀）+ file（純 JSON） ====
def configure_logging(level: str = "INFO", service: str = "user_platform") -> None:
    # 清掉既有 handlers（避免 dev reload 重複加掛）
    root_logger = logging.getLogger()
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)
    root_logger.setLevel(level)  # 允許直接用字串等級

    # 共用 JSON formatter（console 與 file 共用）
    json_formatter = jsonlogger.JsonFormatter(
        fmt="%(message)s %(levelname)s %(name)s %(trace_id)s %(service)s %(component)s "
            "%(pathname)s %(lineno)d %(endpoint)s %(view_func)s %(view_module)s %(view_file)s "
            "%(http_method)s %(url_path)s %(client_ip)s %(workflow_id)s %(login_user)s",
        rename_fields={
            "levelname": "log_level",
            "asctime": "@timestamp",
            "name": "logger",
        },
        timestamp=True,
    )

    # Console log
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(ConsoleWithJsonFormatter(json_formatter, timezone="Asia/Taipei"))
    console_handler.addFilter(ContextFilter(service=service))
    root_logger.addHandler(console_handler)

    # File output（純 JSON，讓 Fluent Bit tail）
    log_file_path = os.environ.get("LOG_FILE_PATH")
    if log_file_path:
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        from logging.handlers import WatchedFileHandler
        file_handler = WatchedFileHandler(log_file_path, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(json_formatter)
        file_handler.addFilter(ContextFilter(service=service))
        root_logger.addHandler(file_handler)

# ==== with 區塊暫時性覆蓋 Context（可用可不用） ====
@contextmanager
def log_context(**kwargs):
    """
    用法：
        with with_temp_context(workflow_id="123", component="pipeline"):
            logger.info("x")  # 這段內會帶上暫時欄位
    會自動保存/還原舊值，不汙染其他請求或後續程式。
    """
    old_values = {
        "trace_id": get_current_trace_id(),
        "endpoint": endpoint_context.get(),
        "view_func": view_function_context.get(),
        "view_module": view_module_context.get(),
        "view_file": view_file_path_context.get(),
        "http_method": http_method_context.get(),
        "url_path": url_path_context.get(),
        "client_ip": client_ip_context.get(),
        "workflow_id": workflow_id_context.get(),
        "component": component_context.get(),
        "login_user": login_user_context.get(),
    }
    try:
        bind_context(**kwargs)
        yield
    finally:
        set_trace_id(old_values["trace_id"])
        endpoint_context.set(old_values["endpoint"]);        view_function_context.set(old_values["view_func"])
        view_module_context.set(old_values["view_module"]);  view_file_path_context.set(old_values["view_file"])
        http_method_context.set(old_values["http_method"]);  url_path_context.set(old_values["url_path"])
        client_ip_context.set(old_values["client_ip"]);      workflow_id_context.set(old_values["workflow_id"])
        component_context.set(old_values["component"]);      login_user_context.set(old_values["login_user"])
