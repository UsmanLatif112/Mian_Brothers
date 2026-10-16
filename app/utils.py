"""Shared helpers for date ranges and period financial stats."""
from datetime import date, datetime, timedelta, time
from types import SimpleNamespace
from sqlalchemy import func


PERIOD_CHOICES = (
    ('today', 'Today'),
    ('7days', 'Last 7 Days'),
    ('month', 'This Month'),
    ('6months', 'Last 6 Months'),
    ('year', 'This Year'),
    ('all', 'All time'),
    ('custom', 'Custom Dates'),
)

WORKING_DATE_SESSION_KEY = 'working_date'


def calendar_today():
    """Wall-clock date (UTC)."""
    return datetime.utcnow().date()


def business_today():
    """Operational “today” — working date from session, else calendar today."""
    try:
        from flask import has_request_context, session
        if has_request_context():
            raw = session.get(WORKING_DATE_SESSION_KEY)
            if raw:
                return datetime.strptime(str(raw)[:10], '%Y-%m-%d').date()
    except (RuntimeError, ValueError, TypeError):
        pass
    return calendar_today()


def expense_settle_remark(name, settle_note=None):
    """Customer-ledger remark for expense settled to customer credit (not a cash loan)."""
    remark = f'Settle — {(name or "").strip() or "expense"}'
    note = (settle_note or '').strip()
    if note:
        remark = f'{remark} — {note}'
    return remark


def is_expense_settle_remark(remarks):
    """True when CreditSale remarks mark an expense→customer credit settle."""
    r = (remarks or '').strip().lower()
    if not r:
        return False
    return (
        r.startswith('expense settle')
        or r.startswith('settle —')
        or r.startswith('settle -')
        or r.startswith('settle –')
    )


def set_working_date(day):
    from flask import session
    if day is None:
        session.pop(WORKING_DATE_SESSION_KEY, None)
        return
    if isinstance(day, datetime):
        day = day.date()
    session[WORKING_DATE_SESSION_KEY] = day.isoformat()


def earliest_activity_date():
    """Earliest business date across sales, meters, expenses, payments, purchases."""
    from app.models import (
        MeterReading, CreditSale, Expense, Payment,
        ItemPurchaseLog, StockEntry, DailyCashCount, VendorPayment,
        CashTaken, DailyTillBalance,
    )
    from app.tenancy import apply_agency_filter

    # Use calendar today as fallback — working date must not shrink “All time”
    today = calendar_today()
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

    def _min(q, model, col):
        return apply_agency_filter(q.with_entities(func.min(col)), model).scalar()

    _add(_min(MeterReading.query, MeterReading, MeterReading.reading_date))
    _add(_min(CreditSale.query, CreditSale, CreditSale.sale_date))
    _add(_min(Expense.query, Expense, Expense.expense_date))
    _add(_min(Payment.query, Payment, func.date(Payment.payment_date)))
    _add(_min(ItemPurchaseLog.query, ItemPurchaseLog, ItemPurchaseLog.entry_date))
    _add(_min(StockEntry.query, StockEntry, StockEntry.entry_date))
    _add(_min(VendorPayment.query, VendorPayment, VendorPayment.payment_date))
    _add(_min(DailyCashCount.query, DailyCashCount, DailyCashCount.count_date))
    _add(_min(CashTaken.query, CashTaken, CashTaken.taken_date))
    _add(_min(DailyTillBalance.query, DailyTillBalance, DailyTillBalance.balance_date))

    return min(candidates) if candidates else today


def parse_form_date(raw, default=None):
    """Parse YYYY-MM-DD from a form field; fall back to working date or default."""
    today = business_today()
    if default is None:
        default = today
    raw = (raw or '').strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return default


def parse_form_datetime(raw, default=None):
    """Parse YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM[:SS] from a form field."""
    clock = datetime.utcnow().replace(microsecond=0)
    today = business_today()
    if default is None:
        default = datetime.combine(today, clock.time())
    raw = (raw or '').strip()
    if not raw:
        return default
    raw = raw.replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == '%Y-%m-%d':
                if parsed.date() == today:
                    return datetime.combine(today, clock.time())
                return datetime.combine(parsed.date(), time.min)
            return parsed
        except ValueError:
            continue
    return default


def datetime_from_date(d, hour=12):
    """Build a datetime for storing on DateTime columns from a date.

    Same-day entries use the current clock time so multiple purchases
    on one day stay separate and ordered in purchase logs.
    """
    now = datetime.utcnow()
    if d == business_today():
        return datetime.combine(d, now.time().replace(microsecond=0))
    return datetime.combine(d, time(hour=hour, minute=0, second=0))


def parse_period(args, default_period='all'):
    """Resolve start/end dates from request args. Returns (period, start_date, end_date)."""
    today = business_today()
    period = (args.get('period') or default_period).strip().lower()
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
        # Full history through calendar today (working date must not cut the end)
        start = earliest_activity_date()
        end = max(calendar_today(), today)
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


def _as_plain_date(val):
    """Normalize datetime/date/str to date or None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        try:
            return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return val


def _as_plain_datetime(val):
    """Normalize datetime/date/str to datetime or None.

    Plain dates become end-of-day so a day-level lookup includes that day's prices.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(microsecond=0)
    if isinstance(val, date) and not isinstance(val, datetime):
        return datetime.combine(val, time(23, 59, 59))
    if isinstance(val, str):
        raw = val.strip().replace('T', ' ')
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt == '%Y-%m-%d':
                    return datetime.combine(parsed.date(), time(23, 59, 59))
                return parsed
            except ValueError:
                continue
        return None
    return None


