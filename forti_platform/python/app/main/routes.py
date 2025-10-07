from flask import render_template, request, redirect, url_for, flash, session
from app.decorators.decorators import login_required
from . import main_bp

@main_bp.route("/")
@login_required
def index():
    return render_template("main.html")
