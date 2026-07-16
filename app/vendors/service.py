"""Vendor helpers — link inventory purchases to vendor ledgers."""

from app.models import db, Vendor, ItemPurchaseLog


def normalize_vendor_name(name):
    return ' '.join((name or '').strip().split())


def purchase_log_total(log):
    """Total purchase spend for one log row (cost × liters or quantity)."""
    cost = float(log.cost_price or 0)
    if log.category in ('fuel', 'ft_mobile'):
        return cost * float(log.liters or 0)
    return cost * float(log.quantity or 0)


def get_or_create_vendor(name):
    clean = normalize_vendor_name(name)
    if not clean:
        return None

    existing = Vendor.query.filter(db.func.lower(Vendor.name) == clean.lower()).first()
    if existing:
        return existing

    vendor = Vendor(name=clean)
    db.session.add(vendor)
    db.session.flush()
    return vendor


def link_purchase_to_vendor(vendor_name, purchase_log, stock_entry=None, increment_balance=True):
    """Attach a purchase log (and optional stock entry) to a vendor ledger."""
    vendor = get_or_create_vendor(vendor_name)
    if not vendor:
        return None

    purchase_log.vendor_id = vendor.id
    purchase_log.vendor = vendor.name
    if stock_entry is not None:
        stock_entry.vendor_id = vendor.id
        stock_entry.supplier = vendor.name

    if increment_balance:
        vendor.current_balance_payable = float(vendor.current_balance_payable or 0) + purchase_log_total(purchase_log)

    return vendor


def recalculate_vendor_balance(vendor):
    """Rebuild payable balance from purchases and payments."""
    total_purchases = 0.0
    for log in ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).all():
        total_purchases += purchase_log_total(log)

    opening = float(vendor.previous_payable or 0)
    total_paid = sum(float(p.amount_paid or 0) for p in vendor.payments)
    vendor.current_balance_payable = opening + total_purchases - total_paid
    return vendor.current_balance_payable
