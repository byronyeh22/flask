from flask import session
from app.sidebar.db.sidebar_handler import get_all_roles
from app.sidebar.routes import inject_sidebar_data

def init_sidebar_extensions(app):
    
    @app.context_processor
    def inject_roles():
        return {"sidebar": get_all_roles()}

    @app.context_processor
    def inject_sidebar():
        return inject_sidebar_data()
