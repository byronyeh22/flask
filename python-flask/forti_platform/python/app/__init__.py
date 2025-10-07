from flask import Flask, render_template, redirect, url_for, session
import secrets
import os

def create_app():
    app = Flask(__name__)
    app.config['SESSION_COOKIE_NAME'] = 'forti_platform_session'
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

    from app.auth import auth_bp
    app.register_blueprint(auth_bp)

    from app.main import main_bp
    app.register_blueprint(main_bp)

    from app.fortigate import forti_policy_bp, forti_device_bp, forti_drafts_bp, forti_pipeline_bp, forti_tasks_bp, forti_task_list_bp
    app.register_blueprint(forti_policy_bp)
    app.register_blueprint(forti_device_bp)
    app.register_blueprint(forti_drafts_bp)
    app.register_blueprint(forti_pipeline_bp)
    app.register_blueprint(forti_tasks_bp)
    app.register_blueprint(forti_task_list_bp)

    from app.settings.timezone import tz_bp
    app.register_blueprint(tz_bp)
    
    from app.utils.tz import register_tz_helpers
    register_tz_helpers(app)

    from app.sidebar import init_sidebar_extensions
    init_sidebar_extensions(app)

    @app.errorhandler(404)
    def page_not_found(e):
        if not session.get("username"):
            return redirect(url_for("auth.login"))
        return render_template("404.html"), 404
    

    return app

