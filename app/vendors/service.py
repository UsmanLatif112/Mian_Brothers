"""Vendor helpers — link inventory purchases to vendor ledgers."""

from app.models import db, Vendor, VendorPayment, ItemPurchaseLog


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


def resolve_vendor(vendor_id=None, vendor_name=None):
    """Resolve by id first, otherwise get-or-create by name."""
    if vendor_id:
        try:
            vendor = Vendor.query.get(int(vendor_id))
        except (TypeError, ValueError):
            vendor = None
        if vendor:
            return vendor
    return get_or_create_vendor(vendor_name)


def link_purchase_to_vendor(vendor_name, purchase_log, stock_entry=None, increment_balance=True, vendor=None):
    """Attach a purchase log (and optional stock entry) to a vendor ledger."""
    vendor = vendor or get_or_create_vendor(vendor_name)
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


def apply_purchase_vendor_payment(vendor, purchase_log, payment_status='unpaid', payment_date=None):
    """
    After purchase is linked (payable already increased by batch total):

    unpaid → leave payable increased by cost × liters/qty
    paid   → record VendorPayment for that same batch total (payable reduced by same amount)
    """
    status = (payment_status or 'unpaid').strip().lower()
    if status != 'paid' or not vendor:
        return None

    total = purchase_log_total(purchase_log)
    if total <= 0:
        return None

    payment = VendorPayment(
        vendor_id=vendor.id,
        amount_paid=total,
        payment_date=payment_date,
        method='Cash',
        note=f'Paid with inventory purchase: {purchase_log.item_name} (batch PKR {total:,.2f})',
    )
    db.session.add(payment)
    vendor.current_balance_payable = float(vendor.current_balance_payable or 0) - total
    return payment


def recalculate_vendor_balance(vendor):
    """Rebuild payable balance from purchases and payments."""
    total_purchases = 0.0
    for log in ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).all():
        total_purchases += purchase_log_total(log)

    opening = float(vendor.previous_payable or 0)
    total_paid = sum(float(p.amount_paid or 0) for p in vendor.payments)
    vendor.current_balance_payable = opening + total_purchases - total_paid
    return vendor.current_balance_payable
