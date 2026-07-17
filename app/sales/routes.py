from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.sales import sales_bp
from app.models import (
    db, FuelType, FuelPrice, Inventory, MeterReading, Customer,
    Machine, CreditSale, OtherItem, Expense, Payment, DailyCashCount
)
from app.utils import (
    parse_period, PERIOD_CHOICES, compute_period_stats, fuel_rate_for,
    paginate, parse_form_date, datetime_from_date, infer_sale_overpayments,
)
from app.services.entries import (
    EntryError, edit_credit_sale, delete_credit_sale,
)
from app.customers.service import recalculate_customer_balance
from datetime import datetime
from types import SimpleNamespace

PER_PAGE = 15


def _today():
    return datetime.utcnow().date()


def _fuel_rate(fuel_type_id):
    rate = fuel_rate_for(fuel_type_id, FuelPrice)
    return rate if rate > 0 else None


def _machines_for_fuel(fuel_type_id):
    return (
        Machine.query
        .filter_by(fuel_type_id=fuel_type_id, is_active=True)
        .order_by(Machine.name.asc())
        .all()
    )


def _payment_status(amount, amount_paid):
    if amount_paid <= 0:
        return 'unpaid'
    if amount_paid >= amount:
        return 'paid'
    return 'partial'


def _apply_sale_overpayment(customer, overpayment, sale_day, item_label, recorded_by):
    """
    Cash above sale total: clear existing balance due first (Payment entry),
    then post remainder as customer advance.
    Returns (cleared_due, advance_amount).
    """
    if overpayment <= 0 or not customer:
        return 0.0, 0.0

    due_now = max(float(customer.current_balance_due or 0), 0.0)
    cleared = min(overpayment, due_now)
    advance_amt = max(overpayment - cleared, 0.0)

    if cleared > 0:
        db.session.add(Payment(
            customer_id=customer.id,
            amount_paid=cleared,
            payment_date=datetime_from_date(sale_day),
            method='Cash',
            note=f'Overpayment from sale ({item_label}) — cleared due',
        ))

    if advance_amt > 0:
        db.session.add(CreditSale(
            customer_id=customer.id,
            sale_date=sale_day,
            liters=0,
            rate=0,
            amount=advance_amt,
            amount_paid=advance_amt,
            entry_type='advance',
            payment_status='paid',
            remarks=f'Overpayment from sale ({item_label})',
            recorded_by=recorded_by,
        ))

    return cleared, advance_amt


