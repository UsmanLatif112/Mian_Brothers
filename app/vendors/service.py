"""Vendor helpers — link inventory purchases to vendor ledgers."""

from app.models import (
    db, Vendor, VendorPayment, ItemPurchaseLog, OtherItem, Inventory, StockEntry,
)
from app.tenancy import apply_agency_filter, require_agency_access, stamp_agency


AUTO_PAY_NOTE_PREFIX = 'Paid with inventory purchase:'
EXPENSE_SETTLE_CATEGORY = 'expense_settle'

# Named vendors used for opening / previous stock (excluded from purchasing KPIs + till).
OPENING_VENDOR_NAMES = frozenset({
    'open for previous',
    'opening',
    'opening stock',
    'previous stock',
    'previous / opening',
})


def normalize_funding_source(raw, default='till'):
    """till = cash in hand; account = cash-taken / Account pool."""
    value = (raw or default or 'till').strip().lower()
    if value in ('account', 'taken', 'cash_taken', 'from_account'):
        return 'account'
    return 'till'


def payment_funding_source(payment, default='account'):
    """Read funding; missing/legacy rows default to account (did not drain till)."""
    return normalize_funding_source(getattr(payment, 'funding_source', None), default=default)


def is_opening_vendor_name(name):
    """True for the special opening-stock vendor label(s)."""
    key = ' '.join(str(name or '').strip().lower().split())
    return key in OPENING_VENDOR_NAMES


def is_expense_settle_purchase(log):
    """True when ItemPurchaseLog is an expense→vendor unpaid settle (not stock)."""
    return (getattr(log, 'category', None) or '').strip().lower() == EXPENSE_SETTLE_CATEGORY


def is_opening_stock_purchase(log):
    """
    Opening / previous stock — no vendor, or vendor named "Open for previous".
    Already on hand — must not inflate Purchasing or Account purchase KPIs.
    """
    name = getattr(log, 'vendor', None) or ''
    if is_opening_vendor_name(name):
        return True
    vendor_ref = getattr(log, 'vendor_ref', None)
    if vendor_ref is not None and is_opening_vendor_name(getattr(vendor_ref, 'name', None)):
        return True
    if getattr(log, 'vendor_id', None):
        return False
    return not (name or '').strip()


def counts_toward_purchase_kpis(log):
    """Real vendor purchase batches only (exclude opening stock + expense settles)."""
    if is_expense_settle_purchase(log):
        return False
    if is_opening_stock_purchase(log):
        return False
    return True


def payment_counts_toward_cash(payment):
    """Opening-stock vendor payments must not drain cash in hand / account pool."""
    if not payment:
        return False
    log = getattr(payment, 'purchase_log', None)
    if log is None and getattr(payment, 'purchase_log_id', None):
        log = ItemPurchaseLog.query.get(payment.purchase_log_id)
    if log is not None and is_opening_stock_purchase(log):
        return False
    vendor = getattr(payment, 'vendor', None)
    if vendor is not None and is_opening_vendor_name(getattr(vendor, 'name', None)):
        return False
    return True


def upsert_expense_vendor_payable(expense, vendor, settled_date, settle_note=None, user_id=None):
    """
    Create/update unpaid ItemPurchaseLog for expense settled to a vendor.
    No stock movement — payable only. Returns (log, vendor_ids_to_recalc).
    """
    from datetime import datetime, time
    from app.utils import expense_settle_remark

    amount = float(expense.amount or 0)
    item_name = (expense_settle_remark(expense.name, settle_note) or 'Settle')[:100]
    entry_dt = datetime.combine(settled_date, time.min) if settled_date else datetime.utcnow()
    touched = set()
    old_vendor_id = getattr(expense, 'settle_vendor_id', None)
    log = None
    log_id = getattr(expense, 'settle_purchase_log_id', None)
    if log_id:
        log = ItemPurchaseLog.query.get(log_id)

    if log is None:
        log = stamp_agency(ItemPurchaseLog(
            category=EXPENSE_SETTLE_CATEGORY,
            item_name=item_name,
            cost_price=amount,
            sale_price=0,
            quantity=1,
            liters=None,
            payment_status='unpaid',
            entry_date=entry_dt,
            added_by=user_id or expense.recorded_by,
        ))
        db.session.add(log)
        db.session.flush()
        expense.settle_purchase_log_id = log.id
    else:
        if log.vendor_id and log.vendor_id != vendor.id:
            touched.add(log.vendor_id)
        log.category = EXPENSE_SETTLE_CATEGORY
        log.item_name = item_name
        log.cost_price = amount
        log.sale_price = 0
        log.quantity = 1
        log.liters = None
        log.payment_status = 'unpaid'
        log.entry_date = entry_dt
        # Drop any auto-payments if status was flipped somehow
        for pay in find_auto_payments_for_log(log):
            db.session.delete(pay)

    link_purchase_to_vendor(vendor.name, log, vendor=vendor, increment_balance=False)
    apply_purchase_vendor_payment(vendor, log, payment_status='unpaid', payment_date=entry_dt)
    expense.settle_vendor_id = vendor.id
    touched.add(vendor.id)
    if old_vendor_id:
        touched.add(old_vendor_id)
    return log, touched


