import csv
import io
from flask import render_template, jsonify, request, Response, flash, redirect, url_for
from flask_login import login_required
from app.dashboard import dashboard_bp
from app.models import db, Sale, StockEntry, Customer, Inventory, FuelType, FuelPrice, OtherItem, MeterReading, CreditSale
from datetime import datetime, timedelta, date
from sqlalchemy import func

def get_cost_for_sale(fuel_type_id, sale_date):
    """
    Finds the purchase cost per liter for a fuel type.
    Finds the latest StockEntry before the sale_date.
    Defaults to 80% of current price if no deliveries are logged.
    """
    latest_delivery = StockEntry.query.filter(
        StockEntry.fuel_type_id == fuel_type_id,
        StockEntry.entry_date <= sale_date
    ).order_by(StockEntry.entry_date.desc()).first()
    
    if latest_delivery:
        return float(latest_delivery.cost_per_liter)
        
    # Fallback to general first delivery
    first_delivery = StockEntry.query.filter_by(fuel_type_id=fuel_type_id).first()
    if first_delivery:
        return float(first_delivery.cost_per_liter)
        
    # Standard fallback based on current sales price
    latest_price = FuelPrice.query.filter_by(fuel_type_id=fuel_type_id).order_by(FuelPrice.created_at.desc()).first()
    if latest_price:
        return float(latest_price.price_per_liter) * 0.8
        
    return 1.00 # hard fallback

@dashboard_bp.route('/')
@login_required
def index():
    # Meter readings are the source of truth for fuel sales
    closed_readings = MeterReading.query.filter(MeterReading.closing_reading.isnot(None)).all()
    total_sales_amt = 0.0
    total_profit_amt = 0.0
    for reading in closed_readings:
        liters = float(reading.liters_sold or 0)
        # Prefer price effective near reading date
        price_rec = FuelPrice.query.filter_by(fuel_type_id=reading.fuel_type_id).order_by(FuelPrice.created_at.desc()).first()
        rate = float(price_rec.price_per_liter) if price_rec else 0.0
        amount = liters * rate
        total_sales_amt += amount
        cost = get_cost_for_sale(reading.fuel_type_id, datetime.combine(reading.reading_date, datetime.min.time()))
        total_profit_amt += liters * (rate - cost)
        
    # Total Credit Outstanding
    total_credit_outstanding = db.session.query(func.sum(Customer.current_balance_due)).scalar() or 0.0
    
    # Live Stock Levels & Dry Warnings (< 100 L)
    DRY_THRESHOLD = 100.0
    inventory_items = Inventory.query.all()
    stock_summary = {}
    petrol_stock = None
    diesel_stock = None
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
            
    # Recent credit ledger entries (not meter duplicates)
    recent_sales = CreditSale.query.order_by(CreditSale.created_at.desc()).limit(5).all()
    recent_deliveries = StockEntry.query.order_by(StockEntry.entry_date.desc()).limit(5).all()
    other_items = OtherItem.query.order_by(OtherItem.name.asc()).all()
    
    return render_template('dashboard/index.html',
                           total_sales=total_sales_amt,
                           total_profit=total_profit_amt,
                           total_credit=total_credit_outstanding,
                           stock_summary=stock_summary,
                           dry_message=dry_message,
                           petrol_stock=petrol_stock,
                           diesel_stock=diesel_stock,
                           dry_threshold=DRY_THRESHOLD,
                           recent_sales=recent_sales,
                           recent_deliveries=recent_deliveries,
                           other_items=other_items,
                           recent_is_credit=True)

@dashboard_bp.route('/api/chart-data')
@login_required
def chart_data():
    period = request.args.get('period', '7days') # '7days' or '30days'
    days_to_subtract = 7 if period == '7days' else 30
    
    today = datetime.utcnow().date()
    start_date = today - timedelta(days=days_to_subtract - 1)
    
    # Sales & Profit Daily trends
    dates_list = [start_date + timedelta(days=x) for x in range(days_to_subtract)]
    labels = [d.strftime('%b %d') for d in dates_list]
    
    sales_trend = []
    profit_trend = []
    
    for d in dates_list:
        readings = MeterReading.query.filter(
            MeterReading.reading_date == d,
            MeterReading.closing_reading.isnot(None)
        ).all()
        day_sales = 0.0
        day_profit = 0.0
        for reading in readings:
            liters = float(reading.liters_sold or 0)
            price_rec = FuelPrice.query.filter_by(fuel_type_id=reading.fuel_type_id).order_by(FuelPrice.created_at.desc()).first()
            rate = float(price_rec.price_per_liter) if price_rec else 0.0
            day_sales += liters * rate
            cost = get_cost_for_sale(reading.fuel_type_id, datetime.combine(d, datetime.min.time()))
            day_profit += liters * (rate - cost)
            
        sales_trend.append(round(day_sales, 2))
        profit_trend.append(round(day_profit, 2))
        
    # Fuel Type Sales Split from meter (last 30 days)
    start_30d = today - timedelta(days=29)
    fuel_sales = db.session.query(
        FuelType.name, func.sum(MeterReading.liters_sold)
    ).join(MeterReading, MeterReading.fuel_type_id == FuelType.id).filter(
        MeterReading.reading_date >= start_30d,
        MeterReading.closing_reading.isnot(None)
    ).group_by(FuelType.name).all()
    
    fuel_split_labels = [item[0] for item in fuel_sales]
    fuel_split_data = [float(item[1]) if item[1] else 0.0 for item in fuel_sales]
    
    # Cash vs Credit from meter total vs credit sales
    meter_total = 0.0
    for reading in MeterReading.query.filter(
        MeterReading.reading_date >= start_30d,
        MeterReading.closing_reading.isnot(None)
    ).all():
        liters = float(reading.liters_sold or 0)
        price_rec = FuelPrice.query.filter_by(fuel_type_id=reading.fuel_type_id).order_by(FuelPrice.created_at.desc()).first()
        rate = float(price_rec.price_per_liter) if price_rec else 0.0
        meter_total += liters * rate
    credit_total = db.session.query(func.sum(CreditSale.amount)).filter(
        CreditSale.sale_date >= start_30d
    ).scalar() or 0.0
    credit_total = float(credit_total)
    cash_total = max(meter_total - credit_total, 0.0)
    
    return jsonify({
        'labels': labels,
        'sales_trend': sales_trend,
        'profit_trend': profit_trend,
        'fuel_split': {
            'labels': fuel_split_labels if fuel_split_labels else ['No Data'],
            'data': fuel_split_data if fuel_split_data else [0.0]
        },
        'payment_split': {
            'labels': ['Cash', 'Credit'],
            'data': [cash_total, credit_total]
        }
    })

