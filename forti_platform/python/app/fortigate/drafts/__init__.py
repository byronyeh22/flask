from flask import Blueprint

forti_drafts_bp = Blueprint("forti_drafts_bp", __name__, url_prefix="/fortigate")

from . import routes
