from __future__ import annotations
from flask import Blueprint

forti_device_bp = Blueprint(
    "forti_device",
    __name__,
    url_prefix="/fortigate/device",   # ← 依你的要求使用 /fortigate/device
    template_folder="../templates",
)

from . import routes  # noqa: E402,F401

