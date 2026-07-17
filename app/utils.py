"""Shared helpers for date ranges and period financial stats."""
from datetime import datetime, timedelta, time
from types import SimpleNamespace
from sqlalchemy import func


PERIOD_CHOICES = (
    ('today', 'Today'),
    ('7days', 'Last 7 Days'),
    ('month', 'This Month'),
    ('6months', 'Last 6 Months'),
    ('year', 'This Year'),
    ('all', 'All'),
    ('custom', 'Custom Dates'),
)


def earliest_activity_date():
    """Earliest business date across sales, meters, expenses, payments, purchases."""
    from app.models import (
        MeterReading, CreditSale, Expense, Payment,
        ItemPurchaseLog, StockEntry, DailyCashCount, VendorPayment,
    )

    today = datetime.utcnow().date()
    candidates = []

    def _add(val):
        if val is None:
            return
        if isinstance(val, datetime):
            val = val.date()
        elif isinstance(val, str):
            try:
                val = datetime.strptime(val[:10], '%Y-%m-%d').date()
            except ValueError:
                return
        candidates.append(val)

    _add(MeterReading.query.with_entities(func.min(MeterReading.reading_date)).scalar())
    _add(CreditSale.query.with_entities(func.min(CreditSale.sale_date)).scalar())
    _add(Expense.query.with_entities(func.min(Expense.expense_date)).scalar())
    _add(Payment.query.with_entities(func.min(func.date(Payment.payment_date))).scalar())
    _add(ItemPurchaseLog.query.with_entities(func.min(ItemPurchaseLog.entry_date)).scalar())
    _add(StockEntry.query.with_entities(func.min(StockEntry.entry_date)).scalar())
    _add(VendorPayment.query.with_entities(func.min(VendorPayment.payment_date)).scalar())
    _add(DailyCashCount.query.with_entities(func.min(DailyCashCount.count_date)).scalar())

    return min(candidates) if candidates else today


def parse_form_date(raw, default=None):
    """Parse YYYY-MM-DD from a form field; fall back to today (UTC) or default."""
    today = datetime.utcnow().date()
    if default is None:
        default = today
    raw = (raw or '').strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return default


def datetime_from_date(d, hour=12):
    """Build a datetime for storing on DateTime columns from a date.

    Same-day entries use the current clock time so multiple purchases
    on one day stay separate and ordered in purchase logs.
    """
    now = datetime.utcnow()
    if d == now.date():
        return datetime.combine(d, now.time().replace(microsecond=0))
    return datetime.combine(d, time(hour=hour, minute=0, second=0))


def parse_period(args):
    """Resolve start/end dates from request args. Returns (period, start_date, end_date)."""
    today = datetime.utcnow().date()
    period = (args.get('period') or 'all').strip().lower()
    if period not in dict(PERIOD_CHOICES):
        period = 'all'

    start = end = today

    if period == 'today':
        start = end = today
    elif period == '7days':
        start = today - timedelta(days=6)
    elif period == 'month':
        start = today.replace(day=1)
    elif period == '6months':
        start = today - timedelta(days=182)
    elif period == 'year':
        start = today.replace(month=1, day=1)
    elif period == 'all':
        # Full history: first recorded activity → today
        start = earliest_activity_date()
        end = today
    elif period == 'custom':
        start_raw = (args.get('start_date') or '').strip()
        end_raw = (args.get('end_date') or '').strip()
        try:
            start = datetime.strptime(start_raw, '%Y-%m-%d').date() if start_raw else today
        except ValueError:
            start = today
        try:
            end = datetime.strptime(end_raw, '%Y-%m-%d').date() if end_raw else today
        except ValueError:
            end = today
        if start > end:
            start, end = end, start

    return period, start, end


def fuel_rate_for(fuel_type_id, FuelPrice):
    latest = (
        FuelPrice.query.filter_by(fuel_type_id=fuel_type_id)
        .order_by(FuelPrice.created_at.desc())
        .first()
    )
    return float(latest.price_per_liter) if latest else 0.0


