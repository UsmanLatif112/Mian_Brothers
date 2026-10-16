from flask import render_template, jsonify, request, Response, flash, redirect, url_for
from flask_login import login_required
from app.dashboard import dashboard_bp
from app.models import (
    db, Sale, StockEntry, Customer, Inventory, FuelType, FuelPrice,
    OtherItem, MeterReading, CreditSale, Expense, Payment, DailyCashCount,
    ItemPurchaseLog, Vendor, ShopCategory,
)
from app.tenancy import apply_agency_filter
from app.utils import (
    parse_period, PERIOD_CHOICES, compute_period_stats, fuel_rate_for,
    build_fuel_rate_lookup, business_today, calendar_today, set_working_date,
    parse_form_date,
)
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import joinedload, load_only
from types import SimpleNamespace
import csv
import io


def _as_plain_date(val):
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


def _fuel_cost_timeline(as_of_end):
    """
    Cost history per fuel_type_id up to as_of_end (one load, bisect as-of).
    Prefers purchase logs; falls back to stock entries for fuels with no logs.
    """
    import bisect
    from collections import defaultdict

    end_dt = as_of_end
    if not isinstance(as_of_end, datetime):
        end_dt = datetime.combine(as_of_end, datetime.max.time())

    fuel_costs = defaultdict(list)
    for log in apply_agency_filter(
        ItemPurchaseLog.query.filter(
            ItemPurchaseLog.category == 'fuel',
            ItemPurchaseLog.fuel_type_id.isnot(None),
            ItemPurchaseLog.entry_date <= end_dt,
        ).options(
            load_only(
                ItemPurchaseLog.id,
                ItemPurchaseLog.fuel_type_id,
                ItemPurchaseLog.entry_date,
                ItemPurchaseLog.cost_price,
                ItemPurchaseLog.agency_id,
                ItemPurchaseLog.category,
            )
        ),
        ItemPurchaseLog,
    ).order_by(ItemPurchaseLog.entry_date.asc()).all():
        d = _as_plain_date(log.entry_date)
        if d is None:
            continue
        fuel_costs[log.fuel_type_id].append((d, float(log.cost_price or 0)))

    fuels_with_logs = set(fuel_costs.keys())
    for se in apply_agency_filter(
        StockEntry.query.filter(
            StockEntry.entry_date <= end_dt,
            StockEntry.fuel_type_id.isnot(None),
        ).options(
            load_only(
                StockEntry.id,
                StockEntry.fuel_type_id,
                StockEntry.entry_date,
                StockEntry.cost_per_liter,
                StockEntry.agency_id,
            )
        ),
        StockEntry,
    ).order_by(StockEntry.entry_date.asc()).all():
        if se.fuel_type_id in fuels_with_logs:
            continue
        d = _as_plain_date(se.entry_date)
        if d is None:
            continue
        fuel_costs[se.fuel_type_id].append((d, float(se.cost_per_liter or 0)))

    indexed = {}
    for fuel_id, entries in fuel_costs.items():
        entries.sort(key=lambda x: x[0])
        indexed[fuel_id] = (
            [e[0] for e in entries],
            [e[1] for e in entries],
        )

    def fuel_cost_as_of(fuel_type_id, sale_date):
        pair = indexed.get(fuel_type_id)
        if not pair:
            return 0.0
        dates, costs = pair
        sale_date = _as_plain_date(sale_date)
        if sale_date is None:
            return costs[-1]
        i = bisect.bisect_right(dates, sale_date) - 1
        if i >= 0:
            return costs[i]
        return costs[0]

    return fuel_cost_as_of


