import os
from flask import Flask
import json
from config import Config  # 導入 Config
from app.mysql.db import init_db # 保持這行，但只用於導入

def from_json_filter(value):
    """A custom Jinja2 filter to parse a JSON string."""
    return json.loads(value)

def create_app():
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    app.secret_key = b'some_secure_key'
    app.jinja_env.filters['fromjson'] = from_json_filter
    app.config.from_object(Config)

    # 修正: 將 init_db() 移至 app context 內
    with app.app_context():
        init_db()

    # Import & register for Blueprint
    from app.auth import auth_bp
    app.register_blueprint(auth_bp)

    from app.vsphere.vm import vm_bp
    app.register_blueprint(vm_bp)
    
    from app.vsphere.vm.scheduler.pipeline_monitor import start_monitor_thread
    start_monitor_thread(app)

    return app