def paginate(query_or_list, page, per_page=15):
    """
    Paginate a SQLAlchemy query or a plain list.
    Returns (items, pagination_dict).
    """
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    per_page = max(int(per_page or 15), 1)

    # SQLAlchemy query / pagination
    if hasattr(query_or_list, 'paginate'):
        pagination = query_or_list.paginate(page=page, per_page=per_page, error_out=False)
        return pagination.items, {
            'page': pagination.page,
            'pages': pagination.pages or 1,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'has_prev': pagination.has_prev,
            'has_next': pagination.has_next,
            'prev_num': pagination.prev_num,
            'next_num': pagination.next_num,
        }

    items = list(query_or_list or [])
    total = len(items)
    pages = max((total + per_page - 1) // per_page, 1)
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], {
        'page': page,
        'pages': pages,
        'per_page': per_page,
        'total': total,
        'has_prev': page > 1,
        'has_next': page < pages,
        'prev_num': page - 1,
        'next_num': page + 1,
    }


def compute_period_stats(start, end, models, include_opening_credit=False):
    """
    Aggregate petrol/diesel/other/credit/expense/cash for [start, end].
    models: namespace with MeterReading, FuelType, FuelPrice, CreditSale, Expense, Payment, DailyCashCount

    include_opening_credit: when True (e.g. period=All), unpaid opening/previous-book
    credit is included in period_credit for display. Cash-in-hand still excludes opening.
    """
    MeterReading = models.MeterReading
    FuelType = models.FuelType
    FuelPrice = models.FuelPrice
    CreditSale = models.CreditSale
    Expense = models.Expense
    Payment = models.Payment
    DailyCashCount = models.DailyCashCount
    Customer = models.Customer

    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()

    # Latest rates once (avoid per-fuel FuelPrice queries)
    rate_by_fuel = {}
    for fp in FuelPrice.query.order_by(FuelPrice.created_at.desc()).all():
        if fp.fuel_type_id not in rate_by_fuel:
            rate_by_fuel[fp.fuel_type_id] = float(fp.price_per_liter or 0)

    # All meter readings in range — single query
    meter_rows = MeterReading.query.filter(
        MeterReading.reading_date >= start,
        MeterReading.reading_date <= end,
        MeterReading.closing_reading.isnot(None),
    ).all()
    liters_by_fuel = {}
    for r in meter_rows:
        liters_by_fuel[r.fuel_type_id] = liters_by_fuel.get(r.fuel_type_id, 0.0) + float(r.liters_sold or 0)

    by_fuel = {}
    meter_total = 0.0
    meter_liters = 0.0
    petrol_sale = diesel_sale = 0.0
    petrol_liters = diesel_liters = 0.0

    for ft in fuel_types:
        liters = liters_by_fuel.get(ft.id, 0.0)
        rate = rate_by_fuel.get(ft.id, 0.0)
        amount = liters * rate
        by_fuel[ft.id] = {'fuel': ft, 'liters': liters, 'rate': rate, 'amount': amount}
        meter_total += amount
        meter_liters += liters
        name = ft.name.lower()
        if 'petrol' in name:
            petrol_sale += amount
            petrol_liters += liters
        elif 'diesel' in name:
            diesel_sale += amount
            diesel_liters += liters

    entries = CreditSale.query.filter(
        CreditSale.sale_date >= start,
        CreditSale.sale_date <= end,
    ).all()

    other_sale = 0.0
    other_cash = 0.0
    fuel_credit = 0.0
    other_credit = 0.0
    ft_sale = 0.0
    ft_cash = 0.0
    ft_credit = 0.0
    ft_liters = 0.0
    advances = 0.0
    loans = 0.0
    sale_cash_paid = 0.0
    period_credit_sales = 0.0
    opening_credit = 0.0

    for e in entries:
        amt = float(e.amount or 0)
        paid = float(e.amount_paid or 0)
        status = (e.payment_status or 'unpaid').lower()
        # Legacy rows: paid status with amount_paid still 0
        if status == 'paid' and paid <= 0:
            paid = amt
        credit = max(amt - paid, 0.0)
        et = (e.entry_type or 'sale').lower()

        if et == 'advance':
            advances += amt
            continue
        if et == 'loan':
            loans += amt
            continue
        if et == 'opening':
            # Opening / previous book credit — excluded from cash-in-hand.
            # Included in Credit KPI only when include_opening_credit=True (All).
            opening_credit += credit
            continue

        # sale
        period_credit_sales += credit
        sale_cash_paid += paid
        if e.other_item_id:
            oi = e.other_item
            if oi and oi.category == 'ft_mobile':
                ft_sale += amt
                ft_cash += paid
                ft_credit += credit
                ft_liters += float(e.liters or 0)
            else:
                other_sale += amt
                other_cash += paid
                other_credit += credit
        elif e.fuel_type_id:
            fuel_credit += credit

    expenses = Expense.query.filter(
        Expense.expense_date >= start,
        Expense.expense_date <= end,
    ).order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
    expense_total = sum(float(x.amount or 0) for x in expenses)
    unsettled_expense_total = sum(
        float(x.amount or 0) for x in expenses if not getattr(x, 'is_settled', False)
    )
    settled_expense_total = expense_total - unsettled_expense_total

    # Money returned to till on settle date (may differ from expense_date).
    expense_settlements = Expense.query.filter(
        Expense.is_settled.is_(True),
        Expense.settled_date.isnot(None),
        Expense.settled_date >= start,
        Expense.settled_date <= end,
    ).order_by(Expense.settled_date.desc(), Expense.id.desc()).all()
    expense_return_total = sum(float(x.amount or 0) for x in expense_settlements)

    payments = Payment.query.filter(
        func.date(Payment.payment_date) >= start,
        func.date(Payment.payment_date) <= end,
    ).all()
    payments_total = sum(float(p.amount_paid or 0) for p in payments)

    # Purchases in period (cost × liters/qty)
    from app.models import ItemPurchaseLog, StockEntry, VendorPayment
    from app.vendors.service import purchase_log_total
    purchase_total = 0.0
    purchase_start = datetime.combine(start, time.min)
    purchase_end = datetime.combine(end, time.max)
    purchase_logs = ItemPurchaseLog.query.filter(
        ItemPurchaseLog.entry_date >= purchase_start,
        ItemPurchaseLog.entry_date <= purchase_end,
    ).order_by(ItemPurchaseLog.entry_date.desc(), ItemPurchaseLog.id.desc()).all()
    for log in purchase_logs:
        purchase_total += purchase_log_total(log)

    # Only fall back to StockEntry if there are no fuel purchase logs at all (legacy)
    legacy_stock_entries = []
    has_fuel_purchase_log = ItemPurchaseLog.query.filter_by(category='fuel').first() is not None
    if not has_fuel_purchase_log:
        legacy_stock_entries = StockEntry.query.filter(
            StockEntry.entry_date >= purchase_start,
            StockEntry.entry_date <= purchase_end,
        ).order_by(StockEntry.entry_date.desc(), StockEntry.id.desc()).all()
        for entry in legacy_stock_entries:
            purchase_total += float(entry.cost_per_liter or 0) * float(entry.liters_added or 0)

    vendor_payments = VendorPayment.query.filter(
        func.date(VendorPayment.payment_date) >= start,
        func.date(VendorPayment.payment_date) <= end,
    ).order_by(VendorPayment.payment_date.desc(), VendorPayment.id.desc()).all()
    vendor_payments_total = sum(float(p.amount_paid or 0) for p in vendor_payments)

    # Total sale = fuel (meter) + other items + FT Mobile Oil
    total_sale = meter_total + other_sale + ft_sale

    # Activity credit only (excludes opening) — used for cash-in-hand
    activity_credit = fuel_credit + other_credit + ft_credit + loans

    # Credit KPI: optionally include opening (All filter)
    period_credit = activity_credit + (opening_credit if include_opening_credit else 0.0)

    # Deposits = customer advances + payments collected
    deposits = advances + payments_total

    # Cash in hand = sales + deposits − credit − expenses (out on expense_date)
    #              + settled returns (in on settled_date)
    # Vendor payments are tracked in the journal only — they do not affect till cash.
    # Opening book credit must never reduce cash-in-hand.
    cash_in_hand = (
        total_sale + deposits - activity_credit - expense_total + expense_return_total
    )

    cash_counts = DailyCashCount.query.filter(
        DailyCashCount.count_date >= start,
        DailyCashCount.count_date <= end,
    ).all()
    counted_cash = sum(float(c.cash_in_hand or 0) for c in cash_counts)
    if start == end:
        day_count = DailyCashCount.query.filter_by(count_date=start).first()
        counted_cash = float(day_count.cash_in_hand) if day_count else None
        cash_variance = (counted_cash - cash_in_hand) if counted_cash is not None else 0.0
    else:
        cash_variance = (counted_cash - cash_in_hand) if cash_counts else 0.0

    outstanding = float(
        Customer.query.with_entities(
            func.coalesce(func.sum(Customer.current_balance_due), 0)
        ).scalar() or 0
    )

    return {
        'by_fuel': by_fuel,
        'meter_total': meter_total,
        'meter_liters': meter_liters,
        'petrol_sale': petrol_sale,
        'petrol_liters': petrol_liters,
        'diesel_sale': diesel_sale,
        'diesel_liters': diesel_liters,
        'other_sale': other_sale,
        'other_cash': other_cash,
        'other_credit': other_credit,
        'ft_sale': ft_sale,
        'ft_cash': ft_cash,
        'ft_credit': ft_credit,
        'ft_liters': ft_liters,
        'fuel_credit': fuel_credit,
        'opening_credit': opening_credit,
        'activity_credit': activity_credit,
        'period_credit': period_credit,
        'period_credit_sales': period_credit_sales,
        'sale_cash_paid': sale_cash_paid,
        'advances': advances,
        'loans': loans,
        'deposits': deposits,
        'total_sale': total_sale,
        'expense_total': expense_total,
        'unsettled_expense_total': unsettled_expense_total,
        'settled_expense_total': settled_expense_total,
        'expense_return_total': expense_return_total,
        'expenses': expenses,
        'expense_settlements': expense_settlements,
        'payments_total': payments_total,
        'purchase_total': purchase_total,
        'purchase_logs': purchase_logs,
        'legacy_stock_entries': legacy_stock_entries,
        'vendor_payments': vendor_payments,
        'vendor_payments_total': vendor_payments_total,
        'expected_cash': cash_in_hand,
        'cash_in_hand': cash_in_hand,
        'counted_cash': counted_cash if counted_cash is not None else 0.0,
        'cash_variance': cash_variance,
        'outstanding_credit': outstanding,
        'entries': entries,
        'payments': payments,
        'cash_counts': cash_counts,
    }


