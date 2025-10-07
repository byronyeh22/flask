from __future__ import annotations
from flask import Blueprint

forti_policy_bp = Blueprint("forti_policy", __name__, url_prefix="/fortigate", template_folder="../templates",)

from . import routes
