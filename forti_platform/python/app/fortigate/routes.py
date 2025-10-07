# app/fortigate/routes.py

from flask import render_template, redirect, url_for
from .policy import forti_policy_bp
from .device import forti_device_bp
from .task_list import forti_task_list_bp
from app.decorators.decorators import login_required

@forti_policy_bp.route("/requests", endpoint="page_request_index")
@login_required
def page_request_index():
    return render_template("request_list.html")

@forti_device_bp.route("/device", endpoint="page_device_index")
@login_required
def page_device_index():
    return render_template("forti_device.html")

@forti_task_list_bp.route("/task_list_page", endpoint="page_task_list_index")
@login_required
def page_task_list_index():
    return render_template("task_list.html")