def clear_expense_vendor_payable(expense):
    """Delete linked expense-settle purchase log; returns vendor ids to recalc."""
    touched = set()
    if getattr(expense, 'settle_vendor_id', None):
        touched.add(expense.settle_vendor_id)
    log_id = getattr(expense, 'settle_purchase_log_id', None)
    if log_id:
        log = ItemPurchaseLog.query.get(log_id)
        if log:
            if log.vendor_id:
                touched.add(log.vendor_id)
            for pay in find_auto_payments_for_log(log):
                db.session.delete(pay)
            # No stock reverse — expense settles never added stock
            db.session.delete(log)
            db.session.flush()
    expense.settle_purchase_log_id = None
    expense.settle_vendor_id = None
    return touched


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

    existing = apply_agency_filter(
        Vendor.query.filter(db.func.lower(Vendor.name) == clean.lower()), Vendor
    ).first()
    if existing:
        return existing

    vendor = Vendor(name=clean)
    stamp_agency(vendor)
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
            require_agency_access(vendor)
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
        db.session.flush()
        recalculate_vendor_balance(vendor)

    return vendor


def normalize_purchase_payment_status(status):
    status = (status or 'unpaid').strip().lower()
    return 'paid' if status == 'paid' else 'unpaid'


def purchase_log_payment_status(log):
    """Resolve paid/unpaid from column, falling back to linked auto-payment."""
    stored = normalize_purchase_payment_status(getattr(log, 'payment_status', None))
    if stored == 'paid':
        return 'paid'
    if find_auto_payments_for_log(log):
        return 'paid'
    return 'unpaid'


def apply_purchase_vendor_payment(
    vendor, purchase_log, payment_status='unpaid', payment_date=None, funding_source='account',
):
    """
    After purchase is linked:

    unpaid → payment_status=unpaid, payable increases by batch total
    paid   → payment_status=paid + one linked VendorPayment (ledger shows ONE paid row)
    Never leave a paid batch as unpaid with a separate “auto” payment line.

    Inventory defaults to account funding so paid buys do not drain cash in hand.
    Opening-stock vendor (\"Open for previous\") is always unpaid — no payment row.
    """
    # Opening stock is not a real purchase — never create a till/account payment.
    if is_opening_stock_purchase(purchase_log) or (
        vendor is not None and is_opening_vendor_name(getattr(vendor, 'name', None))
    ):
        status = 'unpaid'
        purchase_log.payment_status = status
        if getattr(purchase_log, 'id', None):
            for old in find_auto_payments_for_log(purchase_log):
                db.session.delete(old)
            db.session.flush()
        if vendor:
            db.session.flush()
            recalculate_vendor_balance(vendor)
        return None

    status = normalize_purchase_payment_status(payment_status)
    purchase_log.payment_status = status
    payment = None
    funding = normalize_funding_source(funding_source, default='account')

    # Drop any prior auto-payments for this log so we never get unpaid + paid duplicates
    if getattr(purchase_log, 'id', None):
        for old in find_auto_payments_for_log(purchase_log):
            db.session.delete(old)
        db.session.flush()

    if status == 'paid' and vendor:
        total = purchase_log_total(purchase_log)
        if total > 0:
            db.session.flush()  # need purchase_log.id for FK
            payment = VendorPayment(
                vendor_id=vendor.id,
                amount_paid=total,
                payment_date=payment_date or purchase_log.entry_date,
                method='Cash',
                funding_source=funding,
                note=f'{AUTO_PAY_NOTE_PREFIX} {purchase_log.item_name} (batch PKR {total:,.2f})',
                purchase_log_id=purchase_log.id,
            )
            stamp_agency(payment)
            db.session.add(payment)

    if vendor:
        db.session.flush()
        recalculate_vendor_balance(vendor)
    return payment


