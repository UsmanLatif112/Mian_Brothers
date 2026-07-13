from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.sales import sales_bp
from app.models import (
    db, FuelType, FuelPrice, Inventory, MeterReading, Customer,
    Machine, CreditSale, OtherItem
)
from app.sms.sms_service import send_sms
from datetime import datetime
from sqlalchemy import func


def _today():
    return datetime.utcnow().date()


def _fuel_rate(fuel_type_id):
    latest = FuelPrice.query.filter_by(fuel_type_id=fuel_type_id).order_by(FuelPrice.created_at.desc()).first()
    return float(latest.price_per_liter) if latest else None


def _machines_for_fuel(fuel_type_id):
    return (
        Machine.query
        .filter_by(fuel_type_id=fuel_type_id, is_active=True)
        .order_by(Machine.name.asc())
        .all()
    )


def _day_fuel_totals(target_date):
    """Liters and amounts sold today per fuel type from closed meter readings."""
    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()
    totals = {}
    for ft in fuel_types:
        readings = MeterReading.query.filter(
            MeterReading.fuel_type_id == ft.id,
            MeterReading.reading_date == target_date,
            MeterReading.closing_reading.isnot(None)
        ).all()
        liters = sum(float(r.liters_sold or 0) for r in readings)
        rate = _fuel_rate(ft.id) or 0.0
        totals[ft.id] = {
            'fuel': ft,
            'liters': liters,
            'rate': rate,
            'amount': liters * rate,
            'readings': readings,
        }
    return totals


