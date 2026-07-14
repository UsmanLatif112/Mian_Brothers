from flask import Blueprint

expenses_bp = Blueprint('expenses', __name__)

from app.expenses import routes  # noqa: E402,F401