def _purchase_log_description(log):
    """Human-readable line for a purchase log row."""
    cost = float(log.cost_price or 0)
    if log.category == 'fuel':
        qty_desc = f"{float(log.liters or 0):,.2f} L"
    elif log.category == 'ft_mobile':
        qty_desc = f"{float(log.liters or 0):,.2f} L"
    else:
        qty_desc = f"{int(log.quantity or 0)} pcs"
    desc = f"{log.item_name} — {qty_desc} @ PKR {cost:,.2f}"
    if log.company:
        desc = f"{log.company} {desc}"
    return desc


def _purchase_log_qty(log):
    if log.category in ('fuel', 'ft_mobile'):
        return float(log.liters or 0)
    return float(log.quantity or 0)


def _overpayment_note_item(text):
    """Extract item label from 'Overpayment from sale (Diesel) …' notes."""
    import re
    m = re.match(r'^overpayment from sale\s*\((.+?)\)', (text or '').strip(), re.IGNORECASE)
    return m.group(1).strip().lower() if m else None


def infer_sale_overpayments(entries, payments=None):
    """
    Map credit_sale.id -> overpayment amount using stored overpayment and/or
    linked Payment / Advance rows created from sale overpay.
    """
    payments = payments or []
    over_by_key = {}  # (customer_id, date, item) -> amount

    for pay in payments:
        item = _overpayment_note_item(pay.note)
        if not item or not pay.customer_id:
            continue
        pay_dt = pay.payment_date
        sale_date = pay_dt.date() if hasattr(pay_dt, 'date') else pay_dt
        key = (pay.customer_id, sale_date, item)
        over_by_key[key] = over_by_key.get(key, 0.0) + float(pay.amount_paid or 0)

    for e in entries or []:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        if et != 'advance':
            continue
        item = _overpayment_note_item(getattr(e, 'remarks', None))
        if not item or not e.customer_id:
            continue
        key = (e.customer_id, e.sale_date, item)
        over_by_key[key] = over_by_key.get(key, 0.0) + float(e.amount or 0)

    result = {}
    for e in entries or []:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        if et != 'sale':
            continue
        stored = float(getattr(e, 'overpayment', 0) or 0)
        if stored > 0:
            result[e.id] = stored
            continue
        if not e.customer_id:
            continue
        key = (e.customer_id, e.sale_date, (e.item_name or '').strip().lower())
        inferred = float(over_by_key.get(key, 0.0) or 0.0)
        if inferred > 0:
            result[e.id] = inferred
    return result


