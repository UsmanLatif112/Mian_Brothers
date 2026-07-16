"""Shared helpers for date ranges and period financial stats."""
from datetime import datetime, timedelta, time
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
        ItemPurchaseLog, StockEntry, DailyCashCount,
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

    payments = Payment.query.filter(
        func.date(Payment.payment_date) >= start,
        func.date(Payment.payment_date) <= end,
    ).all()
    payments_total = sum(float(p.amount_paid or 0) for p in payments)

    # Purchases in period (cost × liters/qty)
    from app.models import ItemPurchaseLog, StockEntry
    purchase_total = 0.0
    purchase_start = datetime.combine(start, time.min)
    purchase_end = datetime.combine(end, time.max)
    purchase_logs = ItemPurchaseLog.query.filter(
        ItemPurchaseLog.entry_date >= purchase_start,
        ItemPurchaseLog.entry_date <= purchase_end,
    ).all()
    for log in purchase_logs:
        cost = float(log.cost_price or 0)
        if log.category in ('fuel', 'ft_mobile'):
            purchase_total += cost * float(log.liters or 0)
        else:
            purchase_total += cost * float(log.quantity or 0)

    # Only fall back to StockEntry if there are no fuel purchase logs at all (legacy)
    has_fuel_purchase_log = any(log.category == 'fuel' for log in purchase_logs)
    if not has_fuel_purchase_log:
        for entry in StockEntry.query.filter(
            StockEntry.entry_date >= purchase_start,
            StockEntry.entry_date <= purchase_end,
        ).all():
            purchase_total += float(entry.cost_per_liter or 0) * float(entry.liters_added or 0)

    # Total sale = fuel (meter) + other items + FT Mobile Oil
    total_sale = meter_total + other_sale + ft_sale

    # Activity credit only (excludes opening) — used for cash-in-hand
    activity_credit = fuel_credit + other_credit + ft_credit + loans

    # Credit KPI: optionally include opening (All filter)
    period_credit = activity_credit + (opening_credit if include_opening_credit else 0.0)

    # Deposits = customer advances + payments collected
    deposits = advances + payments_total

    # Cash in hand = (fuel + other + FT + deposits) − activity credit − expenses
    # Opening book credit must never reduce cash-in-hand.
    cash_in_hand = total_sale + deposits - activity_credit - expense_total

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
        'expenses': expenses,
        'payments_total': payments_total,
        'purchase_total': purchase_total,
        'expected_cash': cash_in_hand,
        'cash_in_hand': cash_in_hand,
        'counted_cash': counted_cash if counted_cash is not None else 0.0,
        'cash_variance': cash_variance,
        'outstanding_credit': outstanding,
        'entries': entries,
        'payments': payments,
        'cash_counts': cash_counts,
    }