def get_cost_for_sale(fuel_type_id, sale_date):
    """Latest fuel cost as-of date — uses request-cached timeline when possible."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            as_of = _as_plain_date(sale_date) or business_today()
            cache_key = '_of_fuel_cost_as_of'
            end_key = '_of_fuel_cost_end'
            fn = getattr(g, cache_key, None)
            cached_end = getattr(g, end_key, None)
            if fn is None or cached_end is None or as_of > cached_end:
                g._of_fuel_cost_as_of = _fuel_cost_timeline(as_of)
                g._of_fuel_cost_end = as_of
                fn = g._of_fuel_cost_as_of
            return fn(fuel_type_id, as_of)
    except RuntimeError:
        pass
    return _fuel_cost_timeline(_as_plain_date(sale_date) or business_today())(
        fuel_type_id, sale_date
    )


def _other_item_costs():
    """id → cost_price only (no full ORM hydration of every column)."""
    rows = apply_agency_filter(
        db.session.query(OtherItem.id, OtherItem.cost_price),
        OtherItem,
    ).all()
    return {oid: float(cost or 0) for oid, cost in rows}


def _build_meter_trend(start, end):
    """
    Profit for the selected period:

      Gross = Sale value − COGS
      Net   = Gross − Expenses   ← Profit KPI / chart

    Include in sales: meter fuel, other/shop, FT, credit sales.
    Exclude: advance, loan, opening due.
    """
    from collections import defaultdict

    if start > end:
        start, end = end, start
    span = (end - start).days + 1

    # Selling rates as-of each reading date (not latest only)
    rate_as_of = build_fuel_rate_lookup(FuelPrice)
    fuel_cost_as_of = _fuel_cost_timeline(end)
    item_costs = _other_item_costs()
    as_date = _as_plain_date

    day_sales = defaultdict(float)
    day_cogs = defaultdict(float)
    day_gross = defaultdict(float)
    day_expenses = defaultdict(float)

    # 1) Meter / pump fuel
    readings = apply_agency_filter(
        MeterReading.query.options(joinedload(MeterReading.fuel_type)).filter(
            MeterReading.reading_date >= start,
            MeterReading.reading_date <= end,
            MeterReading.closing_reading.isnot(None),
        ),
        MeterReading,
    ).all()
    for reading in readings:
        d = as_date(reading.reading_date)
        if d is None:
            continue
        liters = float(reading.liters_sold or 0)
        stored = getattr(reading, 'sale_rate', None)
        rate = float(stored) if stored is not None else rate_as_of(reading.fuel_type_id, d)
        cost = fuel_cost_as_of(reading.fuel_type_id, d)
        sale_val = liters * rate
        cogs_val = liters * cost
        day_sales[d] += sale_val
        day_cogs[d] += cogs_val
        day_gross[d] += sale_val - cogs_val

    # 2) Credit / other / FT sales — skip advance/loan/opening
    entries = apply_agency_filter(
        CreditSale.query.options(
            joinedload(CreditSale.fuel_type),
            joinedload(CreditSale.other_item),
        ).filter(
            CreditSale.sale_date >= start,
            CreditSale.sale_date <= end,
        ),
        CreditSale,
    ).all()
    for e in entries:
        et = (e.entry_type or 'sale').lower()
        if et in ('advance', 'loan', 'opening'):
            continue

        d = as_date(e.sale_date)
        if d is None:
            continue
        sale_val = float(e.amount or 0)
        qty = float(e.liters or 0)
        cogs_val = 0.0

        if e.fuel_type_id:
            cogs_val = qty * fuel_cost_as_of(e.fuel_type_id, d)
        elif e.other_item_id:
            unit_cost = item_costs.get(e.other_item_id, 0.0)
            cogs_val = qty * unit_cost

        day_sales[d] += sale_val
        day_cogs[d] += cogs_val
        day_gross[d] += sale_val - cogs_val

    # 3) Period expenses (by expense_date)
    for exp in apply_agency_filter(
        Expense.query.filter(
            Expense.expense_date >= start,
            Expense.expense_date <= end,
        ),
        Expense,
    ).all():
        d = as_date(exp.expense_date)
        if d is not None:
            day_expenses[d] += float(exp.amount or 0)

    day_net = defaultdict(float)
    for d in set(day_gross) | set(day_expenses):
        day_net[d] = day_gross.get(d, 0.0) - day_expenses.get(d, 0.0)

    total_sales = round(sum(day_sales.values()), 2)
    total_cogs = round(sum(day_cogs.values()), 2)
    total_gross = round(sum(day_gross.values()), 2)
    total_expenses = round(sum(day_expenses.values()), 2)
    total_profit = round(total_gross - total_expenses, 2)

    labels = []
    date_labels = []
    sales_trend = []
    profit_trend = []
    expense_trend = []
    gross_trend = []
    cogs_trend = []
    grain = 'day'

    # Cap empty day walks — long “All” ranges used to keep the worker busy.
    # Aggregate from activity days only for week/month grains.
    active_days = sorted(set(day_sales) | set(day_net) | set(day_expenses) | set(day_gross) | set(day_cogs))

    if span <= 62:
        grain = 'day'
        cursor = start
        # Hard cap: never emit more than 62 points even if span is wrong
        points = 0
        while cursor <= end and points < 62:
            labels.append(cursor.strftime('%b %d'))
            date_labels.append(cursor.strftime('%Y-%m-%d (%a)'))
            sales_trend.append(round(day_sales.get(cursor, 0.0), 2))
            profit_trend.append(round(day_net.get(cursor, 0.0), 2))
            expense_trend.append(round(day_expenses.get(cursor, 0.0), 2))
            gross_trend.append(round(day_gross.get(cursor, 0.0), 2))
            cogs_trend.append(round(day_cogs.get(cursor, 0.0), 2))
            cursor += timedelta(days=1)
            points += 1
    elif span <= 400:
        grain = 'week'
        # Bucket active days into ISO weeks — O(activity), not O(calendar days)
        buckets = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, None, None])
        for d in active_days:
            if d < start or d > end:
                continue
            week_start = d - timedelta(days=d.weekday())
            if week_start < start:
                week_start = start
            b = buckets[week_start]
            b[0] += day_sales.get(d, 0.0)
            b[1] += day_net.get(d, 0.0)
            b[2] += day_expenses.get(d, 0.0)
            b[3] += day_gross.get(d, 0.0)
            b[4] += day_cogs.get(d, 0.0)
            b[5] = d if b[5] is None else min(b[5], d)
            b[6] = d if b[6] is None else max(b[6], d)
        for week_start in sorted(buckets):
            s, p, ex, g, c, d0, d1 = buckets[week_start]
            week_end = min(week_start + timedelta(days=6), end)
            if d0 == d1:
                labels.append(d0.strftime('%b %d'))
                date_labels.append(d0.strftime('%Y-%m-%d (%a)'))
            else:
                labels.append(f"{week_start.strftime('%b %d')}–{week_end.strftime('%b %d')}")
                date_labels.append(
                    f"{week_start.strftime('%Y-%m-%d')} → {week_end.strftime('%Y-%m-%d')}"
                )
            sales_trend.append(round(s, 2))
            profit_trend.append(round(p, 2))
            expense_trend.append(round(ex, 2))
            gross_trend.append(round(g, 2))
            cogs_trend.append(round(c, 2))
    else:
        grain = 'month'
        # Month buckets from activity days only — no while True calendar walk
        buckets = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
        for d in active_days:
            if d < start or d > end:
                continue
            key = (d.year, d.month)
            b = buckets[key]
            b[0] += day_sales.get(d, 0.0)
            b[1] += day_net.get(d, 0.0)
            b[2] += day_expenses.get(d, 0.0)
            b[3] += day_gross.get(d, 0.0)
            b[4] += day_cogs.get(d, 0.0)
        for (y, m) in sorted(buckets):
            s, p, ex, g, c = buckets[(y, m)]
            month_start = datetime(y, m, 1).date()
            if m == 12:
                month_end = datetime(y + 1, 1, 1).date() - timedelta(days=1)
            else:
                month_end = datetime(y, m + 1, 1).date() - timedelta(days=1)
            range_start = max(month_start, start)
            range_end = min(month_end, end)
            labels.append(range_start.strftime('%b %Y'))
            date_labels.append(
                f"{range_start.strftime('%Y-%m-%d')} → {range_end.strftime('%Y-%m-%d')}"
            )
            sales_trend.append(round(s, 2))
            profit_trend.append(round(p, 2))
            expense_trend.append(round(ex, 2))
            gross_trend.append(round(g, 2))
            cogs_trend.append(round(c, 2))

    return {
        'labels': labels,
        'date_labels': date_labels,
        'sales_trend': sales_trend,
        'profit_trend': profit_trend,
        'expense_trend': expense_trend,
        'gross_trend': gross_trend,
        'cogs_trend': cogs_trend,
        'total_sales': total_sales,
        'total_cogs': total_cogs,
        'total_gross': total_gross,
        'total_expenses': total_expenses,
        'total_profit': total_profit,
        'grain': grain,
        'start': start.isoformat(),
        'end': end.isoformat(),
    }


def _models_ns():
    return SimpleNamespace(
        MeterReading=MeterReading,
        FuelType=FuelType,
        FuelPrice=FuelPrice,
        CreditSale=CreditSale,
        Expense=Expense,
        Payment=Payment,
        DailyCashCount=DailyCashCount,
        Customer=Customer,
    )


@dashboard_bp.route('/')
@login_required
def index():
    # Set / clear operational “today” for the whole app (session)
    if (request.args.get('clear_working_date') or '').strip() in ('1', 'true', 'yes'):
        set_working_date(None)
        args = request.args.to_dict(flat=True)
        args.pop('clear_working_date', None)
        args.pop('working_date', None)
        if 'period' not in args:
            args['period'] = 'all'
        return redirect(url_for('dashboard.index', **args))

    wd_raw = (request.args.get('working_date') or '').strip()
    if wd_raw:
        try:
            day = datetime.strptime(wd_raw[:10], '%Y-%m-%d').date()
        except ValueError:
            day = parse_form_date(wd_raw)
        # Same as calendar day → stay live (don’t lock yesterday after midnight)
        if day == calendar_today():
            set_working_date(None)
        else:
            set_working_date(day)
        args = request.args.to_dict(flat=True)
        args.pop('working_date', None)
        # After setting a working day, jump to that day’s “Today” view
        if 'period' not in request.args:
            args['period'] = 'today'
        else:
            args['period'] = request.args.get('period') or 'all'
        return redirect(url_for('dashboard.index', **args))

    if not request.args:
        return redirect(url_for('dashboard.index', period='all'))

    period, start, end = parse_period(request.args, default_period='all')
    stats = compute_period_stats(
        start, end, _models_ns(),
        include_opening_credit=(period == 'all'),
    )

    # Same calculation as the Revenue & Margin chart (period total)
    meter_trend = _build_meter_trend(start, end)
    total_profit = meter_trend['total_profit']
    total_gross = meter_trend['total_gross']
    total_cogs = meter_trend['total_cogs']
    total_expenses_profit = meter_trend['total_expenses']

    DRY_THRESHOLD = 100.0
    inventory_items = (
        apply_agency_filter(
            Inventory.query.options(joinedload(Inventory.fuel_type)),
            Inventory,
        ).all()
    )
    stock_summary = {}
    petrol_stock = diesel_stock = None
    for inv in inventory_items:
        ft = inv.fuel_type
        name = ft.name if ft else f'Fuel #{inv.fuel_type_id}'
        current = float(inv.current_stock_liters or 0)
        stock_summary[name] = current
        if name.lower() == 'petrol':
            petrol_stock = current
        elif name.lower() == 'diesel':
            diesel_stock = current

    dry_message = None
    petrol_dry = petrol_stock is not None and petrol_stock < DRY_THRESHOLD
    diesel_dry = diesel_stock is not None and diesel_stock < DRY_THRESHOLD
    if petrol_dry and diesel_dry:
        dry_message = 'Fuel Dry'
    elif petrol_dry:
        dry_message = 'Petrol Dry'
    elif diesel_dry:
        dry_message = 'Diesel Dry'

    recent_deliveries = (
        apply_agency_filter(
            StockEntry.query.options(joinedload(StockEntry.fuel_type)),
            StockEntry,
        )
        .order_by(StockEntry.entry_date.desc())
        .limit(5)
        .all()
    )
    # Stock cards only need items with qty/liters > 0 (not every dormant SKU)
    other_items = apply_agency_filter(OtherItem.query, OtherItem).filter(
        db.or_(
            OtherItem.quantity > 0,
            OtherItem.liters > 0,
        )
    ).order_by(OtherItem.name.asc()).all()

    today = business_today()
    unit_by_key = {
        key: (unit or 'qty')
        for key, unit in apply_agency_filter(
            db.session.query(ShopCategory.key, ShopCategory.unit_mode),
            ShopCategory,
        ).all()
    }
    total_stock_value = 0.0
    # One cost timeline for all fuel stock rows (not get_cost_for_sale per row)
    fuel_cost_as_of = _fuel_cost_timeline(today)
    for inv in inventory_items:
        liters = float(inv.current_stock_liters or 0)
        if liters <= 0:
            continue
        cost = fuel_cost_as_of(inv.fuel_type_id, today)
        total_stock_value += liters * float(cost or 0)
    for item in other_items:
        cost = float(item.cost_price or 0)
        cat_key = item.category or 'other'
        unit_mode = unit_by_key.get(cat_key) or ('liters' if cat_key == 'ft_mobile' else 'qty')
        if unit_mode == 'liters' or cat_key == 'ft_mobile':
            qty = float(item.liters or 0)
        else:
            qty = float(item.quantity or 0)
        if qty > 0:
            total_stock_value += qty * cost
    total_stock_value = round(total_stock_value, 2)

    insights = _dashboard_insights(start, end, stock_summary, other_items)

    return render_template(
        'dashboard/index.html',
        stats=stats,
        total_profit=total_profit,
        total_gross=total_gross,
        total_cogs=total_cogs,
        total_expenses_profit=total_expenses_profit,
        total_stock_value=total_stock_value,
        stock_summary=stock_summary,
        dry_message=dry_message,
        petrol_stock=petrol_stock,
        diesel_stock=diesel_stock,
        dry_threshold=DRY_THRESHOLD,
        recent_deliveries=recent_deliveries,
        other_items=other_items,
        insights=insights,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
    )


def _dashboard_insights(start, end, stock_summary, other_items):
    """Pie-card datasets for dashboard insight cards — period-scoped, no full-table dumps."""
    from collections import defaultdict

    fuel_labels = []
    fuel_values = []
    for name, liters in sorted((stock_summary or {}).items(), key=lambda x: -float(x[1] or 0)):
        qty = float(liters or 0)
        if qty <= 0:
            continue
        fuel_labels.append(name)
        fuel_values.append(round(qty, 2))

    other_labels = []
    other_values = []
    for item in other_items or []:
        if (item.category or '') == 'ft_mobile':
            qty = float(item.liters or 0)
        else:
            qty = float(item.quantity or 0)
        if qty <= 0:
            continue
        other_labels.append(item.display_name())
        other_values.append(round(qty, 2))
    if len(other_labels) > 8:
        paired = sorted(zip(other_labels, other_values), key=lambda x: -x[1])
        top = paired[:7]
        rest = sum(v for _, v in paired[7:])
        other_labels = [n for n, _ in top] + (['Other'] if rest else [])
        other_values = [v for _, v in top] + ([round(rest, 2)] if rest else [])

    rate_as_of = build_fuel_rate_lookup(FuelPrice)
    fuel_cost_as_of = _fuel_cost_timeline(end)
    item_costs = _other_item_costs()
    profit_by = defaultdict(float)

    for reading in apply_agency_filter(
        MeterReading.query.options(joinedload(MeterReading.fuel_type)).filter(
            MeterReading.reading_date >= start,
            MeterReading.reading_date <= end,
            MeterReading.closing_reading.isnot(None),
        ),
        MeterReading,
    ).all():
        d = reading.reading_date
        liters = float(reading.liters_sold or 0)
        stored = getattr(reading, 'sale_rate', None)
        rate = float(stored) if stored is not None else rate_as_of(reading.fuel_type_id, d)
        cost = fuel_cost_as_of(reading.fuel_type_id, d)
        name = reading.fuel_type.name if reading.fuel_type else f'Fuel #{reading.fuel_type_id}'
        profit_by[name] += liters * (rate - cost)

    for e in apply_agency_filter(
        CreditSale.query.options(
            joinedload(CreditSale.fuel_type),
            joinedload(CreditSale.other_item),
        ).filter(
            CreditSale.sale_date >= start,
            CreditSale.sale_date <= end,
        ),
        CreditSale,
    ).all():
        et = (e.entry_type or 'sale').lower()
        if et in ('advance', 'loan', 'opening'):
            continue
        sale_val = float(e.amount or 0)
        qty = float(e.liters or 0)
        cogs = 0.0
        if e.fuel_type_id:
            cogs = qty * fuel_cost_as_of(e.fuel_type_id, e.sale_date)
            name = e.fuel_type.name if e.fuel_type else e.item_name
        elif e.other_item_id:
            cogs = qty * item_costs.get(e.other_item_id, 0.0)
            name = e.item_name
        else:
            name = e.item_name or 'Sale'
        profit_by[name] += sale_val - cogs

    top_profit = sorted(profit_by.items(), key=lambda x: -x[1])[:3]
    top_profit = [(n, round(v, 2)) for n, v in top_profit if v > 0]

    exp_rows = (
        apply_agency_filter(
            Expense.query.filter(Expense.expense_date >= start, Expense.expense_date <= end),
            Expense,
        )
        .order_by(Expense.amount.desc())
        .limit(5)
        .all()
    )
    top_expenses = [(e.name, round(float(e.amount or 0), 2)) for e in exp_rows if float(e.amount or 0) > 0]

    top_recv = (
        apply_agency_filter(
            Customer.query.options(
                load_only(Customer.id, Customer.name, Customer.current_balance_due, Customer.agency_id)
            ).filter(Customer.current_balance_due > 0),
            Customer,
        )
        .order_by(Customer.current_balance_due.desc())
        .limit(5)
        .all()
    )
    top_customers = [(c.name, round(float(c.current_balance_due or 0), 2)) for c in top_recv]

    top_pay = (
        apply_agency_filter(
            Vendor.query.options(
                load_only(Vendor.id, Vendor.name, Vendor.current_balance_payable, Vendor.agency_id)
            ).filter(Vendor.current_balance_payable > 0),
            Vendor,
        )
        .order_by(Vendor.current_balance_payable.desc())
        .limit(5)
        .all()
    )
    top_vendors = [(v.name, round(float(v.current_balance_payable or 0), 2)) for v in top_pay]

    def pack(pairs):
        return {
            'labels': [p[0] for p in pairs],
            'values': [p[1] for p in pairs],
        }

    return {
        'fuel_stock': {'labels': fuel_labels, 'values': fuel_values},
        'other_stock': {'labels': other_labels, 'values': other_values},
        'top_profit': pack(top_profit),
        'top_expenses': pack(top_expenses),
        'top_customers': pack(top_customers),
        'top_vendors': pack(top_vendors),
    }


def _top_credit_customers(limit=200000):
    """
    Customers with current_balance_due > limit, highest due first.
    Also attaches oldest unpaid sale/loan date and last payment for display.
    """
    today = datetime.utcnow().date()
    rows = []

    candidates = (
        apply_agency_filter(
            Customer.query.filter(Customer.current_balance_due > limit),
            Customer,
        )
        .order_by(Customer.current_balance_due.desc())
        .all()
    )

    for customer in candidates:
        unpaid = CreditSale.query.filter(
            CreditSale.customer_id == customer.id,
            CreditSale.entry_type.in_(('sale', 'loan')),
        ).all()

        oldest = None
        for e in unpaid:
            paid = float(e.amount_paid or 0)
            amt = float(e.amount or 0)
            status = (e.payment_status or 'unpaid').lower()
            if status == 'paid' and paid <= 0:
                paid = amt
            owed = max(amt - paid, 0.0)
            if (e.entry_type or 'sale') == 'loan':
                owed = amt if paid <= 0 else max(amt - paid, 0.0)
            if owed <= 0:
                continue
            if oldest is None or e.sale_date < oldest:
                oldest = e.sale_date

        last_payment = (
            Payment.query
            .filter_by(customer_id=customer.id)
            .order_by(Payment.payment_date.desc())
            .first()
        )
        days_overdue = (today - oldest).days if oldest else 0
        rows.append({
            'customer': customer,
            'due': float(customer.current_balance_due),
            'oldest_credit_date': oldest or '—',
            'days_overdue': days_overdue,
            'last_payment_date': last_payment.payment_date.date() if last_payment else None,
        })

    return rows


@dashboard_bp.route('/api/chart-data')
@login_required
def chart_data():
    """Trend series using the same profit math as the Profit KPI card."""
    _, start, end = parse_period(request.args)
    return jsonify(_build_meter_trend(start, end))


@dashboard_bp.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    report_type = request.form.get('report_type', 'sales')
    start_date_str = request.form.get('start_date', '')
    end_date_str = request.form.get('end_date', '')

    if not start_date_str:
        start_date_str = (datetime.utcnow().date() - timedelta(days=30)).strftime('%Y-%m-%d')
    if not end_date_str:
        end_date_str = datetime.utcnow().date().strftime('%Y-%m-%d')

    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_dt = datetime.combine(datetime.strptime(end_date_str, '%Y-%m-%d').date(), datetime.max.time())
    start_d = start_dt.date()
    end_d = end_dt.date()

    preview_data = []
    headers = []

    if report_type == 'sales':
        headers = ['ID', 'Customer', 'Type', 'Item', 'Qty', 'Amount', 'Paid', 'Credit', 'Status', 'Date']
        records = apply_agency_filter(
            CreditSale.query.options(
                joinedload(CreditSale.customer),
                joinedload(CreditSale.fuel_type),
                joinedload(CreditSale.other_item),
            ).filter(
                CreditSale.sale_date >= start_d,
                CreditSale.sale_date <= end_d,
            ),
            CreditSale,
        ).order_by(CreditSale.sale_date.asc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.customer.name if r.customer else 'Walk-In',
                r.entry_type,
                r.item_name,
                f"{float(r.liters):.2f}",
                f"PKR {float(r.amount):,.2f}",
                f"PKR {float(r.amount_paid or 0):,.2f}",
                f"PKR {r.credit_amount:,.2f}",
                r.payment_status,
                r.sale_date.strftime('%Y-%m-%d'),
            ])

    elif report_type == 'profit':
        headers = ['Date', 'Fuel', 'Liters', 'Rate', 'Cost/L', 'Revenue', 'Profit']
        rate_as_of = build_fuel_rate_lookup(FuelPrice)
        cost_as_of = _fuel_cost_timeline(end_d)
        records = apply_agency_filter(
            MeterReading.query.options(joinedload(MeterReading.fuel_type)).filter(
                MeterReading.reading_date >= start_d,
                MeterReading.reading_date <= end_d,
                MeterReading.closing_reading.isnot(None),
            ),
            MeterReading,
        ).order_by(MeterReading.reading_date.asc()).all()
        for r in records:
            liters = float(r.liters_sold or 0)
            stored = getattr(r, 'sale_rate', None)
            rate = float(stored) if stored is not None else rate_as_of(
                r.fuel_type_id, r.reading_date
            )
            cost = cost_as_of(r.fuel_type_id, r.reading_date)
            preview_data.append([
                r.reading_date.strftime('%Y-%m-%d'),
                r.fuel_type.name if r.fuel_type else r.fuel_type_id,
                f"{liters:.2f}L",
                f"PKR {rate:,.2f}",
                f"PKR {cost:,.2f}",
                f"PKR {liters * rate:,.2f}",
                f"PKR {liters * (rate - cost):,.2f}",
            ])

    elif report_type == 'stock':
        headers = ['Entry ID', 'Fuel Type', 'Liters Added', 'Cost/L', 'Total Delivery Cost', 'Supplier', 'Recorded By', 'Date']
        records = apply_agency_filter(
            StockEntry.query.options(
                joinedload(StockEntry.fuel_type),
                joinedload(StockEntry.creator),
            ).filter(
                StockEntry.entry_date >= start_dt,
                StockEntry.entry_date <= end_dt,
            ),
            StockEntry,
        ).order_by(StockEntry.entry_date.asc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.fuel_type.name if r.fuel_type else '-',
                f"{r.liters_added:.2f}L",
                f"PKR {float(r.cost_per_liter):,.2f}",
                f"PKR {float(r.liters_added * r.cost_per_liter):,.2f}",
                r.supplier or '-',
                r.creator.name,
                r.entry_date.strftime('%Y-%m-%d %H:%M'),
            ])

    elif report_type == 'due':
        headers = ['Customer ID', 'Name', 'Phone', 'Address', 'Credit Limit', 'Balance Outstanding']
        records = apply_agency_filter(
            Customer.query.filter(Customer.current_balance_due != 0),
            Customer,
        ).order_by(Customer.current_balance_due.desc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.name,
                r.phone or '-',
                r.address or '-',
                f"PKR {float(r.credit_limit):,.2f}" if r.credit_limit else 'No Limit',
                f"PKR {float(r.current_balance_due):,.2f}",
            ])

    elif report_type == 'expense':
        headers = ['ID', 'Date', 'Name', 'Description', 'Amount', 'By']
        records = apply_agency_filter(
            Expense.query.filter(
                Expense.expense_date >= start_d,
                Expense.expense_date <= end_d,
            ),
            Expense,
        ).order_by(Expense.expense_date.asc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.expense_date.strftime('%Y-%m-%d'),
                r.name,
                r.description or '-',
                f"PKR {float(r.amount):,.2f}",
                r.recorder.name if r.recorder else '-',
            ])

    if request.args.get('export') == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([f"OCTANEFLOW REPORT: {report_type.upper()}"])
        writer.writerow([f"Period: {start_date_str} to {end_date_str}"])
        writer.writerow([])
        writer.writerow(headers)
        writer.writerows(preview_data)
        response = Response(output.getvalue(), mimetype='text/csv')
        response.headers['Content-Disposition'] = (
            f"attachment; filename=octaneflow_{report_type}_report_{start_date_str}_to_{end_date_str}.csv"
        )
        return response

    return render_template(
        'dashboard/reports.html',
        report_type=report_type,
        start_date=start_date_str,
        end_date=end_date_str,
        headers=headers,
        preview_data=preview_data,
    )
