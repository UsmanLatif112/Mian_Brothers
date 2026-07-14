from flask import render_template, jsonify, request, Response, flash, redirect, url_for
from flask_login import login_required
from app.dashboard import dashboard_bp
from app.models import (
    db, Sale, StockEntry, Customer, Inventory, FuelType, FuelPrice,
    OtherItem, MeterReading, CreditSale, Expense, Payment, DailyCashCount
)
from app.utils import parse_period, PERIOD_CHOICES, compute_period_stats, fuel_rate_for
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
    stats = compute_period_stats(start, end, _models_ns())

    # Profit for period from meter readings
    total_profit = 0.0
    readings = MeterReading.query.filter(
        MeterReading.reading_date >= start,
        MeterReading.reading_date <= end,
        MeterReading.closing_reading.isnot(None),
    ).all()
    for reading in readings:
        liters = float(reading.liters_sold or 0)
        rate = fuel_rate_for(reading.fuel_type_id, FuelPrice)
        cost = get_cost_for_sale(
            reading.fuel_type_id,
            datetime.combine(reading.reading_date, datetime.min.time()),
        )
        total_profit += liters * (rate - cost)

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

    return render_template(
        'dashboard/index.html',
        stats=stats,
        total_profit=total_profit,
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
    period, start, end = parse_period(request.args)
    days = (end - start).days + 1
    days = max(min(days, 366), 1)
    dates_list = [start + timedelta(days=x) for x in range(days)]
    labels = [d.strftime('%b %d') for d in dates_list]

    sales_trend = []
    profit_trend = []
    for d in dates_list:
        readings = MeterReading.query.filter(
            MeterReading.reading_date == d,
            MeterReading.closing_reading.isnot(None),
        ).all()
        day_sales = 0.0
        day_profit = 0.0
        for reading in readings:
            liters = float(reading.liters_sold or 0)
            rate = fuel_rate_for(reading.fuel_type_id, FuelPrice)
            day_sales += liters * rate
            cost = get_cost_for_sale(reading.fuel_type_id, datetime.combine(d, datetime.min.time()))
            day_profit += liters * (rate - cost)
        sales_trend.append(round(day_sales, 2))
        profit_trend.append(round(day_profit, 2))

    fuel_sales = db.session.query(
        FuelType.name, func.sum(MeterReading.liters_sold)
    ).join(MeterReading, MeterReading.fuel_type_id == FuelType.id).filter(
        MeterReading.reading_date >= start,
        MeterReading.reading_date <= end,
        MeterReading.closing_reading.isnot(None),
    ).group_by(FuelType.name).all()

    stats = compute_period_stats(start, end, _models_ns())

    return jsonify({
        'labels': labels,
        'sales_trend': sales_trend,
        'profit_trend': profit_trend,
        'fuel_split': {
            'labels': [item[0] for item in fuel_sales] or ['No Data'],
            'data': [float(item[1] or 0) for item in fuel_sales] or [0.0],
        },
        'payment_split': {
            'labels': ['Expected Cash', 'Period Credit', 'Expenses'],
            'data': [stats['expected_cash'], stats['period_credit'], stats['expense_total']],
        },
    })


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