@sales_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    today = _today()
    period, start, end = parse_period(request.args if request.method == 'GET' else {
        'period': request.form.get('period') or request.args.get('period') or 'today',
        'start_date': request.form.get('start_date') or request.args.get('start_date'),
        'end_date': request.form.get('end_date') or request.args.get('end_date'),
    })

    if request.method == 'POST':
        action = request.form.get('action', 'meter_sale')

        # ---------- Meter Sale ----------
        if action == 'meter_sale':
            fuel_type_id = request.form.get('fuel_type_id')
            sale_day = parse_form_date(request.form.get('entry_date'), today)
            if not fuel_type_id:
                flash('Please select a fuel type.', 'danger')
                return redirect(url_for('sales.index'))

            fuel_type = FuelType.query.get(fuel_type_id)
            if not fuel_type:
                flash('Fuel type not found.', 'danger')
                return redirect(url_for('sales.index'))

            machines = _machines_for_fuel(fuel_type.id)
            if len(machines) < 1:
                flash(f'No machines for {fuel_type.name}. Add a machine from the dropdown.', 'danger')
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

                previous_liters = 0.0
                for machine in machines:
                    existing = MeterReading.query.filter_by(
                        machine_id=machine.id,
                        reading_date=sale_day
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

                for item in machine_sales:
                    machine = item['machine']
                    reading = MeterReading.query.filter_by(
                        machine_id=machine.id,
                        reading_date=sale_day
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
                            reading_date=sale_day,
                            recorded_by=current_user.id,
                            closed_by=current_user.id,
                            closed_at=datetime.utcnow()
                        ))

                inventory.current_stock_liters = available - delta_liters
                db.session.commit()

                amount = total_liters * rate
                flash(
                    f'{fuel_type.name} meter sale saved for {sale_day}: {total_liters:.2f}L '
                    f'(PKR {amount:,.2f}). Inventory adjusted by {delta_liters:.2f}L.',
                    'success'
                )
            except ValueError as e:
                db.session.rollback()
                flash(str(e), 'danger')

            return redirect(url_for('sales.index'))

        # ---------- Item sale (paid / unpaid / partial) ----------
        if action == 'credit_sale':
            customer_id = (request.form.get('customer_id') or '').strip() or None
            item_key = (request.form.get('item_key') or '').strip()
            qty_raw = request.form.get('liters')
            discount_raw = request.form.get('discount')
            remarks = (request.form.get('remarks') or '').strip() or None
            payment_status = request.form.get('payment_status', 'unpaid')
            amount_paid_raw = request.form.get('amount_paid')
            sale_day = parse_form_date(request.form.get('entry_date'), today)

            if not item_key or not qty_raw:
                flash('Item and quantity are required.', 'danger')
                return redirect(url_for('sales.index'))

            is_fuel_sale = item_key.startswith('fuel:')
            is_shop_sale = item_key.startswith('item:')

            if is_fuel_sale and not customer_id:
                flash('Customer is required for petrol/diesel sales (walk-in not allowed).', 'danger')
                return redirect(url_for('sales.index'))

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
                if other_item.category == 'ft_mobile':
                    flash('Use the FT Sale card for FT Mobile Oil.', 'warning')
                    return redirect(url_for('sales.index'))
                rate = float(other_item.sale_price or 0)
                item_label = other_item.display_name()
                if rate <= 0:
                    flash(f'No sale price for {item_label}. Set price first.', 'danger')
                    return redirect(url_for('sales.index'))

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

            gross = qty_val * rate
            # Discount applies to full sale total for all item types (fuel + shop).
            try:
                discount = float(discount_raw or 0)
            except (TypeError, ValueError):
                flash('Invalid discount amount.', 'danger')
                return redirect(url_for('sales.index'))
            if discount < 0:
                flash('Discount cannot be negative.', 'danger')
                return redirect(url_for('sales.index'))
            if discount > gross:
                flash(
                    f'Discount PKR {discount:,.2f} cannot exceed sale total PKR {gross:,.2f}.',
                    'danger'
                )
                return redirect(url_for('sales.index'))

            amount = max(gross - discount, 0.0)

            # Resolve cash paid now (supports underpay, full pay, and overpayment)
            overpayment = 0.0
            try:
                if payment_status == 'paid':
                    if amount_paid_raw not in (None, ''):
                        cash_received = float(amount_paid_raw)
                        if cash_received < 0:
                            raise ValueError('Amount paid cannot be negative.')
                        if cash_received < amount:
                            raise ValueError(
                                'For Paid, cash must be at least the sale total. '
                                'Use Partial if paying less, or enter more to credit the customer.'
                            )
                    else:
                        cash_received = amount
                elif payment_status == 'partial':
                    cash_received = float(amount_paid_raw or 0)
                    if cash_received <= 0:
                        raise ValueError('Cash paid must be greater than 0.')
                    # Allow cash > total (overpayment → customer account).
                    # Allow cash < total (normal partial / credit).
                else:
                    cash_received = 0.0
            except (TypeError, ValueError) as e:
                flash(f'Invalid amount paid: {e}', 'danger')
                return redirect(url_for('sales.index'))

            amount_paid = min(cash_received, amount)
            overpayment = max(cash_received - amount, 0.0)
            credit_amt = max(amount - amount_paid, 0.0)
            payment_status = _payment_status(amount, amount_paid)

            if overpayment > 0:
                if not customer:
                    flash('Customer is required when cash paid exceeds the sale total.', 'danger')
                    return redirect(url_for('sales.index'))

            if credit_amt > 0:
                if not customer:
                    flash('Customer is required when any amount is on credit.', 'danger')
                    return redirect(url_for('sales.index'))
                if customer.credit_limit is not None:
                    projected = float(customer.current_balance_due) + credit_amt
                    if projected > float(customer.credit_limit):
                        flash(
                            f"Exceeds credit limit of PKR {float(customer.credit_limit):,.2f}.",
                            'danger'
                        )
                        return redirect(url_for('sales.index'))

            db.session.add(CreditSale(
                customer_id=customer.id if customer else None,
                fuel_type_id=fuel_type.id if fuel_type else None,
                other_item_id=other_item.id if other_item else None,
                sale_date=sale_day,
                liters=qty_val,
                rate=rate,
                amount=amount,
                discount=discount,
                amount_paid=amount_paid,
                overpayment=overpayment,
                entry_type='sale',
                payment_status=payment_status,
                remarks=remarks,
                recorded_by=current_user.id
            ))
            db.session.flush()

            cleared_due = 0.0
            advance_amt = 0.0
            if overpayment > 0 and customer:
                # Recalc after sale so we know current due before applying overpay
                recalculate_customer_balance(customer)
                cleared_due, advance_amt = _apply_sale_overpayment(
                    customer, overpayment, sale_day, item_label, current_user.id
                )
                db.session.flush()

            if customer:
                recalculate_customer_balance(customer)

            db.session.commit()

            who = customer.name if customer else 'Walk-in'
            disc_note = f', discount {discount:,.2f}' if discount > 0 else ''
            over_note = ''
            if overpayment > 0:
                parts = []
                if cleared_due > 0:
                    parts.append(f'cleared due {cleared_due:,.2f}')
                if advance_amt > 0:
                    parts.append(f'advance +{advance_amt:,.2f}')
                over_note = f' — overpay {overpayment:,.2f}' + (f' ({", ".join(parts)})' if parts else '')
            flash(
                f'Sale recorded for {who}: {item_label} — total {amount:,.2f}{disc_note} — '
                f'paid {cash_received:,.2f}, credit {credit_amt:,.2f}{over_note} — {stock_note}.',
                'success'
            )
            return redirect(url_for('sales.index'))

        # ---------- FT Mobile Oil sale (liters stock, paid / unpaid / partial) ----------
        if action == 'ft_sale':
            customer_id = (request.form.get('customer_id') or '').strip() or None
            item_id = (request.form.get('ft_item_id') or '').strip()
            qty_raw = request.form.get('liters')
            discount_raw = request.form.get('discount')
            remarks = (request.form.get('remarks') or '').strip() or None
            payment_status = request.form.get('payment_status', 'paid')
            amount_paid_raw = request.form.get('amount_paid')
            sale_day = parse_form_date(request.form.get('entry_date'), today)

            if not item_id or not qty_raw:
                flash('FT Mobile Oil item and liters are required.', 'danger')
                return redirect(url_for('sales.index'))

            if not customer_id:
                payment_status = 'paid'

            try:
                liters_val = float(qty_raw)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except ValueError as e:
                flash(f'Invalid sale input: {e}', 'danger')
                return redirect(url_for('sales.index'))

            customer = Customer.query.get(customer_id) if customer_id else None
            if customer_id and not customer:
                flash('Customer not found.', 'danger')
                return redirect(url_for('sales.index'))

            other_item = OtherItem.query.get(item_id)
            if not other_item or other_item.category != 'ft_mobile':
                flash('FT Mobile Oil item not found.', 'danger')
                return redirect(url_for('sales.index'))

            rate = float(other_item.sale_price or 0)
            cost_rate = float(other_item.cost_price or 0)
            item_label = f"FT Mobile Oil — {other_item.display_name()}"
            if rate <= 0:
                flash(f'No sale price for {item_label}. Set price in inventory first.', 'danger')
                return redirect(url_for('sales.index'))

            available = float(other_item.liters or 0)
            if available < liters_val:
                flash(
                    f'Insufficient FT Mobile Oil stock for {other_item.name}. '
                    f'Available: {available:.2f}L, requested: {liters_val:.2f}L.',
                    'danger',
                )
                return redirect(url_for('sales.index'))

            gross = liters_val * rate
            # Discount applies to the full FT sale total.
            try:
                discount = float(discount_raw or 0)
            except (TypeError, ValueError):
                flash('Invalid discount amount.', 'danger')
                return redirect(url_for('sales.index'))
            if discount < 0:
                flash('Discount cannot be negative.', 'danger')
                return redirect(url_for('sales.index'))
            if discount > gross:
                flash(
                    f'Discount PKR {discount:,.2f} cannot exceed sale total PKR {gross:,.2f}.',
                    'danger'
                )
                return redirect(url_for('sales.index'))

            amount = max(gross - discount, 0.0)

            if payment_status == 'paid':
                amount_paid = amount
            elif payment_status == 'partial':
                try:
                    amount_paid = float(amount_paid_raw or 0)
                except (TypeError, ValueError):
                    flash('Invalid amount paid.', 'danger')
                    return redirect(url_for('sales.index'))
                if amount_paid <= 0 or amount_paid >= amount:
                    flash('Partial payment must be greater than 0 and less than total amount.', 'danger')
                    return redirect(url_for('sales.index'))
            else:
                amount_paid = 0.0

            credit_amt = max(amount - amount_paid, 0.0)
            payment_status = _payment_status(amount, amount_paid)

            if credit_amt > 0:
                if not customer:
                    flash('Customer is required when any amount is on credit.', 'danger')
                    return redirect(url_for('sales.index'))
                if customer.credit_limit is not None:
                    projected = float(customer.current_balance_due) + credit_amt
                    if projected > float(customer.credit_limit):
                        flash(
                            f"Exceeds credit limit of PKR {float(customer.credit_limit):,.2f}.",
                            'danger',
                        )
                        return redirect(url_for('sales.index'))
                customer.current_balance_due = float(customer.current_balance_due) + credit_amt

            other_item.liters = available - liters_val

            db.session.add(CreditSale(
                customer_id=customer.id if customer else None,
                fuel_type_id=None,
                other_item_id=other_item.id,
                sale_date=sale_day,
                liters=liters_val,
                rate=rate,
                amount=amount,
                discount=discount,
                amount_paid=amount_paid,
                entry_type='sale',
                payment_status=payment_status,
                remarks=remarks or f'FT sale · cost {cost_rate:.2f}/L',
                recorded_by=current_user.id,
            ))
            db.session.commit()

            who = customer.name if customer else 'Walk-in'
            disc_note = f', discount {discount:,.2f}' if discount > 0 else ''
            flash(
                f'FT sale for {who}: {item_label} {liters_val:.2f}L — total {amount:,.2f}{disc_note} — '
                f'paid {amount_paid:,.2f}, credit {credit_amt:,.2f} — stock left {float(other_item.liters):.2f}L.',
                'success',
            )
            return redirect(url_for('sales.index'))

        # ---------- Customer advance / loan removed from Sales UI ----------
        if action in ('advance', 'loan'):
            flash('Advance and loan are not available on Sales. Use Customers later.', 'warning')
            return redirect(url_for('sales.index'))

        # ---------- Cash in hand count (journal) ----------
        if action == 'cash_count':
            amount_raw = request.form.get('cash_in_hand')
            note = (request.form.get('note') or '').strip() or None
            try:
                cash_val = float(amount_raw)
                if cash_val < 0:
                    raise ValueError('Cash cannot be negative.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid cash amount: {e}', 'danger')
                return redirect(url_for('sales.index'))

            row = DailyCashCount.query.filter_by(count_date=today).first()
            if row:
                row.cash_in_hand = cash_val
                row.note = note
                row.recorded_by = current_user.id
            else:
                db.session.add(DailyCashCount(
                    count_date=today,
                    cash_in_hand=cash_val,
                    note=note,
                    recorded_by=current_user.id,
                ))
            db.session.commit()
            flash(f'Cash in hand for today set to PKR {cash_val:,.2f}.', 'success')
            return redirect(url_for('sales.index'))

        # ---------- Edit / delete period entry (CreditSale) ----------
        if action == 'edit_entry':
            entry = CreditSale.query.get(request.form.get('entry_id'))
            if not entry:
                flash('Entry not found.', 'danger')
                return redirect(url_for('sales.index'))
            try:
                edit_credit_sale(entry, request.form)
                db.session.commit()
                flash(f'Entry #{entry.id} updated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return redirect(url_for('sales.index'))

        if action == 'delete_entry':
            entry = CreditSale.query.get(request.form.get('entry_id'))
            if not entry:
                flash('Entry not found.', 'danger')
                return redirect(url_for('sales.index'))
            try:
                label = f'{entry.entry_type} #{entry.id}'
                delete_credit_sale(entry)
                db.session.commit()
                flash(f'Deleted {label}. Balance and stock recalculated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return redirect(url_for('sales.index'))

        flash('Unknown action.', 'danger')
        return redirect(url_for('sales.index'))

    # GET
    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()
    fuel_prices = {ft.id: _fuel_rate(ft.id) or 0.0 for ft in fuel_types}
    machines_by_fuel = {ft.id: _machines_for_fuel(ft.id) for ft in fuel_types}

    today_readings = {}
    for r in MeterReading.query.filter_by(reading_date=today).all():
        if r.machine_id:
            today_readings[r.machine_id] = r

    customers = Customer.query.order_by(Customer.name.asc()).all()
    shop_items = (
        OtherItem.query
        .filter(OtherItem.category != 'ft_mobile')
        .order_by(OtherItem.category.asc(), OtherItem.name.asc())
        .all()
    )
    ft_items = (
        OtherItem.query
        .filter_by(category='ft_mobile')
        .order_by(OtherItem.name.asc())
        .all()
    )

    models_ns = SimpleNamespace(
        MeterReading=MeterReading,
        FuelType=FuelType,
        FuelPrice=FuelPrice,
        CreditSale=CreditSale,
        Expense=Expense,
        Payment=Payment,
        DailyCashCount=DailyCashCount,
        Customer=Customer,
    )
    stats = compute_period_stats(
        start, end, models_ns,
        include_opening_credit=(period == 'all'),
    )

    day_cash = DailyCashCount.query.filter_by(count_date=today).first()

    entries_page = request.args.get('page', 1)
    period_entries, entries_pagination = paginate(stats['entries'], entries_page, PER_PAGE)
    sale_overs = infer_sale_overpayments(stats['entries'], stats.get('payments'))

    return render_template(
        'sales/index.html',
        fuel_types=fuel_types,
        fuel_prices=fuel_prices,
        machines_by_fuel=machines_by_fuel,
        today_readings=today_readings,
        customers=customers,
        shop_items=shop_items,
        ft_items=ft_items,
        stats=stats,
        period_entries=period_entries,
        entries_pagination=entries_pagination,
        sale_overs=sale_overs,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        today=today.isoformat() if hasattr(today, 'isoformat') else today,
        day_cash=day_cash,
    )


# ---------- Quick-add APIs (dropdown "Add new") ----------

@sales_bp.route('/api/quick/customer', methods=['POST'])
@login_required
def quick_customer():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    phone = (data.get('phone') or '').strip() or None
    if not name:
        return jsonify({'ok': False, 'error': 'Name is required'}), 400
    customer = Customer(name=name, phone=phone, current_balance_due=0)
    db.session.add(customer)
    db.session.commit()
    label = f"{customer.name}{' · ' + customer.phone if customer.phone else ''} (Due: PKR 0.00)"
    return jsonify({'ok': True, 'id': customer.id, 'text': label, 'phone': customer.phone or ''})


@sales_bp.route('/api/quick/fuel', methods=['POST'])
@login_required
def quick_fuel():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Fuel name is required'}), 400
    for ft in FuelType.query.all():
        if ft.name.lower() == name.lower():
            return jsonify({'ok': True, 'id': ft.id, 'text': ft.name, 'rate': _fuel_rate(ft.id) or 0})
    fuel = FuelType(name=name, unit='Liter')
    db.session.add(fuel)
    db.session.flush()
    db.session.add(Inventory(fuel_type_id=fuel.id, current_stock_liters=0, reorder_threshold=0))
    db.session.commit()
    return jsonify({'ok': True, 'id': fuel.id, 'text': fuel.name, 'rate': 0})


@sales_bp.route('/api/quick/item', methods=['POST'])
@login_required
def quick_item():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    try:
        sale_price = float(data.get('sale_price') or 0)
    except (TypeError, ValueError):
        sale_price = 0
    if not name:
        return jsonify({'ok': False, 'error': 'Item name is required'}), 400
    item = OtherItem(
        category='other',
        name=name,
        sale_price=sale_price,
        cost_price=0,
        quantity=0,
    )
    db.session.add(item)
    db.session.commit()
    text = f"Other — {item.display_name()} (PKR {sale_price:,.2f}) · 0 left"
    return jsonify({
        'ok': True,
        'id': item.id,
        'value': f'item:{item.id}',
        'text': text,
        'rate': sale_price,
    })


@sales_bp.route('/api/quick/machine', methods=['POST'])
@login_required
def quick_machine():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    fuel_type_id = data.get('fuel_type_id')
    if not name or not fuel_type_id:
        return jsonify({'ok': False, 'error': 'Machine name and fuel type are required'}), 400
    fuel = FuelType.query.get(fuel_type_id)
    if not fuel:
        return jsonify({'ok': False, 'error': 'Fuel type not found'}), 404
    if Machine.query.filter_by(name=name).first():
        return jsonify({'ok': False, 'error': 'Machine name already exists'}), 400
    machine = Machine(name=name, fuel_type_id=fuel.id, is_active=True)
    db.session.add(machine)
    db.session.commit()
    return jsonify({
        'ok': True,
        'id': machine.id,
        'text': machine.name,
        'name': machine.name,
        'fuel_type_id': fuel.id,
    })
