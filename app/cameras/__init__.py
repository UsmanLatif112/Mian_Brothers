from flask import Blueprint

cameras_bp = Blueprint('cameras', __name__)

from app.cameras import routes  # noqa: E402,F401
