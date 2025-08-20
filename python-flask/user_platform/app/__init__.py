import os
from flask import Flask
import json # 1. 匯入 json 函式庫


def from_json_filter(value):
    """A custom Jinja2 filter to parse a JSON string."""
    return json.loads(value)

def create_app():

    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    app.secret_key = b'some_secure_key'
    app.jinja_env.filters['fromjson'] = from_json_filter

    # Import and initialize DB
    from app.mysql.db import init_db
    init_db()

    # Import & register for Blueprint
    # auth - Login
    from app.auth import auth_bp
    app.register_blueprint(auth_bp)

    # vSphere - VM
    from app.vsphere.vm import vm_bp
    app.register_blueprint(vm_bp)
    ## scheduler - VM
    from app.vsphere.vm.scheduler.pipeline_monitor import start_monitor_thread
    start_monitor_thread()


    return app
