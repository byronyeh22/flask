from flask import Flask, request, jsonify, current_app, session, g, current_app
from config import Config
from app.mysql.db import init_db
# JSON Logger
from app.log.logging_setup import (
    configure_logging,
    set_trace_id,
    new_trace_id,
    mark_start,
    get_duration_ms,
    bind_request_context,
    log_request_start,
    get_current_trace_id
)

from werkzeug.exceptions import HTTPException

import inspect
import json
import os
import logging

def from_json_filter(value):
    """A custom Jinja2 filter to parse a JSON string."""
    return json.loads(value)

def create_app():
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    app.secret_key = b'some_secure_key'
    app.jinja_env.filters['fromjson'] = from_json_filter
    # 從 config.py 載入設定值 (例如 DB 連線、日誌級別等)
    app.config.from_object(Config)

    # 啟用 JSON Logger，等級讀 config（預設 INFO）
    configure_logging(level=app.config.get("LOG_LEVEL", "INFO"), service="user_platform")
    http_logger = logging.getLogger("http.access")

    @app.before_request
    def _before_request():
        trace_id_header_or_new = request.headers.get("X-Request-ID") or new_trace_id()
        set_trace_id(trace_id_header_or_new)
        mark_start()
        bind_request_context(component="web")   # ← 集中：蒐集並綁 ContextVar
        log_request_start()                     # ← 起點 log

    @app.after_request
    def _after_request(resp):
        duration_ms = get_duration_ms()
        http_logger = logging.getLogger("http.access")
        http_logger.info(
            "request_end",
            extra={"event": "request_end", "http_status": resp.status_code, "duration_ms": duration_ms},
            stacklevel=2
        )
        resp.headers["X-Trace-ID"] = get_current_trace_id() or ""
        return resp

    @app.errorhandler(HTTPException)
    def _on_http_exception(err: HTTPException):
        trace_id = get_current_trace_id()

        http_logger = logging.getLogger("http.access")
        # 4xx 用 warning；5xx 用 error（保留原始狀態碼）
        logger_function = http_logger.warning if 400 <= err.code < 500 else http_logger.error
        logger_function(
            err.description,
            extra={
                "event": "http_exception",
                "http_status": err.code,
                "url_path": getattr(request, "path", None),
                "http_method": getattr(request, "method", None),
            },
            stacklevel=2,
        )

        # 回傳 JSON + Header，都帶「當前」 trace_id
        response = jsonify({
            "error": err.name,                # e.g. "Not Found"
            "message": err.description,       # e.g. default text
            "trace_id": trace_id or ""
        })
        response.headers["X-Trace-ID"] = trace_id or ""
        return response, err.code

    # @app.route("/debug/raise", methods=["GET"])
    # def _debug_raise():
    #     # 此路由僅用於本地/測試；真正的錯誤處理交由全域 errorhandler
    #     # before_request 會先設定 trace_id；errorhandler 會把「當前」 trace_id 回傳在 JSON 與 Header
    #     raise RuntimeError("debug_raise triggered")

    @app.errorhandler(Exception)
    def _on_error(err):
        trace_id = get_current_trace_id()

        http_logger = logging.getLogger("http.access")
        http_logger.error(
            str(err),
            exc_info=True,
            extra={
                "event": "internal_error",
                "http_status": 500,
                "url_path": getattr(request, "path", None),
                "http_method": getattr(request, "method", None),
            },
            stacklevel=2,
        )

        # 回傳給前端：JSON 與 Header 都帶「當前」 trace_id（None 就傳空字串）
        response = jsonify({"error": "internal_error", "trace_id": trace_id or ""})
        response.headers["X-Trace-ID"] = trace_id or ""
        return response, 500

    # 初始化資料庫
    with app.app_context():
        init_db()

    # Import & register for Blueprint
    from app.auth import auth_bp
    app.register_blueprint(auth_bp)

    from app.vsphere.vm import vm_bp
    app.register_blueprint(vm_bp)

    # 背景監控執行緒
    from app.vsphere.vm.scheduler.pipeline_monitor import start_monitor_thread
    start_monitor_thread(app)

    # 關閉 werkzeug 的 INFO access log（包含自動 refresh 觸發的請求），但若有異常仍會出現
    import logging as _pylogging
    _pylogging.getLogger("werkzeug").setLevel(_pylogging.WARNING)  # 只保留 WARNING/ERROR

    return app