def build_period_cash_entries(stats):
    """
    Unify sales, customer/vendor cash, expenses, purchases, and settles into one
    activity list for Sales / Dashboard cash journals (same period as compute_period_stats).
    """
    from app.vendors.service import purchase_log_total

    entries = stats.get('entries') or []
    payments = stats.get('payments') or []
    sale_overs = infer_sale_overpayments(entries, payments)

    absorbed_payment_ids = set()
    absorbed_advance_ids = set()
    for pay in payments:
        if _overpayment_note_item(pay.note):
            absorbed_payment_ids.add(pay.id)
    for e in entries:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        if et == 'advance' and _overpayment_note_item(getattr(e, 'remarks', None)):
            absorbed_advance_ids.add(e.id)

    rows = []
    for e in entries:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        remarks = (getattr(e, 'remarks', None) or '').strip()
        item_name = e.item_name if hasattr(e, 'item_name') else ''

        # Skip advance rows already shown as sale overpayment
        if et == 'advance' and e.id in absorbed_advance_ids:
            continue

        display_type = et
        if et == 'advance' and remarks.lower().startswith('overpayment'):
            display_type = 'overpay'
            item_name = remarks
        elif remarks and et in ('advance', 'loan', 'sale'):
            item_name = f'{item_name} — {remarks}' if item_name else remarks

        paid = float(getattr(e, 'amount_paid', 0) or 0)
        over = float(sale_overs.get(e.id, 0.0) if et == 'sale' else 0.0)
        if et == 'sale' and over <= 0:
            over = float(getattr(e, 'overpayment', 0) or 0)
        amt = float(getattr(e, 'amount', 0) or 0)
        status = (getattr(e, 'payment_status', None) or 'unpaid').lower()
        if status == 'paid' and paid <= 0:
            paid = amt
        credit = max(amt - paid, 0.0)

        if et == 'advance':
            credit = 0.0
            paid = amt
            over = 0.0
            status = 'paid'
        elif et == 'loan':
            credit = amt
            paid = 0.0
            over = 0.0
            status = 'unpaid'
        elif et == 'sale' and over > 0:
            status = 'overpay'
            display_type = 'sale'

        other_amt = over if over > 0 else credit
        paid_display = (paid + over) if et == 'sale' else paid

        rows.append(SimpleNamespace(
            id=e.id,
            sale_date=e.sale_date,
            entry_type=display_type,
            customer=getattr(e, 'customer', None),
            item_name=item_name,
            liters=float(getattr(e, 'liters', 0) or 0),
            amount=amt,
            discount=float(getattr(e, 'discount', 0) or 0),
            amount_paid=paid_display,
            overpayment=over,
            credit_amount=credit,
            other_amount=other_amt,
            other_kind='over' if over > 0 else ('due' if credit > 0 else 'none'),
            payment_status=status,
            cash_direction='in' if et in ('advance',) or paid_display > 0 else 'none',
            is_fuel=bool(getattr(e, 'is_fuel', False) or getattr(e, 'fuel_type_id', None)),
            other_item=getattr(e, 'other_item', None),
        ))

    for pay in payments:
        # Already rolled into the sale Paid/Other columns
        if pay.id in absorbed_payment_ids:
            continue
        pay_dt = pay.payment_date
        sale_date = pay_dt.date() if hasattr(pay_dt, 'date') else pay_dt
        method = (pay.method or 'Cash').strip()
        note = (pay.note or '').strip()
        item_name = f'Payment · {method}'
        display_type = 'payment'
        if note:
            item_name = f'{item_name} — {note}'
        amt = float(pay.amount_paid or 0)
        rows.append(SimpleNamespace(
            id=pay.id,
            sale_date=sale_date,
            entry_type=display_type,
            customer=pay.customer,
            item_name=item_name,
            liters=0,
            amount=amt,
            discount=0,
            amount_paid=amt,
            overpayment=0,
            credit_amount=0,
            other_amount=0,
            other_kind='none',
            payment_status='paid',
            cash_direction='in',
            is_fuel=False,
            other_item=None,
        ))

    for log in stats.get('purchase_logs') or []:
        amt = purchase_log_total(log)
        entry_dt = log.entry_date
        sale_date = entry_dt.date() if hasattr(entry_dt, 'date') else entry_dt
        vendor = getattr(log, 'vendor_ref', None)
        rows.append(SimpleNamespace(
            id=log.id,
            sale_date=sale_date,
            entry_type='purchase',
            customer=None,
            vendor=vendor,
            vendor_name=(vendor.name if vendor else (log.vendor or '—')),
            item_name=_purchase_log_description(log),
            liters=_purchase_log_qty(log),
            amount=amt,
            discount=0,
            amount_paid=0,
            overpayment=0,
            credit_amount=amt,
            other_amount=amt,
            other_kind='due',
            payment_status='payable',
            cash_direction='none',
            is_fuel=(log.category == 'fuel'),
            other_item=None,
            purchase_category=log.category,
        ))

    for entry in stats.get('legacy_stock_entries') or []:
        amt = float(entry.cost_per_liter or 0) * float(entry.liters_added or 0)
        entry_dt = entry.entry_date
        sale_date = entry_dt.date() if hasattr(entry_dt, 'date') else entry_dt
        vendor = getattr(entry, 'vendor_ref', None)
        fuel_name = entry.fuel_type.name if getattr(entry, 'fuel_type', None) else 'Fuel'
        rows.append(SimpleNamespace(
            id=entry.id,
            sale_date=sale_date,
            entry_type='purchase',
            customer=None,
            vendor=vendor,
            vendor_name=(vendor.name if vendor else (entry.supplier or '—')),
            item_name=f"{fuel_name} — {float(entry.liters_added or 0):,.2f} L @ PKR {float(entry.cost_per_liter or 0):,.2f}",
            liters=float(entry.liters_added or 0),
            amount=amt,
            discount=0,
            amount_paid=0,
            overpayment=0,
            credit_amount=amt,
            other_amount=amt,
            other_kind='due',
            payment_status='payable',
            cash_direction='none',
            is_fuel=True,
            other_item=None,
            purchase_category='fuel',
        ))

    for vp in stats.get('vendor_payments') or []:
        pay_dt = vp.payment_date
        sale_date = pay_dt.date() if hasattr(pay_dt, 'date') else pay_dt
        method = (vp.method or 'Cash').strip()
        note = (vp.note or '').strip()
        item_name = f'Payment · {method}'
        if note:
            item_name = f'{item_name} — {note}'
        amt = float(vp.amount_paid or 0)
        vendor = getattr(vp, 'vendor', None)
        rows.append(SimpleNamespace(
            id=vp.id,
            sale_date=sale_date,
            entry_type='vendor_pay',
            customer=None,
            vendor=vendor,
            vendor_name=(vendor.name if vendor else '—'),
            item_name=item_name,
            liters=0,
            amount=amt,
            discount=0,
            amount_paid=amt,
            overpayment=0,
            credit_amount=0,
            other_amount=0,
            other_kind='none',
            payment_status='paid',
            cash_direction='out',
            is_fuel=False,
            other_item=None,
        ))

    for exp in stats.get('expenses') or []:
        amt = float(exp.amount or 0)
        desc = (exp.description or '').strip()
        item_name = exp.name
        if desc:
            item_name = f'{item_name} — {desc}'
        is_settled = bool(getattr(exp, 'is_settled', False))
        rows.append(SimpleNamespace(
            id=exp.id,
            sale_date=exp.expense_date,
            entry_type='expense',
            customer=None,
            item_name=item_name,
            liters=0,
            amount=amt,
            discount=0,
            amount_paid=0,
            overpayment=0,
            credit_amount=amt,
            other_amount=amt,
            other_kind='due',
            payment_status='settled' if is_settled else 'open',
            cash_direction='out',
            is_fuel=False,
            other_item=None,
        ))

    for exp in stats.get('expense_settlements') or []:
        amt = float(exp.amount or 0)
        note = (getattr(exp, 'settle_note', None) or '').strip()
        item_name = f'Settle return · {exp.name}'
        if note:
            item_name = f'{item_name} — {note}'
        rows.append(SimpleNamespace(
            id=exp.id,
            sale_date=exp.settled_date,
            entry_type='settle',
            customer=None,
            item_name=item_name,
            liters=0,
            amount=amt,
            discount=0,
            amount_paid=amt,
            overpayment=0,
            credit_amount=0,
            other_amount=0,
            other_kind='none',
            payment_status='settled',
            cash_direction='in',
            is_fuel=False,
            other_item=None,
        ))

    def _sort_key(row):
        d = getattr(row, 'sale_date', None)
        return (d or datetime.min.date(), getattr(row, 'id', 0) or 0)

    rows.sort(key=_sort_key, reverse=True)
    return rows


