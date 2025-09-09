from flask import Blueprint

# Blueprint 的 url_prefix='/auth' 代表所有這裡的路由會加上 /auth 路徑前綴 例如: http://localhost/auth/login , 可以用 url_prefix='' 就不用加 /auth
vm_bp = Blueprint('vm', __name__, url_prefix='', template_folder='templates')

# from . import routes

# 直接導入所有路由文件，路由會自動註冊到 vm_bp
from .routes import workflow_routes
from .routes import vsphere_connection_routes
from .routes import form_routes
from .routes import main_routes
