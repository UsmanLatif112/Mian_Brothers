from flask import Blueprint

receipts_bp = Blueprint('receipts', __name__)

from app.receipts import routes  # noqa: E402, F401
