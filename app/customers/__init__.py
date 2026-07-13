from flask import Blueprint

customers_bp = Blueprint('customers', __name__)

from app.customers import routes
