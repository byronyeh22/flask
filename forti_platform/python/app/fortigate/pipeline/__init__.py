from flask import Blueprint

forti_pipeline_bp = Blueprint("forti_pipeline_bp", __name__, url_prefix="/fortigate")

from . import routes
