"""Vendor helpers — link inventory purchases to vendor ledgers."""

from app.models import db, Vendor, VendorPayment, ItemPurchaseLog


def normalize_vendor_name(name):
    return ' '.join((name or '').strip().split())


def purchase_log_total(log):
    """Total purchase spend for one log row (cost × liters or quantity)."""
    cost = float(log.cost_price or 0)
    liters = float(log.liters or 0) if log.liters is not None else 0.0
    qty = int(log.quantity or 0) if log.quantity is not None else 0
    if log.category in ('fuel', 'ft_mobile') or (liters > 0 and qty <= 0):
        return cost * liters
    return cost * float(qty)


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

    # Payable is always rebuilt via recalculate_vendor_balance after payment handling.
    # Keep increment_balance for callers that skip apply_purchase_vendor_payment.
    if increment_balance:
        db.session.flush()
        recalculate_vendor_balance(vendor)

    return vendor


def apply_purchase_vendor_payment(vendor, purchase_log, payment_status='unpaid', payment_date=None):
    """
    After purchase is linked (payable already increased by batch total):

    unpaid → leave payable increased by cost × liters/qty
    paid   → record VendorPayment for that same batch total (payable reduced by same amount)
    Always rebuild payable from sources of truth.
    """
    status = (payment_status or 'unpaid').strip().lower()
    payment = None
    if status == 'paid' and vendor:
        total = purchase_log_total(purchase_log)
        if total > 0:
            payment = VendorPayment(
                vendor_id=vendor.id,
                amount_paid=total,
                payment_date=payment_date,
                method='Cash',
                note=f'Paid with inventory purchase: {purchase_log.item_name} (batch PKR {total:,.2f})',
            )
            db.session.add(payment)

    if vendor:
        db.session.flush()
        recalculate_vendor_balance(vendor)
    return payment


def recalculate_vendor_balance(vendor):
    """Rebuild payable balance from opening + purchases − payments."""
    total_purchases = 0.0
    for log in ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).all():
        total_purchases += purchase_log_total(log)

    opening = float(vendor.previous_payable or 0)
    total_paid = sum(float(p.amount_paid or 0) for p in vendor.payments)
    vendor.current_balance_payable = opening + total_purchases - total_paid
    return vendor.current_balance_payable


def edit_vendor_payment(payment, form):
    """Update a vendor payment and rebuild payable."""
    try:
        amt = float(form.get('amount_paid') or form.get('amount'))
        if amt <= 0:
            raise ValueError('Amount must be greater than zero.')
    except (TypeError, ValueError) as e:
        raise ValueError(f'Invalid amount: {e}') from e

    from app.utils import parse_form_date, datetime_from_date

    entry_date = parse_form_date(form.get('entry_date') or form.get('payment_date'))
    method = (form.get('method') or payment.method or 'Cash').strip() or 'Cash'
    note = (form.get('note') or '').strip() or None

    payment.amount_paid = amt
    payment.payment_date = datetime_from_date(entry_date)
    payment.method = method
    payment.note = note

    vendor = Vendor.query.get(payment.vendor_id)
    db.session.flush()
    if vendor:
        recalculate_vendor_balance(vendor)
    return payment


def delete_vendor_payment(payment):
    """Delete a vendor payment and rebuild payable."""
    vendor = Vendor.query.get(payment.vendor_id)
    db.session.delete(payment)
    db.session.flush()
    if vendor:
        recalculate_vendor_balance(vendor)
    return vendor
