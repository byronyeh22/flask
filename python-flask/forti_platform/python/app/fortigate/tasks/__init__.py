from flask import Blueprint

forti_tasks_bp = Blueprint("forti_tasks_bp", __name__, url_prefix="/fortigate")

from . import routes