def _fuel_price_effective_at(fp):
    """Resolve the moment a FuelPrice row becomes active."""
    if getattr(fp, 'effective_at', None):
        return fp.effective_at.replace(microsecond=0)
    day = _as_plain_date(fp.effective_date) or _as_plain_date(fp.created_at)
    if day is None:
        return None
    if fp.created_at and hasattr(fp.created_at, 'time'):
        return datetime.combine(day, fp.created_at.time().replace(microsecond=0))
    return datetime.combine(day, time.min)


def build_fuel_rate_lookup(FuelPrice, include_shared=False):
    """
    Load all FuelPrice rows once and return rate(fuel_type_id, as_of=None).

    Pass the FuelPrice model, not an already-filtered query.
    as_of (date or datetime): latest price with effective_at <= as_of.
    None: current pump price (latest effective_at <= now).
    Uses bisect — O(log n) per lookup, not a full scan.
    """
    import bisect
    from collections import defaultdict
    from app.tenancy import apply_agency_filter

    by_fuel = defaultdict(list)
    prices = apply_agency_filter(
        FuelPrice.query, FuelPrice, include_shared=include_shared
    ).order_by(FuelPrice.id.asc())
    for fp in prices.all():
        at = _fuel_price_effective_at(fp)
        if at is None:
            continue
        by_fuel[fp.fuel_type_id].append((at, float(fp.price_per_liter or 0), fp.id))

    # Parallel arrays for bisect: times[i] pairs with prices[i]
    indexed = {}
    for fuel_id, entries in by_fuel.items():
        entries.sort(key=lambda x: (x[0], x[2]))
        indexed[fuel_id] = (
            [e[0] for e in entries],
            [e[1] for e in entries],
        )

    def rate(fuel_type_id, as_of_date=None):
        pair = indexed.get(fuel_type_id)
        if not pair:
            return 0.0
        times, prices = pair
        as_of = _as_plain_datetime(as_of_date)
        if as_of is None:
            as_of = datetime.utcnow().replace(microsecond=0)
        i = bisect.bisect_right(times, as_of) - 1
        if i >= 0:
            return prices[i]
        return prices[0]

    return rate