@dashboard_bp.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    report_type = request.form.get('report_type', 'sales')
    start_date_str = request.form.get('start_date', '')
    end_date_str = request.form.get('end_date', '')
    
    # Defaults
    if not start_date_str:
        start_date_str = (datetime.utcnow().date() - timedelta(days=30)).strftime('%Y-%m-%d')
    if not end_date_str:
        end_date_str = datetime.utcnow().date().strftime('%Y-%m-%d')
        
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_dt = datetime.combine(datetime.strptime(end_date_str, '%Y-%m-%d').date(), datetime.max.time())
    
    preview_data = []
    headers = []
    
    if report_type == 'sales':
        headers = ['Sale ID', 'Customer', 'Fuel Type', 'Liters', 'Price/L', 'Total Amount', 'Type', 'Date']
        records = Sale.query.filter(Sale.sale_date >= start_dt, Sale.sale_date <= end_dt).order_by(Sale.sale_date.asc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.customer.name if r.customer else 'Walk-In',
                r.fuel_type.name,
                f"{r.liters:.2f}L",
                f"PKR {float(r.price_per_liter):,.2f}",
                f"PKR {float(r.total_amount):,.2f}",
                r.payment_type.upper(),
                r.sale_date.strftime('%Y-%m-%d %H:%M')
            ])
            
    elif report_type == 'profit':
        headers = ['Sale ID', 'Fuel Type', 'Liters', 'Retail Price/L', 'Cost Price/L', 'Net Revenue', 'Net Margin', 'Profit']
        records = Sale.query.filter(Sale.sale_date >= start_dt, Sale.sale_date <= end_dt).order_by(Sale.sale_date.asc()).all()
        for r in records:
            cost = get_cost_for_sale(r.fuel_type_id, r.sale_date)
            profit = float(r.liters) * (float(r.price_per_liter) - cost)
            preview_data.append([
                r.id,
                r.fuel_type.name,
                f"{r.liters:.2f}L",
                f"PKR {float(r.price_per_liter):,.2f}",
                f"PKR {cost:,.2f}",
                f"PKR {float(r.total_amount):,.2f}",
                f"PKR {(float(r.price_per_liter) - cost):,.2f}/L",
                f"PKR {profit:,.2f}"
            ])
            
    elif report_type == 'stock':
        headers = ['Entry ID', 'Fuel Type', 'Liters Added', 'Cost/L', 'Total Delivery Cost', 'Supplier', 'Recorded By', 'Date']
        records = StockEntry.query.filter(StockEntry.entry_date >= start_dt, StockEntry.entry_date <= end_dt).order_by(StockEntry.entry_date.asc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.fuel_type.name,
                f"{r.liters_added:.2f}L",
                f"PKR {float(r.cost_per_liter):,.2f}",
                f"PKR {float(r.liters_added * r.cost_per_liter):,.2f}",
                r.supplier or '-',
                r.creator.name,
                r.entry_date.strftime('%Y-%m-%d %H:%M')
            ])
            
    elif report_type == 'due':
        headers = ['Customer ID', 'Name', 'Phone', 'Address', 'Credit Limit', 'Balance Outstanding']
        records = Customer.query.filter(Customer.current_balance_due > 0).order_by(Customer.current_balance_due.desc()).all()
        for r in records:
            preview_data.append([
                r.id,
                r.name,
                r.phone or '-',
                r.address or '-',
                f"PKR {float(r.credit_limit):,.2f}" if r.credit_limit else 'No Limit',
                f"PKR {float(r.current_balance_due):,.2f}"
            ])
            
    # If request is CSV export
    if request.args.get('export') == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write Title Header
        writer.writerow([f"OCTANEFLOW REPORT: {report_type.upper()}"])
        writer.writerow([f"Period: {start_date_str} to {end_date_str}"])
        writer.writerow([])
        
        # Write columns
        writer.writerow(headers)
        # Write data rows
        writer.writerows(preview_data)
        
        response = Response(output.getvalue(), mimetype='text/csv')
        response.headers['Content-Disposition'] = f"attachment; filename=octaneflow_{report_type}_report_{start_date_str}_to_{end_date_str}.csv"
        return response
        
    return render_template('dashboard/reports.html',
                           report_type=report_type,
                           start_date=start_date_str,
                           end_date=end_date_str,
                           headers=headers,
                           preview_data=preview_data)
