from flask import Blueprint

sales_bp = Blueprint('sales', __name__)

from app.sales import routes