def fuel_rate_for(fuel_type_id, FuelPrice, as_of_date=None):
    """
    Selling rate for a fuel type.

    as_of_date: date or datetime — price effective at that moment
    (effective_at <= as_of). Same calendar day can have multiple rates.
    None: current pump price (latest effective_at <= now).

    Within a request, reuses one cached lookup (no per-call full table scan).
    """
    try:
        from flask import g, has_request_context
        if has_request_context():
            cache = getattr(g, '_of_fuel_rate_lookup', None)
            if cache is None:
                g._of_fuel_rate_lookup = build_fuel_rate_lookup(FuelPrice)
                cache = g._of_fuel_rate_lookup
            return cache(fuel_type_id, as_of_date)
    except RuntimeError:
        pass
    return build_fuel_rate_lookup(FuelPrice)(fuel_type_id, as_of_date)


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

    include_opening_credit: when True, unpaid opening/previous-book credit is included
    in period_credit (customer-ledger Credit KPI). Cash-in-hand still excludes opening.
    """
    MeterReading = models.MeterReading
    FuelType = models.FuelType
    FuelPrice = models.FuelPrice
    CreditSale = models.CreditSale
    Expense = models.Expense
    Payment = models.Payment
    DailyCashCount = models.DailyCashCount
    Customer = models.Customer

    from app.tenancy import apply_agency_filter

    fuel_types = apply_agency_filter(FuelType.query, FuelType, include_shared=True).order_by(FuelType.name.asc()).all()
    rate_as_of = build_fuel_rate_lookup(FuelPrice)

    # All meter readings in range — value each row at its reading-date rate
    from sqlalchemy.orm import joinedload
    meter_rows = apply_agency_filter(
        MeterReading.query.options(
            joinedload(MeterReading.fuel_type),
            joinedload(MeterReading.machine),
        ).filter(
            MeterReading.reading_date >= start,
            MeterReading.reading_date <= end,
            MeterReading.closing_reading.isnot(None),
        ),
        MeterReading,
    ).order_by(MeterReading.reading_date.asc(), MeterReading.id.asc()).all()
    liters_by_fuel = {}
    amount_by_fuel = {}
    for r in meter_rows:
        liters = float(r.liters_sold or 0)
        stored = getattr(r, 'sale_rate', None)
        rate = float(stored) if stored is not None else rate_as_of(r.fuel_type_id, r.reading_date)
        liters_by_fuel[r.fuel_type_id] = liters_by_fuel.get(r.fuel_type_id, 0.0) + liters
        amount_by_fuel[r.fuel_type_id] = amount_by_fuel.get(r.fuel_type_id, 0.0) + (liters * rate)

    by_fuel = {}
    meter_total = 0.0
    meter_liters = 0.0
    petrol_sale = diesel_sale = 0.0
    petrol_liters = diesel_liters = 0.0

    for ft in fuel_types:
        liters = liters_by_fuel.get(ft.id, 0.0)
        amount = amount_by_fuel.get(ft.id, 0.0)
        # Display rate: weighted average when period spans multiple prices
        rate = (amount / liters) if liters > 0 else rate_as_of(ft.id, end)
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

    entries = apply_agency_filter(
        CreditSale.query.options(
            joinedload(CreditSale.other_item),
            joinedload(CreditSale.fuel_type),
        ).filter(
            CreditSale.sale_date >= start,
            CreditSale.sale_date <= end,
        ),
        CreditSale,
    ).all()

    other_sale = 0.0
    other_cash = 0.0
    fuel_credit = 0.0
    fuel_cash_paid = 0.0
    other_credit = 0.0
    ft_sale = 0.0
    ft_cash = 0.0
    ft_credit = 0.0
    ft_liters = 0.0
    ft_cash_liters = 0.0
    ft_credit_liters = 0.0
    petrol_paid = diesel_paid = 0.0
    petrol_credit = diesel_credit = 0.0
    petrol_credit_liters = diesel_credit_liters = 0.0
    # Pump value of unpaid credit liters (at sale rate) — used so discount
    # reduces credit/total only, and does not inflate cash.
    petrol_credit_gross = diesel_credit_gross = 0.0
    advances = 0.0
    loans = 0.0
    expense_settle_credit = 0.0
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
        liters = float(getattr(e, 'liters', 0) or 0)
        sale_rate = float(getattr(e, 'rate', 0) or 0)
        paid_liters = liters if amt <= 0 or paid >= amt else liters * (paid / amt)
        credit_liters = 0.0 if amt <= 0 else liters * (credit / amt)
        et = (e.entry_type or 'sale').lower()

        if et == 'advance':
            advances += amt
            continue
        if et == 'loan':
            # Expense→customer credit settle: receivable on ledger (not a cash loan).
            if is_expense_settle_remark(getattr(e, 'remarks', None)):
                expense_settle_credit += credit if credit > 0 else amt
                continue
            loans += amt
            continue
        if et == 'opening':
            # Opening / previous book credit — excluded from cash-in-hand.
            opening_credit += credit
            continue

        # sale
        period_credit_sales += credit
        sale_cash_paid += paid
        if e.other_item_id:
            oi = e.other_item
            # FT Mobile Oil is sold via Other Sale — counts in other_* KPIs.
            other_sale += amt
            other_cash += paid
            other_credit += credit
            if oi and oi.category == 'ft_mobile':
                ft_sale += amt
                ft_cash += paid
                ft_credit += credit
                ft_liters += liters
                ft_cash_liters += paid_liters
                ft_credit_liters += credit_liters

        elif e.fuel_type_id:
            fuel_credit += credit
            fuel_cash_paid += paid
            fuel_name = (e.fuel_type.name if getattr(e, 'fuel_type', None) else '').lower()
            credit_gross = credit_liters * sale_rate
            if 'petrol' in fuel_name:
                petrol_paid += paid
                petrol_credit += credit
                petrol_credit_liters += credit_liters
                petrol_credit_gross += credit_gross
            elif 'diesel' in fuel_name:
                diesel_paid += paid
                diesel_credit += credit
                diesel_credit_liters += credit_liters
                diesel_credit_gross += credit_gross

    # Cash = meter pump value of non-credit liters (at pump rate).
    # Do NOT use (meter − discounted credit) — discount must not move into cash.
    petrol_cash_liters = max(petrol_liters - petrol_credit_liters, 0.0)
    diesel_cash_liters = max(diesel_liters - diesel_credit_liters, 0.0)
    petrol_cash = max(petrol_sale - petrol_credit_gross, 0.0)
    diesel_cash = max(diesel_sale - diesel_credit_gross, 0.0)
    total_cash_sale = petrol_cash + diesel_cash
    total_cash_liters = petrol_cash_liters + diesel_cash_liters
    petrol_credit_discount = max(petrol_credit_gross - petrol_credit, 0.0)
    diesel_credit_discount = max(diesel_credit_gross - diesel_credit, 0.0)
    petrol_total = petrol_cash + petrol_credit
    petrol_total_liters = petrol_cash_liters + petrol_credit_liters
    diesel_total = diesel_cash + diesel_credit
    diesel_total_liters = diesel_cash_liters + diesel_credit_liters

    def _expense_is_settled(row):
        """Treat settled_date as source of truth (flag can be wrong on some DBs)."""
        if getattr(row, 'settled_date', None):
            return True
        flag = getattr(row, 'is_settled', False)
        return bool(flag) and str(flag).lower() not in ('0', 'false', 'none')

    expenses = apply_agency_filter(
        Expense.query.filter(
            Expense.expense_date >= start,
            Expense.expense_date <= end,
        ),
        Expense,
    ).order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
    expense_total = sum(float(x.amount or 0) for x in expenses)
    unsettled_expense_total = sum(
        float(x.amount or 0) for x in expenses if not _expense_is_settled(x)
    )
    settled_expense_total = expense_total - unsettled_expense_total

    # Settles in period by settled_date (may differ from expense_date).
    expense_settlements = apply_agency_filter(
        Expense.query.filter(
            Expense.settled_date.isnot(None),
            Expense.settled_date >= start,
            Expense.settled_date <= end,
        ),
        Expense,
    ).order_by(Expense.settled_date.desc(), Expense.id.desc()).all()
    expense_settlements = [x for x in expense_settlements if _expense_is_settled(x)]

    def _settle_method(row):
        method = (getattr(row, 'settle_method', None) or 'cash').strip().lower()
        # Linked FKs win over a stale settle_method flag.
        if (
            method == 'vendor'
            or getattr(row, 'settle_vendor_id', None)
            or getattr(row, 'settle_purchase_log_id', None)
        ):
            return 'vendor'
        if (
            method == 'credit'
            or getattr(row, 'settle_customer_id', None)
            or getattr(row, 'settle_credit_sale_id', None)
        ):
            return 'credit'
        return 'cash'

    # Cash returns only — these reverse till drain.
    expense_return_total = sum(
        float(x.amount or 0) for x in expense_settlements if _settle_method(x) == 'cash'
    )
    # Credit / vendor settles — recover expense for P&L, no till movement.
    expense_credit_settle_total = sum(
        float(x.amount or 0) for x in expense_settlements if _settle_method(x) == 'credit'
    )
    expense_vendor_settle_total = sum(
        float(x.amount or 0) for x in expense_settlements if _settle_method(x) == 'vendor'
    )
    # P&L / “net expense cost” after all recoveries.
    expense_net = (
        expense_total
        - expense_return_total
        - expense_credit_settle_total
        - expense_vendor_settle_total
    )
    # Till still holds cash-settled returns only (credit/vendor settle do not refill till).
    expense_cash_net = expense_total - expense_return_total

    payments = apply_agency_filter(
        Payment.query.filter(
            func.date(Payment.payment_date) >= start,
            func.date(Payment.payment_date) <= end,
        ),
        Payment,
    ).all()
    payments_total = sum(float(p.amount_paid or 0) for p in payments)

    # Purchases in period (cost × liters/qty); skip opening stock + expense settles.
    from app.models import ItemPurchaseLog, StockEntry, VendorPayment
    from app.vendors.service import (
        purchase_log_total, counts_toward_purchase_kpis, purchase_log_payment_status,
    )
    purchase_total = 0.0
    purchase_paid_total = 0.0
    purchase_start = datetime.combine(start, time.min)
    purchase_end = datetime.combine(end, time.max)
    purchase_logs = apply_agency_filter(
        ItemPurchaseLog.query.filter(
            ItemPurchaseLog.entry_date >= purchase_start,
            ItemPurchaseLog.entry_date <= purchase_end,
        ),
        ItemPurchaseLog,
    ).order_by(ItemPurchaseLog.entry_date.desc(), ItemPurchaseLog.id.desc()).all()
    for log in purchase_logs:
        if not counts_toward_purchase_kpis(log):
            continue
        amt = purchase_log_total(log)
        purchase_total += amt
        if purchase_log_payment_status(log) == 'paid':
            purchase_paid_total += amt

    # Only fall back to StockEntry if there are no fuel purchase logs at all (legacy)
    legacy_stock_entries = []
    has_fuel_purchase_log = apply_agency_filter(
        ItemPurchaseLog.query.filter_by(category='fuel'), ItemPurchaseLog
    ).first() is not None
    if not has_fuel_purchase_log:
        legacy_stock_entries = apply_agency_filter(
            StockEntry.query.filter(
                StockEntry.entry_date >= purchase_start,
                StockEntry.entry_date <= purchase_end,
            ),
            StockEntry,
        ).order_by(StockEntry.entry_date.desc(), StockEntry.id.desc()).all()
        for entry in legacy_stock_entries:
            purchase_total += float(entry.cost_per_liter or 0) * float(entry.liters_added or 0)

    from app.vendors.service import payment_funding_source

    vendor_payments = apply_agency_filter(
        VendorPayment.query.filter(
            func.date(VendorPayment.payment_date) >= start,
            func.date(VendorPayment.payment_date) <= end,
        ),
        VendorPayment,
    ).order_by(VendorPayment.payment_date.desc(), VendorPayment.id.desc()).all()
    vendor_payments_total = sum(float(p.amount_paid or 0) for p in vendor_payments)
    vendor_payments_till_total = sum(
        float(p.amount_paid or 0) for p in vendor_payments
        if payment_funding_source(p) == 'till'
    )
    vendor_payments_account_total = sum(
        float(p.amount_paid or 0) for p in vendor_payments
        if payment_funding_source(p) == 'account'
    )

    # Total sale = fuel (meter) + other items (includes FT Mobile Oil)
    total_sale = meter_total + other_sale

    # Activity credit (sales unpaid + cash loans) — cash-in-hand uses loans separately.
    activity_credit = fuel_credit + other_credit + loans

    # Credit KPI = full customer-ledger receivables in period:
    # unpaid sales + loans + expense settles + (optional) opening balances.
    period_credit = (
        activity_credit
        + expense_settle_credit
        + (opening_credit if include_opening_credit else 0.0)
    )

    # Deposits = customer advances + payments collected
    deposits = advances + payments_total

    from app.account.service import previous_balance_for_date, cash_taken_total, cash_taken_rows
    previous_balance = previous_balance_for_date(start)
    total_receiving = deposits

    # Cash in hand = cash sales + previous balance + customer receiving
    #                − customer cash loans − expense cash net (out − cash settles)
    #                − vendor payments funded from till.
    # Account-funded vendor pays use the cash-taken pool — till unchanged.
    cash_in_hand = (
        petrol_cash + diesel_cash + other_cash
        + previous_balance + total_receiving
        - loans - expense_cash_net
        - vendor_payments_till_total
    )
    # Same as cash in hand, but expenses (and expense settles) excluded.
    cash_in_hand_wo_expense = (
        petrol_cash + diesel_cash + other_cash
        + previous_balance + total_receiving
        - loans
        - vendor_payments_till_total
    )
    # Period cash only: no expenses, no previous balance (keeps sales, receiving, loans).
    cash_in_hand_wo_expense_prev = (
        petrol_cash + diesel_cash + other_cash
        + total_receiving
        - loans
        - vendor_payments_till_total
    )
    cash_taken_amount = cash_taken_total(start, end)
    remaining_balance = cash_in_hand - cash_taken_amount

    cash_counts = apply_agency_filter(
        DailyCashCount.query.filter(
            DailyCashCount.count_date >= start,
            DailyCashCount.count_date <= end,
        ),
        DailyCashCount,
    ).all()
    counted_cash = sum(float(c.cash_in_hand or 0) for c in cash_counts)
    if start == end:
        day_count = apply_agency_filter(
            DailyCashCount.query.filter_by(count_date=start), DailyCashCount
        ).first()
        counted_cash = float(day_count.cash_in_hand) if day_count else None
        cash_variance = (counted_cash - cash_in_hand) if counted_cash is not None else 0.0
    else:
        cash_variance = (counted_cash - cash_in_hand) if cash_counts else 0.0

    outstanding = float(
        apply_agency_filter(
            Customer.query.with_entities(
                func.coalesce(func.sum(Customer.current_balance_due), 0)
            ),
            Customer,
        ).scalar() or 0
    )

    return {
        'by_fuel': by_fuel,
        'meter_total': meter_total,
        'meter_liters': meter_liters,
        'petrol_sale': petrol_sale,
        'petrol_liters': petrol_liters,
        'petrol_total': petrol_total,
        'petrol_total_liters': petrol_total_liters,
        'petrol_cash': petrol_cash,
        'petrol_cash_liters': petrol_cash_liters,
        'petrol_credit': petrol_credit,
        'petrol_credit_liters': petrol_credit_liters,
        'petrol_credit_gross': petrol_credit_gross,
        'petrol_credit_discount': petrol_credit_discount,
        'diesel_sale': diesel_sale,
        'diesel_liters': diesel_liters,
        'diesel_total': diesel_total,
        'diesel_total_liters': diesel_total_liters,
        'diesel_cash': diesel_cash,
        'diesel_cash_liters': diesel_cash_liters,
        'total_cash_sale': total_cash_sale,
        'total_cash_liters': total_cash_liters,
        'diesel_credit': diesel_credit,
        'diesel_credit_liters': diesel_credit_liters,
        'diesel_credit_gross': diesel_credit_gross,
        'diesel_credit_discount': diesel_credit_discount,
        'other_sale': other_sale,
        'other_cash': other_cash,
        'other_credit': other_credit,
        'ft_sale': ft_sale,
        'ft_cash': ft_cash,
        'ft_credit': ft_credit,
        'ft_liters': ft_liters,
        'ft_cash_liters': ft_cash_liters,
        'ft_credit_liters': ft_credit_liters,
        'fuel_credit': fuel_credit,
        'fuel_cash_paid': fuel_cash_paid,
        'opening_credit': opening_credit,
        'expense_settle_credit': expense_settle_credit,
        'activity_credit': activity_credit,
        'period_credit': period_credit,
        'period_credit_sales': period_credit_sales,
        'sale_cash_paid': sale_cash_paid,
        'advances': advances,
        'loans': loans,
        'deposits': deposits,
        'total_receiving': total_receiving,
        'previous_balance': previous_balance,
        'cash_taken_total': cash_taken_amount,
        'remaining_balance': remaining_balance,
        'total_sale': total_sale,
        'expense_total': expense_total,
        'unsettled_expense_total': unsettled_expense_total,
        'settled_expense_total': settled_expense_total,
        'expense_return_total': expense_return_total,
        'expense_credit_settle_total': expense_credit_settle_total,
        'expense_vendor_settle_total': expense_vendor_settle_total,
        'expense_cash_net': expense_cash_net,
        'expense_net': expense_net,
        'expenses': expenses,
        'expense_settlements': expense_settlements,
        'payments_total': payments_total,
        'purchase_total': purchase_total,
        'purchase_paid_total': purchase_paid_total,
        'purchase_logs': purchase_logs,
        'legacy_stock_entries': legacy_stock_entries,
        'vendor_payments': vendor_payments,
        'vendor_payments_total': vendor_payments_total,
        'vendor_payments_till_total': vendor_payments_till_total,
        'vendor_payments_account_total': vendor_payments_account_total,
        'expected_cash': cash_in_hand,
        'cash_in_hand': cash_in_hand,
        'cash_in_hand_wo_expense': cash_in_hand_wo_expense,
        'cash_in_hand_wo_expense_prev': cash_in_hand_wo_expense_prev,
        'counted_cash': counted_cash if counted_cash is not None else 0.0,
        'cash_variance': cash_variance,
        'outstanding_credit': outstanding,
        'entries': entries,
        'payments': payments,
        'cash_counts': cash_counts,
        'cash_taken_rows': cash_taken_rows(start, end),
        'meter_rows': meter_rows,
        'start_date': start,
        'end_date': end,
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


def build_sales_price_bands(stats):
    """
    Sales “by price” summary — one row per date + fuel + rate.

    All machines at the same rate are combined into one liters/sale line.
    A later meter segment after a price change becomes a new rate row.
    Credit is matched by date/fuel/rate; cash = sale − credit gross.
    """
    from collections import defaultdict
    from app.models import FuelPrice

    rate_as_of = build_fuel_rate_lookup(FuelPrice, include_shared=True)

    def _near(a, b, eps=0.02):
        return abs(float(a or 0) - float(b or 0)) <= eps

    def _plain_day(val):
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        return val

    def _rate_key(rate):
        return round(float(rate or 0), 2)

    # Aggregate meters: (day, fuel_id, rate_key) → liters + meta
    band_map = {}  # key -> SimpleNamespace
    band_order = []  # first-seen order within day

    for r in stats.get('meter_rows') or []:
        liters = float(getattr(r, 'liters_sold', 0) or 0)
        if liters <= 0:
            continue
        day = _plain_day(getattr(r, 'reading_date', None))
        if day is None:
            continue
        stored = getattr(r, 'sale_rate', None)
        rate = float(stored) if stored is not None else float(
            rate_as_of(r.fuel_type_id, day) or 0
        )
        rk = _rate_key(rate)
        key = (day, r.fuel_type_id, rk)
        ft = getattr(r, 'fuel_type', None)
        if key not in band_map:
            band_map[key] = SimpleNamespace(
                date=day,
                fuel_type_id=r.fuel_type_id,
                fuel_name=(ft.name if ft else 'Fuel'),
                rate=rate,
                liters=0.0,
                sale=0.0,
                credit=0.0,
                credit_liters=0.0,
                credit_gross=0.0,
                cash=0.0,
                cash_liters=0.0,
                _sort_id=getattr(r, 'id', 0) or 0,
            )
            band_order.append(key)
        band = band_map[key]
        band.liters += liters
        band.sale += liters * rate
        band._sort_id = min(band._sort_id, getattr(r, 'id', 0) or 0)

    # Fuel credit sales
    credit_rows = []
    for e in stats.get('entries') or []:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        if et != 'sale' or not getattr(e, 'fuel_type_id', None):
            continue
        if getattr(e, 'other_item_id', None):
            continue
        day = _plain_day(getattr(e, 'sale_date', None))
        if day is None:
            continue
        amt = float(getattr(e, 'amount', 0) or 0)
        paid = float(getattr(e, 'amount_paid', 0) or 0)
        status = (getattr(e, 'payment_status', None) or 'unpaid').lower()
        if status == 'paid' and paid <= 0:
            paid = amt
        credit = max(amt - paid, 0.0)
        liters = float(getattr(e, 'liters', 0) or 0)
        sale_rate = float(getattr(e, 'rate', 0) or 0)
        credit_liters = 0.0 if amt <= 0 else liters * (credit / amt)
        credit_gross = credit_liters * sale_rate
        if credit_liters <= 0 and credit <= 0:
            continue
        credit_rows.append({
            'day': day,
            'fuel_type_id': e.fuel_type_id,
            'rate': sale_rate,
            'credit_liters': credit_liters,
            'credit': credit,
            'credit_gross': credit_gross,
        })

    bands_by_day_fuel = defaultdict(list)
    for key, band in band_map.items():
        bands_by_day_fuel[(band.date, band.fuel_type_id)].append(band)

    for c in credit_rows:
        candidates = bands_by_day_fuel.get((c['day'], c['fuel_type_id'])) or []
        if not candidates:
            continue
        matched = [b for b in candidates if _near(b.rate, c['rate'])]
        targets = matched or candidates
        total_L = sum(b.liters for b in targets) or 0.0
        if total_L <= 0:
            continue
        for b in targets:
            share = b.liters / total_L
            b.credit_liters += c['credit_liters'] * share
            b.credit += c['credit'] * share
            b.credit_gross += c['credit_gross'] * share

    def _fuel_sort_key(name):
        n = (name or '').lower()
        if 'petrol' in n:
            return (0, n)
        if 'diesel' in n:
            return (1, n)
        return (2, n)

    # day -> fuel_id -> fuel group
    day_fuel = {}
    for key in band_order:
        b = band_map[key]
        b.credit_liters = min(b.credit_liters, b.liters)
        b.cash_liters = max(b.liters - b.credit_liters, 0.0)
        b.credit_gross = min(b.credit_gross, b.sale)
        b.cash = max(b.sale - b.credit_gross, 0.0)

        if b.date not in day_fuel:
            day_fuel[b.date] = {}
        fuels = day_fuel[b.date]
        if b.fuel_type_id not in fuels:
            fuels[b.fuel_type_id] = SimpleNamespace(
                fuel_type_id=b.fuel_type_id,
                fuel_name=b.fuel_name,
                bands=[],
                total_liters=0.0,
                total_sale=0.0,
                total_cash=0.0,
                total_credit=0.0,
                _sort_id=b._sort_id,
            )
        fg = fuels[b.fuel_type_id]
        fg.bands.append(b)
        fg.total_liters += b.liters
        fg.total_sale += b.sale
        fg.total_cash += b.cash
        fg.total_credit += b.credit
        fg._sort_id = min(fg._sort_id, b._sort_id)

    days = []
    grand = SimpleNamespace(
        total_liters=0.0,
        total_sale=0.0,
        total_cash=0.0,
        total_credit=0.0,
    )
    for day in sorted(day_fuel.keys(), reverse=True):
        fuels = list(day_fuel[day].values())
        fuels.sort(key=lambda f: (_fuel_sort_key(f.fuel_name), f._sort_id))
        for fg in fuels:
            # Rate rows in chronological order (first meter segment first)
            fg.bands.sort(key=lambda x: (x._sort_id, x.rate))
            grand.total_liters += fg.total_liters
            grand.total_sale += fg.total_sale
            grand.total_cash += fg.total_cash
            grand.total_credit += fg.total_credit
        day_total = SimpleNamespace(
            liters=sum(f.total_liters for f in fuels),
            sale=sum(f.total_sale for f in fuels),
            cash=sum(f.total_cash for f in fuels),
            credit=sum(f.total_credit for f in fuels),
        )
        days.append(SimpleNamespace(date=day, fuels=fuels, day_total=day_total))

    return SimpleNamespace(days=days, grand=grand)


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
    from app.account.service import cash_taken_journal_rows, previous_balance_journal_row
    from app.models import FuelPrice

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
    prev_row = previous_balance_journal_row(stats)
    if prev_row:
        rows.append(prev_row)

    # Pump / meter cash sales (main fuel IN — not stored as CreditSale rows)
    rate_as_of = build_fuel_rate_lookup(FuelPrice, include_shared=True)
    for r in stats.get('meter_rows') or []:
        liters = float(getattr(r, 'liters_sold', 0) or 0)
        if liters <= 0:
            continue
        stored = getattr(r, 'sale_rate', None)
        rate = float(stored) if stored is not None else rate_as_of(
            r.fuel_type_id, getattr(r, 'reading_date', None)
        )
        amt = liters * float(rate or 0)
        if amt <= 0:
            continue
        ft = getattr(r, 'fuel_type', None)
        fuel_name = (ft.name if ft else 'Fuel').strip()
        rows.append(SimpleNamespace(
            id=getattr(r, 'id', 0),
            sale_date=getattr(r, 'reading_date', None),
            entry_type='sale',
            customer=None,
            item_name=f'Pump · {fuel_name} {liters:,.2f} L @ PKR {rate:,.2f}/L',
            liters=liters,
            amount=amt,
            discount=0,
            amount_paid=amt,
            overpayment=0,
            credit_amount=0,
            other_amount=0,
            other_kind='none',
            payment_status='paid',
            cash_direction='in',
            is_fuel=True,
            other_item=None,
        ))

    for e in entries:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        remarks = (getattr(e, 'remarks', None) or '').strip()
        item_name = e.item_name if hasattr(e, 'item_name') else ''

        # Customer opening account rows are not cash activity.
        if et == 'opening':
            continue

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
            # Expense→credit settle is on the customer ledger; skip cash journal
            # (expense out already posted; till must not move again).
            if is_expense_settle_remark(remarks):
                continue
            credit = amt
            paid = 0.0
            over = 0.0
            status = 'unpaid'
        elif et == 'sale' and over > 0:
            status = 'overpay'
            display_type = 'sale'

        other_amt = over if over > 0 else credit
        paid_display = (paid + over) if et == 'sale' else paid

        # Journal labels: sale / advance / loan keep paid-unpaid clarity
        if display_type == 'sale':
            if status == 'overpay':
                pay_label = 'Overpay'
            elif status == 'paid' or (paid >= amt - 0.02 and amt > 0):
                pay_label = 'Paid'
                status = 'paid'
            elif paid > 0:
                pay_label = 'Partial'
                status = 'partial'
            else:
                pay_label = 'Unpaid'
                status = 'unpaid'
            item_name = f'{item_name} · {pay_label}' if item_name else pay_label
        elif display_type == 'loan':
            item_name = f'{item_name} · Unpaid' if item_name else 'Unpaid'
            status = 'unpaid'
        elif display_type in ('advance', 'overpay'):
            item_name = f'{item_name} · Paid' if item_name else 'Paid'
            status = 'paid'

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
            cash_direction='in' if display_type in ('sale', 'advance', 'overpay') or et in ('advance',) or paid_display > 0 else ('out' if et == 'loan' else 'none'),
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

    AUTO_INV_PAY = 'Paid with inventory purchase:'
    vendor_payments = list(stats.get('vendor_payments') or [])
    absorbed_vendor_pay_ids = set()
    from app.vendors.service import purchase_log_payment_status
    from app.models import VendorPayment as _VendorPayment

    def _match_inventory_auto_payment(log, amount):
        # Prefer FK link (works even if payment_date falls outside the filter window)
        for vp in vendor_payments:
            if vp.id in absorbed_vendor_pay_ids:
                continue
            if getattr(vp, 'purchase_log_id', None) == log.id:
                return vp
        if getattr(log, 'id', None):
            linked = (
                _VendorPayment.query
                .filter_by(purchase_log_id=log.id)
                .order_by(_VendorPayment.id.asc())
                .first()
            )
            if linked and linked.id not in absorbed_vendor_pay_ids:
                return linked

        item = (log.item_name or '').strip().lower()
        for vp in vendor_payments:
            if vp.id in absorbed_vendor_pay_ids:
                continue
            note = (vp.note or '').strip()
            if not note.startswith(AUTO_INV_PAY):
                continue
            if abs(float(vp.amount_paid or 0) - amount) > 0.02:
                continue
            if item and item not in note.lower():
                continue
            return vp
        for vp in vendor_payments:
            if vp.id in absorbed_vendor_pay_ids:
                continue
            note = (vp.note or '').strip()
            if note.startswith(AUTO_INV_PAY) and abs(float(vp.amount_paid or 0) - amount) <= 0.02:
                return vp
        return None

    # Every inventory add = purchasing → journal OUT (paid or unpaid).
    # Paid-with-purchase auto payments are absorbed so we don't double-count.
    for log in stats.get('purchase_logs') or []:
        amt = purchase_log_total(log)
        if amt <= 0:
            continue
        auto_pay = _match_inventory_auto_payment(log, amt)
        status = purchase_log_payment_status(log)
        is_paid = status == 'paid' or auto_pay is not None
        if auto_pay:
            absorbed_vendor_pay_ids.add(auto_pay.id)
        entry_dt = log.entry_date or (auto_pay.payment_date if auto_pay else None)
        sale_date = entry_dt.date() if hasattr(entry_dt, 'date') else entry_dt
        vendor = getattr(log, 'vendor_ref', None)
        desc = _purchase_log_description(log)
        if is_paid:
            desc = f'{desc} · Paid'
        else:
            desc = f'{desc} · Unpaid'
        rows.append(SimpleNamespace(
            id=log.id,
            sale_date=sale_date,
            entry_type='purchase',
            customer=None,
            vendor=vendor,
            vendor_name=(vendor.name if vendor else (log.vendor or '—')),
            item_name=desc,
            liters=_purchase_log_qty(log),
            amount=amt,
            discount=0,
            amount_paid=amt,
            overpayment=0,
            credit_amount=0,
            other_amount=0,
            other_kind='none',
            payment_status='paid' if is_paid else 'unpaid',
            cash_direction='out',
            is_fuel=(log.category == 'fuel'),
            other_item=None,
            purchase_category=log.category,
        ))

    from app.vendors.service import payment_funding_source as _vp_funding

    for vp in vendor_payments:
        if vp.id in absorbed_vendor_pay_ids:
            continue
        pay_dt = vp.payment_date
        sale_date = pay_dt.date() if hasattr(pay_dt, 'date') else pay_dt
        method = (vp.method or 'Cash').strip()
        note = (vp.note or '').strip()
        funding = _vp_funding(vp)
        blob = f'{method} {note}'.lower()
        is_advance = 'advance' in blob
        entry_type = 'vendor_advance' if is_advance else 'vendor_pay'
        fund_label = 'Till' if funding == 'till' else 'Account'
        if is_advance:
            item_name = f'Advance · {method} · {fund_label}'
        else:
            item_name = f'Payment · {method} · {fund_label}'
        if note:
            item_name = f'{item_name} — {note}'
        amt = float(vp.amount_paid or 0)
        vendor = getattr(vp, 'vendor', None)
        rows.append(SimpleNamespace(
            id=vp.id,
            sale_date=sale_date,
            entry_type=entry_type,
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
            # Only till-funded pays move cash journal / till.
            cash_direction='out' if funding == 'till' else 'none',
            is_fuel=False,
            other_item=None,
        ))

    for exp in stats.get('expenses') or []:
        amt = float(exp.amount or 0)
        desc = (exp.description or '').strip()
        item_name = exp.name
        if desc:
            item_name = f'{item_name} — {desc}'
        is_settled = bool(getattr(exp, 'settled_date', None) or getattr(exp, 'is_settled', False))
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
        method = (getattr(exp, 'settle_method', None) or 'cash').strip().lower()
        if method in ('credit', 'vendor') or getattr(exp, 'settle_customer_id', None) or getattr(exp, 'settle_vendor_id', None):
            # No till movement — lives on customer receivable / vendor payable.
            continue
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

    rows.extend(cash_taken_journal_rows(stats))

    def _sort_key(row):
        d = getattr(row, 'sale_date', None)
        return (d or datetime.min.date(), getattr(row, 'id', 0) or 0)

    rows.sort(key=_sort_key, reverse=True)
    return rows


def build_cash_journal_summary(stats):
    """
    Top counters for the cash journal.
    """
    paid_sale_cash = 0.0
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
        else:
            unpaid_due += credit

    return {
        'cash_in_hand': float(stats.get('cash_in_hand') or 0),
        'expense_total': float(stats.get('expense_total') or 0),
        'expense_return_total': float(stats.get('expense_return_total') or 0),
        'expense_net': float(stats.get('expense_net') or 0),
        'purchase_total': float(stats.get('purchase_total') or 0),
        'purchase_paid_total': float(stats.get('purchase_paid_total') or 0),
        'vendor_payments_total': float(stats.get('vendor_payments_total') or 0),
        'paid_sale_cash': paid_sale_cash,
        'unpaid_due': unpaid_due,
        'advances': float(stats.get('advances') or 0),
        'payments_total': float(stats.get('payments_total') or 0),
        'loans': float(stats.get('loans') or 0),
        'previous_balance': float(stats.get('previous_balance') or 0),
        'cash_taken_total': float(stats.get('cash_taken_total') or 0),
        'remaining_balance': float(stats.get('remaining_balance') or 0),
    }
