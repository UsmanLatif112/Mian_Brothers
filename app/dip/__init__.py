from flask import Blueprint

dip_bp = Blueprint('dip', __name__)

from app.dip import routes  # noqa: E402,F401
