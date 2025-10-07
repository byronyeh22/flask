from flask import Blueprint

forti_task_list_bp = Blueprint("forti_task_list", __name__, url_prefix="/fortigate")

from . import routes