@sales_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    today = _today()

    if request.method == 'POST':
        action = request.form.get('action', 'meter_sale')

        # ---------- Meter Sale (single page) ----------
        if action == 'meter_sale':
            fuel_type_id = request.form.get('fuel_type_id')
            if not fuel_type_id:
                flash('Please select a fuel type.', 'danger')
                return redirect(url_for('sales.index'))

            fuel_type = FuelType.query.get(fuel_type_id)
            if not fuel_type:
                flash('Fuel type not found.', 'danger')
                return redirect(url_for('sales.index'))

            machines = _machines_for_fuel(fuel_type.id)
            if len(machines) < 1:
                flash(f'No machines configured for {fuel_type.name}.', 'danger')
                return redirect(url_for('sales.index'))

            rate = _fuel_rate(fuel_type.id)
            if rate is None:
                flash(f'No active price for {fuel_type.name}. Set price first.', 'danger')
                return redirect(url_for('sales.index'))

            try:
                machine_sales = []
                total_liters = 0.0

                for machine in machines:
                    open_raw = request.form.get(f'opening_{machine.id}')
                    close_raw = request.form.get(f'closing_{machine.id}')
                    if open_raw in (None, '') or close_raw in (None, ''):
                        raise ValueError(f'Enter both readings for {machine.name}.')

                    opening = float(open_raw)
                    closing = float(close_raw)
                    if opening < 0 or closing < 0:
                        raise ValueError(f'Readings for {machine.name} cannot be negative.')
                    if closing < opening:
                        raise ValueError(
                            f'{machine.name}: Reading 2 ({closing}) cannot be less than Reading 1 ({opening}).'
                        )

                    liters = closing - opening
                    total_liters += liters
                    machine_sales.append({
                        'machine': machine,
                        'opening': opening,
                        'closing': closing,
                        'liters': liters,
                    })

                if total_liters <= 0:
                    raise ValueError('Total liters sold must be greater than zero.')

                inventory = Inventory.query.filter_by(fuel_type_id=fuel_type.id).first()
                if not inventory:
                    raise ValueError(f'No inventory record for {fuel_type.name}.')

                # Previous liters already applied to stock today for this fuel
                previous_liters = 0.0
                for machine in machines:
                    existing = MeterReading.query.filter_by(
                        machine_id=machine.id,
                        reading_date=today
                    ).first()
                    if existing and existing.liters_sold is not None:
                        previous_liters += float(existing.liters_sold)

                delta_liters = total_liters - previous_liters
                available = float(inventory.current_stock_liters)
                if delta_liters > available:
                    raise ValueError(
                        f'Insufficient {fuel_type.name} stock. Available: {available:.2f}L, '
                        f'Additional sold: {delta_liters:.2f}L.'
                    )

                # Upsert today's meter readings per machine
                for item in machine_sales:
                    machine = item['machine']
                    reading = MeterReading.query.filter_by(
                        machine_id=machine.id,
                        reading_date=today
                    ).first()
                    if reading:
                        reading.opening_reading = item['opening']
                        reading.closing_reading = item['closing']
                        reading.liters_sold = item['liters']
                        reading.closed_by = current_user.id
                        reading.closed_at = datetime.utcnow()
                        reading.fuel_type_id = fuel_type.id
                    else:
                        db.session.add(MeterReading(
                            machine_id=machine.id,
                            dispenser_nozzle_id=machine.name,
                            fuel_type_id=fuel_type.id,
                            opening_reading=item['opening'],
                            closing_reading=item['closing'],
                            liters_sold=item['liters'],
                            reading_date=today,
                            recorded_by=current_user.id,
                            closed_by=current_user.id,
                            closed_at=datetime.utcnow()
                        ))

                inventory.current_stock_liters = available - delta_liters
                db.session.commit()

                amount = total_liters * rate
                flash(
                    f'{fuel_type.name} meter sale saved: {total_liters:.2f}L '
                    f'(PKR {amount:,.2f}). Inventory adjusted by {delta_liters:.2f}L.',
                    'success'
                )
            except ValueError as e:
                db.session.rollback()
                flash(str(e), 'danger')

            return redirect(url_for('sales.index'))

        # ---------- Item sale (fuel = ledger only; shop items decrease stock) ----------
        if action == 'credit_sale':
            customer_id = (request.form.get('customer_id') or '').strip() or None
            item_key = (request.form.get('item_key') or '').strip()
            qty_raw = request.form.get('liters')
            remarks = (request.form.get('remarks') or '').strip() or None
            payment_status = request.form.get('payment_status', 'unpaid')
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'

            if not item_key or not qty_raw:
                flash('Item and quantity are required.', 'danger')
                return redirect(url_for('sales.index'))

            is_fuel_sale = item_key.startswith('fuel:')
            is_shop_sale = item_key.startswith('item:')

            # Petrol/Diesel: customer always required. Other items: walk-in allowed.
            if is_fuel_sale and not customer_id:
                flash('Customer is required for petrol/diesel sales (walk-in not allowed).', 'danger')
                return redirect(url_for('sales.index'))

            # Walk-in shop sale is always cash (cannot put credit on no customer)
            if is_shop_sale and not customer_id:
                payment_status = 'paid'

            try:
                qty_val = float(qty_raw)
                if qty_val <= 0:
                    raise ValueError('Quantity must be greater than zero.')
            except ValueError as e:
                flash(f'Invalid sale input: {e}', 'danger')
                return redirect(url_for('sales.index'))

            customer = Customer.query.get(customer_id) if customer_id else None
            if customer_id and not customer:
                flash('Customer not found.', 'danger')
                return redirect(url_for('sales.index'))

            fuel_type = None
            other_item = None
            rate = None
            item_label = ''
            stock_note = 'ledger only — fuel stock unchanged'

            if is_fuel_sale:
                fuel_type = FuelType.query.get(item_key.split(':', 1)[1])
                if not fuel_type:
                    flash('Fuel type not found.', 'danger')
                    return redirect(url_for('sales.index'))
                rate = _fuel_rate(fuel_type.id)
                item_label = fuel_type.name
                if rate is None:
                    flash(f'No active price for {fuel_type.name}.', 'danger')
                    return redirect(url_for('sales.index'))
            elif is_shop_sale:
                other_item = OtherItem.query.get(item_key.split(':', 1)[1])
                if not other_item:
                    flash('Shop item not found.', 'danger')
                    return redirect(url_for('sales.index'))
                rate = float(other_item.sale_price or 0)
                item_label = other_item.display_name()
                if rate <= 0:
                    flash(f'No sale price for {item_label}. Set price first.', 'danger')
                    return redirect(url_for('sales.index'))

                # Shop items always decrease stock (Paid or Unpaid)
                qty_units = int(round(qty_val))
                if qty_units < 1:
                    flash('Shop item quantity must be at least 1.', 'danger')
                    return redirect(url_for('sales.index'))
                available = int(other_item.quantity or 0)
                if available < qty_units:
                    flash(
                        f'Insufficient stock for {item_label}. Available: {available}, requested: {qty_units}.',
                        'danger'
                    )
                    return redirect(url_for('sales.index'))
                other_item.quantity = available - qty_units
                qty_val = float(qty_units)
                stock_note = f'stock −{qty_units} (left {other_item.quantity})'
            else:
                flash('Invalid item selection.', 'danger')
                return redirect(url_for('sales.index'))

            amount = qty_val * rate
            if payment_status == 'unpaid':
                if customer.credit_limit is not None:
                    projected = float(customer.current_balance_due) + amount
                    if projected > float(customer.credit_limit):
                        flash(
                            f"Exceeds credit limit of PKR {float(customer.credit_limit):,.2f}.",
                            'danger'
                        )
                        return redirect(url_for('sales.index'))
                customer.current_balance_due = float(customer.current_balance_due) + amount

            db.session.add(CreditSale(
                customer_id=customer.id if customer else None,
                fuel_type_id=fuel_type.id if fuel_type else None,
                other_item_id=other_item.id if other_item else None,
                sale_date=today,
                liters=qty_val,
                rate=rate,
                amount=amount,
                payment_status=payment_status,
                remarks=remarks,
                recorded_by=current_user.id
            ))
            db.session.commit()

            if payment_status == 'unpaid' and customer:
                send_sms(customer, 'receipt', {
                    'name': customer.name,
                    'liters': f'{qty_val:.2f}',
                    'fuel': item_label,
                    'amount': f'{amount:.2f}',
                    'due': f'{float(customer.current_balance_due):.2f}'
                })

            who = customer.name if customer else 'Walk-in'
            flash(
                f'Sale recorded for {who}: {item_label} — {stock_note}.',
                'success'
            )
            return redirect(url_for('sales.index'))

        flash('Unknown action.', 'danger')
        return redirect(url_for('sales.index'))

    # GET
    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()
    fuel_prices = {ft.id: _fuel_rate(ft.id) or 0.0 for ft in fuel_types}
    machines_by_fuel = {ft.id: _machines_for_fuel(ft.id) for ft in fuel_types}
    day_totals = _day_fuel_totals(today)

    # Existing readings today for form defaults
    today_readings = {}
    for r in MeterReading.query.filter_by(reading_date=today).all():
        if r.machine_id:
            today_readings[r.machine_id] = r

    customers = Customer.query.order_by(Customer.name.asc()).all()
    shop_items = OtherItem.query.order_by(OtherItem.category.asc(), OtherItem.name.asc()).all()
    credit_sales = CreditSale.query.filter_by(sale_date=today).order_by(CreditSale.created_at.desc()).all()
    credit_total = sum(float(c.amount) for c in credit_sales)

    petrol = next((t for t in day_totals.values() if t['fuel'].name.lower() == 'petrol'), None)
    diesel = next((t for t in day_totals.values() if t['fuel'].name.lower() == 'diesel'), None)
    meter_total = sum(t['amount'] for t in day_totals.values())
    cash_sale = max(meter_total - credit_total, 0.0)

    return render_template(
        'sales/index.html',
        fuel_types=fuel_types,
        fuel_prices=fuel_prices,
        machines_by_fuel=machines_by_fuel,
        today_readings=today_readings,
        day_totals=day_totals,
        petrol_total=petrol,
        diesel_total=diesel,
        customers=customers,
        shop_items=shop_items,
        credit_sales=credit_sales,
        credit_total=credit_total,
        meter_total=meter_total,
        cash_sale=cash_sale,
        today=today
    )
