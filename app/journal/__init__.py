from flask import Blueprint

journal_bp = Blueprint('journal', __name__)

from app.journal import routes  # noqa: E402,F401
