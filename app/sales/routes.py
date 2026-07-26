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
    build_period_cash_entries, build_cash_journal_summary,
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


def _fuel_rate(fuel_type_id, as_of_date=None):
    rate = fuel_rate_for(fuel_type_id, FuelPrice, as_of_date=as_of_date)
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
    return 'paid' if amount_paid >= amount else 'unpaid'


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


def _filter_args():
    """Preserve period filter after create/edit (same as Account)."""
    args = {}
    period = (request.args.get('period') or request.form.get('period') or '').strip()
    if period:
        args['period'] = period
    start_date = (request.args.get('start_date') or request.form.get('start_date') or '').strip()
    end_date = (request.args.get('end_date') or request.form.get('end_date') or '').strip()
    if start_date:
        args['start_date'] = start_date
    if end_date:
        args['end_date'] = end_date
    return args


def _sales_redirect():
    return redirect(url_for('sales.index', **_filter_args()))


@sales_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    today = _today()
    if request.method == 'GET':
        args = request.args.to_dict()
    else:
        args = {
            'period': request.form.get('period') or request.args.get('period') or 'today',
            'start_date': request.form.get('start_date') or request.args.get('start_date'),
            'end_date': request.form.get('end_date') or request.args.get('end_date'),
        }
    # Keep Sales aligned with Account: default to today so Cash in Hand matches.
    if not (args.get('period') or '').strip():
        args['period'] = 'today'
    period, start, end = parse_period(args)

    if request.method == 'POST':
        action = request.form.get('action', 'meter_sale')

        # ---------- Meter Sale ----------
        if action == 'meter_sale':
            fuel_type_id = request.form.get('fuel_type_id')
            sale_day = parse_form_date(request.form.get('entry_date'), today)
            if not fuel_type_id:
                flash('Please select a fuel type.', 'danger')
                return _sales_redirect()

            fuel_type = FuelType.query.get(fuel_type_id)
            if not fuel_type:
                flash('Fuel type not found.', 'danger')
                return _sales_redirect()

            machines = _machines_for_fuel(fuel_type.id)
            if len(machines) < 1:
                flash(f'No machines for {fuel_type.name}. Add a machine from the dropdown.', 'danger')
                return _sales_redirect()

            # Snapshot rate at save time. Same-day hike: use latest pump price.
            # Back-dated entry: use price effective on that sale date.
            if sale_day >= today:
                rate = _fuel_rate(fuel_type.id)
            else:
                rate = _fuel_rate(fuel_type.id, as_of_date=sale_day)
            if rate is None:
                flash(f'No active price for {fuel_type.name}. Set price first.', 'danger')
                return _sales_redirect()

            try:
                machine_sales = []
                segment_liters = 0.0

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
                    if liters <= 0:
                        continue
                    segment_liters += liters
                    machine_sales.append({
                        'machine': machine,
                        'opening': opening,
                        'closing': closing,
                        'liters': liters,
                    })

                if segment_liters <= 0:
                    raise ValueError('Total liters sold must be greater than zero.')

                inventory = Inventory.query.filter_by(fuel_type_id=fuel_type.id).first()
                if not inventory:
                    raise ValueError(f'No inventory record for {fuel_type.name}.')

                stock_delta = 0.0
                day_liters_before = 0.0
                for machine in machines:
                    for existing in MeterReading.query.filter_by(
                        machine_id=machine.id,
                        reading_date=sale_day,
                    ).all():
                        if existing.liters_sold is not None:
                            day_liters_before += float(existing.liters_sold)

                for item in machine_sales:
                    machine = item['machine']
                    opening = item['opening']
                    closing = item['closing']
                    liters = item['liters']

                    readings = (
                        MeterReading.query
                        .filter_by(machine_id=machine.id, reading_date=sale_day)
                        .order_by(MeterReading.id.asc())
                        .all()
                    )
                    latest = readings[-1] if readings else None

                    def _near(a, b):
                        return abs(float(a) - float(b)) < 0.01

                    match = next(
                        (r for r in readings if _near(opening, r.opening_reading)),
                        None,
                    )

                    if match is not None:
                        # Re-edit an existing segment (same opening)
                        old_liters = float(match.liters_sold or 0)
                        match.closing_reading = closing
                        match.liters_sold = liters
                        match.closed_by = current_user.id
                        match.closed_at = datetime.utcnow()
                        match.fuel_type_id = fuel_type.id
                        if match.sale_rate is None:
                            match.sale_rate = rate
                        stock_delta += liters - old_liters
                    elif latest and latest.closing_reading is not None and _near(
                        opening, latest.closing_reading
                    ):
                        # Next segment after price change — append
                        db.session.add(MeterReading(
                            machine_id=machine.id,
                            dispenser_nozzle_id=machine.name,
                            fuel_type_id=fuel_type.id,
                            opening_reading=opening,
                            closing_reading=closing,
                            liters_sold=liters,
                            sale_rate=rate,
                            reading_date=sale_day,
                            recorded_by=current_user.id,
                            closed_by=current_user.id,
                            closed_at=datetime.utcnow(),
                        ))
                        stock_delta += liters
                    elif not latest:
                        db.session.add(MeterReading(
                            machine_id=machine.id,
                            dispenser_nozzle_id=machine.name,
                            fuel_type_id=fuel_type.id,
                            opening_reading=opening,
                            closing_reading=closing,
                            liters_sold=liters,
                            sale_rate=rate,
                            reading_date=sale_day,
                            recorded_by=current_user.id,
                            closed_by=current_user.id,
                            closed_at=datetime.utcnow(),
                        ))
                        stock_delta += liters
                    elif len(readings) == 1:
                        # Legacy single-row replace for the day
                        old_liters = float(latest.liters_sold or 0)
                        latest.opening_reading = opening
                        latest.closing_reading = closing
                        latest.liters_sold = liters
                        latest.sale_rate = rate
                        latest.closed_by = current_user.id
                        latest.closed_at = datetime.utcnow()
                        latest.fuel_type_id = fuel_type.id
                        stock_delta += liters - old_liters
                    else:
                        last_close = float(latest.closing_reading or latest.opening_reading)
                        raise ValueError(
                            f'{machine.name}: to add next segment after a price change, '
                            f'set Opening to last closing ({last_close:.2f}). '
                            f'To edit a segment, keep its Opening reading.'
                        )

                available = float(inventory.current_stock_liters)
                if stock_delta > available + 0.0001:
                    raise ValueError(
                        f'Insufficient {fuel_type.name} stock. Available: {available:.2f}L, '
                        f'Additional sold: {stock_delta:.2f}L.'
                    )

                inventory.current_stock_liters = available - stock_delta
                db.session.commit()

                day_liters_after = day_liters_before + stock_delta
                amount = segment_liters * rate
                flash(
                    f'{fuel_type.name} meter segment saved for {sale_day}: {segment_liters:.2f}L '
                    f'@ PKR {rate:,.2f} (PKR {amount:,.2f}). '
                    f'Day total {day_liters_after:.2f}L. Inventory adjusted by {stock_delta:.2f}L.',
                    'success'
                )
            except ValueError as e:
                db.session.rollback()
                flash(str(e), 'danger')

            return _sales_redirect()

        # ---------- Item sale (paid / unpaid) ----------
        if action == 'credit_sale':
            customer_id = (request.form.get('customer_id') or '').strip() or None
            item_key = (request.form.get('item_key') or '').strip()
            qty_raw = request.form.get('liters')
            discount_raw = request.form.get('discount')
            remarks = (request.form.get('remarks') or '').strip() or None
            payment_status = request.form.get('payment_status', 'unpaid')
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'
            amount_paid_raw = request.form.get('amount_paid')
            sale_day = parse_form_date(request.form.get('entry_date'), today)

            if not item_key or not qty_raw:
                flash('Item and quantity are required.', 'danger')
                return _sales_redirect()

            is_fuel_sale = item_key.startswith('fuel:')
            is_shop_sale = item_key.startswith('item:')

            if is_fuel_sale and not customer_id:
                flash('Customer is required for petrol/diesel sales (walk-in not allowed).', 'danger')
                return _sales_redirect()

            if is_shop_sale and not customer_id:
                payment_status = 'paid'

            try:
                qty_val = float(qty_raw)
                if qty_val <= 0:
                    raise ValueError('Quantity must be greater than zero.')
            except ValueError as e:
                flash(f'Invalid sale input: {e}', 'danger')
                return _sales_redirect()

            customer = Customer.query.get(customer_id) if customer_id else None
            if customer_id and not customer:
                flash('Customer not found.', 'danger')
                return _sales_redirect()

            fuel_type = None
            other_item = None
            rate = None
            item_label = ''
            stock_note = 'ledger only — fuel stock unchanged'

            if is_fuel_sale:
                fuel_type = FuelType.query.get(item_key.split(':', 1)[1])
                if not fuel_type:
                    flash('Fuel type not found.', 'danger')
                    return _sales_redirect()
                rate = _fuel_rate(fuel_type.id, as_of_date=sale_day)
                item_label = fuel_type.name
                if rate is None:
                    flash(f'No active price for {fuel_type.name}.', 'danger')
                    return _sales_redirect()
            elif is_shop_sale:
                other_item = OtherItem.query.get(item_key.split(':', 1)[1])
                if not other_item:
                    flash('Shop item not found.', 'danger')
                    return _sales_redirect()
                if other_item.category == 'ft_mobile':
                    flash('Use the FT Sale card for FT Mobile Oil.', 'warning')
                    return _sales_redirect()
                rate = float(other_item.sale_price or 0)
                item_label = other_item.display_name()
                if rate <= 0:
                    flash(f'No sale price for {item_label}. Set price first.', 'danger')
                    return _sales_redirect()

                qty_units = int(round(qty_val))
                if qty_units < 1:
                    flash('Shop item quantity must be at least 1.', 'danger')
                    return _sales_redirect()
                available = int(other_item.quantity or 0)
                if available < qty_units:
                    flash(
                        f'Insufficient stock for {item_label}. Available: {available}, requested: {qty_units}.',
                        'danger'
                    )
                    return _sales_redirect()
                other_item.quantity = available - qty_units
                qty_val = float(qty_units)
                stock_note = f'stock −{qty_units} (left {other_item.quantity})'
            else:
                flash('Invalid item selection.', 'danger')
                return _sales_redirect()

            # Round to 2 dp so backend totals match what the sale form displays.
            gross = round(qty_val * rate, 2)
            # Discount applies to full sale total for all item types (fuel + shop).
            try:
                discount = float(discount_raw or 0)
            except (TypeError, ValueError):
                flash('Invalid discount amount.', 'danger')
                return _sales_redirect()
            if discount < 0:
                flash('Discount cannot be negative.', 'danger')
                return _sales_redirect()
            if discount > gross:
                flash(
                    f'Discount PKR {discount:,.2f} cannot exceed sale total PKR {gross:,.2f}.',
                    'danger'
                )
                return _sales_redirect()

            amount = round(max(gross - discount, 0.0), 2)

            # Resolve cash paid now (paid / unpaid only; paid may include overpayment)
            overpayment = 0.0
            try:
                if payment_status == 'paid':
                    if amount_paid_raw not in (None, ''):
                        cash_received = float(amount_paid_raw)
                        if cash_received < 0:
                            raise ValueError('Amount paid cannot be negative.')
                        # Allow a 1-paisa rounding tolerance (qty × rate float precision).
                        if cash_received < amount - 0.01:
                            raise ValueError(
                                'For Paid, cash must be at least the sale total. '
                                'Use Unpaid if the customer is not paying now.'
                            )
                        cash_received = max(cash_received, amount)
                    else:
                        cash_received = amount
                else:
                    cash_received = 0.0
            except (TypeError, ValueError) as e:
                flash(f'Invalid amount paid: {e}', 'danger')
                return _sales_redirect()

            amount_paid = min(cash_received, amount)
            overpayment = max(cash_received - amount, 0.0)
            credit_amt = max(amount - amount_paid, 0.0)
            payment_status = _payment_status(amount, amount_paid)

            if overpayment > 0:
                if not customer:
                    flash('Customer is required when cash paid exceeds the sale total.', 'danger')
                    return _sales_redirect()

            if credit_amt > 0:
                if not customer:
                    flash('Customer is required when any amount is on credit.', 'danger')
                    return _sales_redirect()
                if customer.credit_limit is not None:
                    projected = float(customer.current_balance_due) + credit_amt
                    if projected > float(customer.credit_limit):
                        flash(
                            f"Exceeds credit limit of PKR {float(customer.credit_limit):,.2f}.",
                            'danger'
                        )
                        return _sales_redirect()

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
            return _sales_redirect()

        # ---------- FT Mobile Oil sale (liters stock, paid / unpaid) ----------
        if action == 'ft_sale':
            customer_id = (request.form.get('customer_id') or '').strip() or None
            item_id = (request.form.get('ft_item_id') or '').strip()
            qty_raw = request.form.get('liters')
            discount_raw = request.form.get('discount')
            remarks = (request.form.get('remarks') or '').strip() or None
            payment_status = request.form.get('payment_status', 'paid')
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'
            amount_paid_raw = request.form.get('amount_paid')
            sale_day = parse_form_date(request.form.get('entry_date'), today)

            if not item_id or not qty_raw:
                flash('FT Mobile Oil item and liters are required.', 'danger')
                return _sales_redirect()

            if not customer_id:
                payment_status = 'paid'

            try:
                liters_val = float(qty_raw)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except ValueError as e:
                flash(f'Invalid sale input: {e}', 'danger')
                return _sales_redirect()

            customer = Customer.query.get(customer_id) if customer_id else None
            if customer_id and not customer:
                flash('Customer not found.', 'danger')
                return _sales_redirect()

            other_item = OtherItem.query.get(item_id)
            if not other_item or other_item.category != 'ft_mobile':
                flash('FT Mobile Oil item not found.', 'danger')
                return _sales_redirect()

            rate = float(other_item.sale_price or 0)
            cost_rate = float(other_item.cost_price or 0)
            item_label = f"FT Mobile Oil — {other_item.display_name()}"
            if rate <= 0:
                flash(f'No sale price for {item_label}. Set price in inventory first.', 'danger')
                return _sales_redirect()

            available = float(other_item.liters or 0)
            if available < liters_val:
                flash(
                    f'Insufficient FT Mobile Oil stock for {other_item.name}. '
                    f'Available: {available:.2f}L, requested: {liters_val:.2f}L.',
                    'danger',
                )
                return _sales_redirect()

            gross = round(liters_val * rate, 2)
            # Discount applies to the full FT sale total.
            try:
                discount = float(discount_raw or 0)
            except (TypeError, ValueError):
                flash('Invalid discount amount.', 'danger')
                return _sales_redirect()
            if discount < 0:
                flash('Discount cannot be negative.', 'danger')
                return _sales_redirect()
            if discount > gross:
                flash(
                    f'Discount PKR {discount:,.2f} cannot exceed sale total PKR {gross:,.2f}.',
                    'danger'
                )
                return _sales_redirect()

            amount = round(max(gross - discount, 0.0), 2)

            amount_paid = amount if payment_status == 'paid' else 0.0

            credit_amt = max(amount - amount_paid, 0.0)
            payment_status = _payment_status(amount, amount_paid)

            if credit_amt > 0:
                if not customer:
                    flash('Customer is required when any amount is on credit.', 'danger')
                    return _sales_redirect()
                if customer.credit_limit is not None:
                    projected = float(customer.current_balance_due) + credit_amt
                    if projected > float(customer.credit_limit):
                        flash(
                            f"Exceeds credit limit of PKR {float(customer.credit_limit):,.2f}.",
                            'danger',
                        )
                        return _sales_redirect()
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
            return _sales_redirect()

        # ---------- Customer advance / loan removed from Sales UI ----------
        if action in ('advance', 'loan'):
            flash('Advance and loan are not available on Sales. Use Customers later.', 'warning')
            return _sales_redirect()

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
                return _sales_redirect()

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
            return _sales_redirect()

        # ---------- Edit / delete period entry (CreditSale) ----------
        if action == 'edit_entry':
            entry = CreditSale.query.get(request.form.get('entry_id'))
            if not entry:
                flash('Entry not found.', 'danger')
                return _sales_redirect()
            try:
                edit_credit_sale(entry, request.form)
                db.session.commit()
                flash(f'Entry #{entry.id} updated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return _sales_redirect()

        if action == 'delete_entry':
            entry = CreditSale.query.get(request.form.get('entry_id'))
            if not entry:
                flash('Entry not found.', 'danger')
                return _sales_redirect()
            try:
                label = f'{entry.entry_type} #{entry.id}'
                delete_credit_sale(entry)
                db.session.commit()
                flash(f'Deleted {label}. Balance and stock recalculated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return _sales_redirect()

        flash('Unknown action.', 'danger')
        return _sales_redirect()

    # GET
    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()
    fuel_prices = {ft.id: _fuel_rate(ft.id) or 0.0 for ft in fuel_types}
    machines_by_fuel = {ft.id: _machines_for_fuel(ft.id) for ft in fuel_types}

    today_readings = {}
    for r in (
        MeterReading.query
        .filter_by(reading_date=today)
        .order_by(MeterReading.id.asc())
        .all()
    ):
        if r.machine_id:
            # Keep latest segment per machine for form prefill
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
    editable_entries = [
        e for e in stats['entries']
        if (getattr(e, 'entry_type', None) or 'sale').lower() != 'opening'
    ]
    period_entries, entries_pagination = paginate(editable_entries, entries_page, PER_PAGE)
    journal_page = request.args.get('journal_page', 1)
    journal_entries, journal_pagination = paginate(build_period_cash_entries(stats), journal_page, PER_PAGE)
    cash_summary = build_cash_journal_summary(stats)
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
        journal_entries=journal_entries,
        journal_pagination=journal_pagination,
        cash_summary=cash_summary,
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
