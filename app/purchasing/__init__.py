from flask import Blueprint

purchasing_bp = Blueprint('purchasing', __name__)

from app.purchasing import routes  # noqa: E402,F401