def build_cash_journal_summary(stats):
    """
    Top counters for the cash journal: cash in hand, expenses, settles,
    paid sale cash, partial dues, advances, and customer payments.
    """
    paid_sale_cash = 0.0
    partial_paid = 0.0
    partial_due = 0.0
    unpaid_due = 0.0

    for e in stats.get('entries') or []:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        if et != 'sale':
            continue
        amt = float(e.amount or 0)
        paid = float(e.amount_paid or 0)
        status = (e.payment_status or 'unpaid').lower()
        if status == 'paid' and paid <= 0:
            paid = amt
        credit = max(amt - paid, 0.0)
        if status == 'paid':
            paid_sale_cash += paid
        elif status == 'partial':
            partial_paid += paid
            partial_due += credit
        else:
            unpaid_due += credit

    return {
        'cash_in_hand': float(stats.get('cash_in_hand') or 0),
        'expense_total': float(stats.get('expense_total') or 0),
        'expense_return_total': float(stats.get('expense_return_total') or 0),
        'purchase_total': float(stats.get('purchase_total') or 0),
        'vendor_payments_total': float(stats.get('vendor_payments_total') or 0),
        'paid_sale_cash': paid_sale_cash,
        'partial_paid': partial_paid,
        'partial_due': partial_due,
        'unpaid_due': unpaid_due,
        'advances': float(stats.get('advances') or 0),
        'payments_total': float(stats.get('payments_total') or 0),
        'loans': float(stats.get('loans') or 0),
    }
