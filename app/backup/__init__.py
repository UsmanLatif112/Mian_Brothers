from flask import Blueprint

backup_bp = Blueprint('backup', __name__)

from app.backup import routes  # noqa: E402,F401
