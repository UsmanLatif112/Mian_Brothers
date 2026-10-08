"""Purchasing UI removed — purchases live on vendor ledgers + inventory."""
from flask import redirect, url_for, flash
from flask_login import login_required

from app.purchasing import purchasing_bp


@purchasing_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    flash('Purchasing is in Vendor ledgers now. Record stock from Inventory.', 'info')
    return redirect(url_for('vendors.index'))