def find_auto_payments_for_log(log):
    """VendorPayments created automatically for a paid inventory batch."""
    if not log or not log.id:
        return []
    linked = VendorPayment.query.filter_by(purchase_log_id=log.id).all()
    if linked:
        return linked

    if not log.vendor_id:
        return []
    item = (log.item_name or '').strip()
    amount = purchase_log_total(log)
    found = []
    for pay in VendorPayment.query.filter_by(vendor_id=log.vendor_id).all():
        note = (pay.note or '').strip()
        if not note.startswith(AUTO_PAY_NOTE_PREFIX):
            continue
        if abs(float(pay.amount_paid or 0) - amount) > 0.02:
            continue
        if item and item.lower() not in note.lower():
            continue
        found.append(pay)
    return found


def reverse_purchase_stock(log):
    """Undo stock added by this purchase batch (floor at 0)."""
    from app.services.stock_batches import delete_batch_for_purchase_log
    delete_batch_for_purchase_log(log)

    liters = float(log.liters or 0) if log.liters is not None else 0.0
    qty = int(log.quantity or 0) if log.quantity is not None else 0

    if log.category == 'fuel' and log.fuel_type_id:
        inv = Inventory.query.filter_by(fuel_type_id=log.fuel_type_id).first()
        if inv and liters > 0:
            inv.current_stock_liters = max(0.0, float(inv.current_stock_liters or 0) - liters)
        # Best-effort remove matching stock entry
        if liters > 0:
            se = (
                StockEntry.query
                .filter_by(fuel_type_id=log.fuel_type_id, vendor_id=log.vendor_id)
                .filter(StockEntry.liters_added == liters)
                .order_by(StockEntry.entry_date.desc(), StockEntry.id.desc())
                .first()
            )
            if se and abs(float(se.cost_per_liter or 0) - float(log.cost_price or 0)) < 0.02:
                db.session.delete(se)
        return

    item = _shop_item_for_log(log)
    if not item:
        return
    if log.category == 'ft_mobile' or (liters > 0 and qty <= 0):
        item.liters = max(0.0, float(item.liters or 0) - liters)
    else:
        item.quantity = max(0, int(item.quantity or 0) - qty)


def apply_purchase_stock_delta(log, old_liters, old_qty, new_liters, new_qty):
    """Adjust live stock when a purchase log quantity/liters changes."""
    from app.services.stock_batches import adjust_batch_for_purchase_log

    d_liters = float(new_liters or 0) - float(old_liters or 0)
    d_qty = int(new_qty or 0) - int(old_qty or 0)
    old_batch_qty = float(old_liters or 0) if (
        log.category in ('fuel', 'ft_mobile') or float(old_liters or 0) > 0
    ) else float(old_qty or 0)
    new_batch_qty = float(new_liters or 0) if (
        log.category in ('fuel', 'ft_mobile') or float(new_liters or 0) > 0
    ) else float(new_qty or 0)
    adjust_batch_for_purchase_log(
        log, old_batch_qty, new_batch_qty, unit_cost=float(log.cost_price or 0)
    )

    if log.category == 'fuel' and log.fuel_type_id:
        if abs(d_liters) < 1e-9:
            return
        inv = Inventory.query.filter_by(fuel_type_id=log.fuel_type_id).first()
        if not inv:
            inv = Inventory(fuel_type_id=log.fuel_type_id, current_stock_liters=0, reorder_threshold=0)
            stamp_agency(inv)
            db.session.add(inv)
        inv.current_stock_liters = max(0.0, float(inv.current_stock_liters or 0) + d_liters)
        return

    item = _shop_item_for_log(log)
    if not item:
        return
    if log.category == 'ft_mobile' or (float(new_liters or 0) > 0 and int(new_qty or 0) <= 0):
        item.liters = max(0.0, float(item.liters or 0) + d_liters)
    else:
        item.quantity = max(0, int(item.quantity or 0) + d_qty)


