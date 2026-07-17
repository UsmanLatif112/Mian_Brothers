from flask import render_template, jsonify, request, Response, flash, redirect, url_for
from flask_login import login_required
from app.dashboard import dashboard_bp
from app.models import (
    db, Sale, StockEntry, Customer, Inventory, FuelType, FuelPrice,
    OtherItem, MeterReading, CreditSale, Expense, Payment, DailyCashCount,
    ItemPurchaseLog,
)
from app.utils import (
    parse_period, PERIOD_CHOICES, compute_period_stats, fuel_rate_for,
    build_period_cash_entries, build_cash_journal_summary, paginate,
)
from datetime import datetime, timedelta
from sqlalchemy import func
from types import SimpleNamespace
import csv
import io


def get_cost_for_sale(fuel_type_id, sale_date):
    latest_delivery = StockEntry.query.filter(
        StockEntry.fuel_type_id == fuel_type_id,
        StockEntry.entry_date <= sale_date
    ).order_by(StockEntry.entry_date.desc()).first()

    if latest_delivery:
        return float(latest_delivery.cost_per_liter)

    first_delivery = StockEntry.query.filter_by(fuel_type_id=fuel_type_id).first()
    if first_delivery:
        return float(first_delivery.cost_per_liter)

    return 0.0


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

    # Latest selling rates (per fuel type)
    rates = {}
    for fp in FuelPrice.query.order_by(FuelPrice.created_at.desc()).all():
        if fp.fuel_type_id not in rates:
            rates[fp.fuel_type_id] = float(fp.price_per_liter or 0)

    # Fuel cost timeline: (date, cost) ascending per fuel_type_id
    # Prefer purchase logs; fall back to legacy stock_entries per fuel type.
    fuel_costs = defaultdict(list)
    for log in ItemPurchaseLog.query.filter(
        ItemPurchaseLog.category == 'fuel',
        ItemPurchaseLog.fuel_type_id.isnot(None),
    ).order_by(ItemPurchaseLog.entry_date.asc()).all():
        ed = log.entry_date
        d = ed.date() if isinstance(ed, datetime) else ed
        if d is None:
            continue
        fuel_costs[log.fuel_type_id].append((d, float(log.cost_price or 0)))

    fuels_with_purchase_logs = set(fuel_costs.keys())
    for se in StockEntry.query.order_by(StockEntry.entry_date.asc()).all():
        if not se.fuel_type_id or se.fuel_type_id in fuels_with_purchase_logs:
            continue
        ed = se.entry_date
        d = ed.date() if isinstance(ed, datetime) else ed
        if d is None:
            continue
        fuel_costs[se.fuel_type_id].append((d, float(se.cost_per_liter or 0)))

    def as_date(val):
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

    def fuel_cost_as_of(fuel_type_id, sale_date):
        sale_date = as_date(sale_date)
        entries = fuel_costs.get(fuel_type_id) or []
        best = None
        for d, cost in entries:
            if sale_date and d <= sale_date:
                best = cost
            else:
                break
        if best is not None:
            return best
        return entries[0][1] if entries else 0.0

    # Other/FT current cost by item id
    item_costs = {
        oi.id: float(oi.cost_price or 0)
        for oi in OtherItem.query.all()
    }

    day_sales = defaultdict(float)
    day_cogs = defaultdict(float)
    day_gross = defaultdict(float)
    day_expenses = defaultdict(float)

    # 1) Meter / pump fuel
    readings = MeterReading.query.filter(
        MeterReading.reading_date >= start,
        MeterReading.reading_date <= end,
        MeterReading.closing_reading.isnot(None),
    ).all()
    for reading in readings:
        d = as_date(reading.reading_date)
        if d is None:
            continue
        liters = float(reading.liters_sold or 0)
        rate = rates.get(reading.fuel_type_id, 0.0)
        cost = fuel_cost_as_of(reading.fuel_type_id, d)
        sale_val = liters * rate
        cogs_val = liters * cost
        day_sales[d] += sale_val
        day_cogs[d] += cogs_val
        day_gross[d] += sale_val - cogs_val

    # 2) Credit / other / FT sales — skip advance/loan/opening
    entries = CreditSale.query.filter(
        CreditSale.sale_date >= start,
        CreditSale.sale_date <= end,
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
    for exp in Expense.query.filter(
        Expense.expense_date >= start,
        Expense.expense_date <= end,
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

    if span <= 62:
        grain = 'day'
        cursor = start
        while cursor <= end:
            labels.append(cursor.strftime('%b %d'))
            date_labels.append(cursor.strftime('%Y-%m-%d (%a)'))
            sales_trend.append(round(day_sales.get(cursor, 0.0), 2))
            profit_trend.append(round(day_net.get(cursor, 0.0), 2))
            expense_trend.append(round(day_expenses.get(cursor, 0.0), 2))
            gross_trend.append(round(day_gross.get(cursor, 0.0), 2))
            cogs_trend.append(round(day_cogs.get(cursor, 0.0), 2))
            cursor += timedelta(days=1)
    elif span <= 400:
        grain = 'week'
        cursor = start
        while cursor <= end:
            week_end = min(cursor + timedelta(days=6), end)
            s = p = ex = g = c = 0.0
            d = cursor
            while d <= week_end:
                s += day_sales.get(d, 0.0)
                p += day_net.get(d, 0.0)
                ex += day_expenses.get(d, 0.0)
                g += day_gross.get(d, 0.0)
                c += day_cogs.get(d, 0.0)
                d += timedelta(days=1)
            if cursor == week_end:
                labels.append(cursor.strftime('%b %d'))
                date_labels.append(cursor.strftime('%Y-%m-%d (%a)'))
            else:
                labels.append(f"{cursor.strftime('%b %d')}–{week_end.strftime('%b %d')}")
                date_labels.append(
                    f"{cursor.strftime('%Y-%m-%d')} → {week_end.strftime('%Y-%m-%d')}"
                )
            sales_trend.append(round(s, 2))
            profit_trend.append(round(p, 2))
            expense_trend.append(round(ex, 2))
            gross_trend.append(round(g, 2))
            cogs_trend.append(round(c, 2))
            cursor = week_end + timedelta(days=1)
    else:
        grain = 'month'
        y, m = start.year, start.month
        while True:
            month_start = datetime(y, m, 1).date()
            if month_start > end:
                break
            if m == 12:
                next_month = datetime(y + 1, 1, 1).date()
            else:
                next_month = datetime(y, m + 1, 1).date()
            month_end = min(next_month - timedelta(days=1), end)
            range_start = max(month_start, start)
            s = p = ex = g = c = 0.0
            d = range_start
            while d <= month_end:
                s += day_sales.get(d, 0.0)
                p += day_net.get(d, 0.0)
                ex += day_expenses.get(d, 0.0)
                g += day_gross.get(d, 0.0)
                c += day_cogs.get(d, 0.0)
                d += timedelta(days=1)
            labels.append(range_start.strftime('%b %Y'))
            date_labels.append(
                f"{range_start.strftime('%Y-%m-%d')} → {month_end.strftime('%Y-%m-%d')}"
            )
            sales_trend.append(round(s, 2))
            profit_trend.append(round(p, 2))
            expense_trend.append(round(ex, 2))
            gross_trend.append(round(g, 2))
            cogs_trend.append(round(c, 2))
            if next_month > end:
                break
            y, m = next_month.year, next_month.month

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
    period, start, end = parse_period(request.args)
    stats = compute_period_stats(
        start, end, _models_ns(),
        include_opening_credit=(period == 'all'),
    )

    # Same calculation as the Revenue & Margin chart (period total)
    meter_trend = _build_meter_trend(start, end)
    total_profit = meter_trend['total_profit']
    total_gross = meter_trend['total_gross']
    total_expenses_profit = meter_trend['total_expenses']

    DRY_THRESHOLD = 100.0
    inventory_items = Inventory.query.all()
    stock_summary = {}
    petrol_stock = diesel_stock = None
    for inv in inventory_items:
        name = inv.fuel_type.name
        current = float(inv.current_stock_liters)
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

    recent_deliveries = StockEntry.query.order_by(StockEntry.entry_date.desc()).limit(5).all()
    other_items = OtherItem.query.order_by(OtherItem.name.asc()).all()

    # Top customers: credit > 200k and uncleared for more than 1 month
    top_credit_customers = _top_overdue_credit_customers(limit=200000, overdue_days=30)

    detail = request.args.get('detail', '')

    cash_summary = build_cash_journal_summary(stats)
    cash_entries_all = build_period_cash_entries(stats)
    cash_page = request.args.get('cash_page', 1)
    cash_entries, cash_pagination = paginate(cash_entries_all, cash_page, 20)

    return render_template(
        'dashboard/index.html',
        stats=stats,
        total_profit=total_profit,
        total_gross=total_gross,
        total_expenses_profit=total_expenses_profit,
        stock_summary=stock_summary,
        dry_message=dry_message,
        petrol_stock=petrol_stock,
        diesel_stock=diesel_stock,
        dry_threshold=DRY_THRESHOLD,
        top_credit_customers=top_credit_customers,
        recent_deliveries=recent_deliveries,
        other_items=other_items,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        detail=detail,
        cash_summary=cash_summary,
        cash_entries=cash_entries,
        cash_pagination=cash_pagination,
    )


def _top_overdue_credit_customers(limit=200000, overdue_days=30):
    """
    Customers with balance_due > limit whose oldest unpaid credit/loan
    is older than overdue_days (default 1 month).
    """
    today = datetime.utcnow().date()
    cutoff = today - timedelta(days=overdue_days)
    rows = []

    candidates = (
        Customer.query
        .filter(Customer.current_balance_due > limit)
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

        if oldest is None or oldest > cutoff:
            continue

        last_payment = (
            Payment.query
            .filter_by(customer_id=customer.id)
            .order_by(Payment.payment_date.desc())
            .first()
        )
        days_overdue = (today - oldest).days
        rows.append({
            'customer': customer,
            'due': float(customer.current_balance_due),
            'oldest_credit_date': oldest,
            'days_overdue': days_overdue,
            'last_payment_date': last_payment.payment_date.date() if last_payment else None,
        })

    rows.sort(key=lambda r: r['due'], reverse=True)
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
        records = CreditSale.query.filter(
            CreditSale.sale_date >= start_d,
            CreditSale.sale_date <= end_d,
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
        records = MeterReading.query.filter(
            MeterReading.reading_date >= start_d,
            MeterReading.reading_date <= end_d,
            MeterReading.closing_reading.isnot(None),
        ).order_by(MeterReading.reading_date.asc()).all()
        for r in records:
            liters = float(r.liters_sold or 0)
            rate = fuel_rate_for(r.fuel_type_id, FuelPrice)
            cost = get_cost_for_sale(r.fuel_type_id, datetime.combine(r.reading_date, datetime.min.time()))
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
        records = StockEntry.query.filter(
            StockEntry.entry_date >= start_dt,
            StockEntry.entry_date <= end_dt,
        ).order_by(StockEntry.entry_date.asc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.fuel_type.name,
                f"{r.liters_added:.2f}L",
                f"PKR {float(r.cost_per_liter):,.2f}",
                f"PKR {float(r.liters_added * r.cost_per_liter):,.2f}",
                r.supplier or '-',
                r.creator.name,
                r.entry_date.strftime('%Y-%m-%d %H:%M'),
            ])

    elif report_type == 'due':
        headers = ['Customer ID', 'Name', 'Phone', 'Address', 'Credit Limit', 'Balance Outstanding']
        records = Customer.query.filter(Customer.current_balance_due != 0).order_by(
            Customer.current_balance_due.desc()
        ).all()
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
        records = Expense.query.filter(
            Expense.expense_date >= start_d,
            Expense.expense_date <= end_d,
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
