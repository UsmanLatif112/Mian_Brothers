from flask import Blueprint

vendors_bp = Blueprint('vendors', __name__)

from app.vendors import routes