def _shop_item_for_log(log):
    if log.category == 'fuel':
        return None
    q = apply_agency_filter(
        OtherItem.query.filter_by(category=log.category or 'other', name=log.item_name),
        OtherItem,
    )
    if log.company is not None:
        q = q.filter_by(company=log.company)
    if log.item_type is not None:
        q = q.filter_by(item_type=log.item_type)
    return q.first()


def delete_purchase_log_cascade(log):
    """
    Delete a purchase batch everywhere it touches:
    auto vendor payments, stock, purchase log, vendor payable.
    """
    vendor_id = log.vendor_id
    for pay in find_auto_payments_for_log(log):
        db.session.delete(pay)
    reverse_purchase_stock(log)
    db.session.delete(log)
    db.session.flush()
    if vendor_id:
        vendor = Vendor.query.get(vendor_id)
        if vendor:
            recalculate_vendor_balance(vendor)
    return vendor_id


def sync_auto_payment_for_log(log, payment_status=None, payment_date=None, funding_source=None):
    """Keep linked auto-payment + payment_status in sync after log edit."""
    status = normalize_purchase_payment_status(
        payment_status if payment_status is not None else getattr(log, 'payment_status', None)
    )
    log.payment_status = status
    total = purchase_log_total(log)
    pays = find_auto_payments_for_log(log)
    funding = normalize_funding_source(
        funding_source if funding_source is not None else (
            getattr(pays[0], 'funding_source', None) if pays else 'till'
        ),
        default='till',
    )

    if status == 'unpaid':
        for pay in pays:
            db.session.delete(pay)
        return

    # paid
    if total <= 0:
        for pay in pays:
            db.session.delete(pay)
        return

    if pays:
        pay = pays[0]
        for extra in pays[1:]:
            db.session.delete(extra)
        pay.amount_paid = total
        pay.note = f'{AUTO_PAY_NOTE_PREFIX} {log.item_name} (batch PKR {total:,.2f})'
        pay.purchase_log_id = log.id
        pay.funding_source = funding
        if log.vendor_id:
            pay.vendor_id = log.vendor_id
        if payment_date is not None:
            pay.payment_date = payment_date
        return

    if not log.vendor_id:
        return
    db.session.flush()
    db.session.add(stamp_agency(VendorPayment(
        vendor_id=log.vendor_id,
        amount_paid=total,
        payment_date=payment_date or log.entry_date,
        method='Cash',
        funding_source=funding,
        note=f'{AUTO_PAY_NOTE_PREFIX} {log.item_name} (batch PKR {total:,.2f})',
        purchase_log_id=log.id,
    )))


def recalculate_vendor_balance(vendor):
    """Rebuild payable balance from opening + purchases − payments."""
    total_purchases = 0.0
    for log in ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).all():
        if is_opening_stock_purchase(log):
            continue
        total_purchases += purchase_log_total(log)

    opening = float(vendor.previous_payable or 0)
    total_paid = sum(
        float(p.amount_paid or 0) for p in vendor.payments
        if payment_counts_toward_cash(p)
    )
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
    funding = normalize_funding_source(
        form.get('funding_source') or getattr(payment, 'funding_source', None),
        default='till',
    )

    payment.amount_paid = amt
    payment.payment_date = datetime_from_date(entry_date)
    payment.method = method
    payment.note = note
    payment.funding_source = funding

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


def vendor_has_linked_activity(vendor):
    """True if vendor has purchases or payments — block hard delete."""
    if ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).first():
        return True
    if VendorPayment.query.filter_by(vendor_id=vendor.id).first():
        return True
    if StockEntry.query.filter_by(vendor_id=vendor.id).first():
        return True
    if float(vendor.current_balance_payable or 0) != 0:
        return True
    if vendor.previous_payable is not None and float(vendor.previous_payable or 0) != 0:
        return True
    return False
