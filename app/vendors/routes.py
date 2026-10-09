from datetime import datetime

from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.models import db, Vendor, VendorPayment, ItemPurchaseLog
from app.utils import paginate, parse_form_date, datetime_from_date
from app.vendors import vendors_bp
from app.vendors.service import (
    normalize_vendor_name,
    purchase_log_total,
    recalculate_vendor_balance,
    get_or_create_vendor,
    edit_vendor_payment,
    delete_vendor_payment,
    vendor_has_linked_activity,
    purchase_log_payment_status,
)

PER_PAGE = 15


def _vendor_option_payload(vendor):
    due = float(vendor.current_balance_payable or 0)
    label = f"{vendor.name}"
    if vendor.phone:
        label += f" · {vendor.phone}"
    label += f" (Payable: PKR {due:,.2f})"
    return {'ok': True, 'id': vendor.id, 'text': label, 'phone': vendor.phone or ''}


@vendors_bp.route('/api/quick', methods=['POST'])
@login_required
def quick_vendor():
    """Create (or return existing) vendor for searchable inventory dropdown."""
    data = request.get_json(silent=True) or {}
    name = normalize_vendor_name(data.get('name'))
    phone = (data.get('phone') or '').strip() or None
    if not name:
        return jsonify({'ok': False, 'error': 'Vendor name is required'}), 400

    vendor = get_or_create_vendor(name)
    if phone and not vendor.phone:
        vendor.phone = phone
    db.session.commit()
    return jsonify(_vendor_option_payload(vendor))


@vendors_bp.route('/api/create', methods=['POST'])
@login_required
def api_create():
    """Full Register Vendor (same fields as vendors page) for AJAX modal."""
    data = request.get_json(silent=True) or {}
    if not data:
        data = request.form.to_dict()

    name = normalize_vendor_name(data.get('name'))
    phone = (data.get('phone') or '').strip() or None
    address = (data.get('address') or '').strip() or None
    contact_person = (data.get('contact_person') or '').strip() or None
    prev_raw = (data.get('previous_payable') or '').strip()

    if not name:
        return jsonify({'ok': False, 'error': 'Vendor name is required'}), 400

    if Vendor.query.filter(db.func.lower(Vendor.name) == name.lower()).first():
        return jsonify({'ok': False, 'error': f"Vendor '{name}' already exists."}), 400

    prev_payable = 0.0
    if prev_raw:
        try:
            prev_payable = float(prev_raw)
            if prev_payable < 0:
                raise ValueError('Opening payable cannot be negative.')
        except ValueError as e:
            return jsonify({'ok': False, 'error': f'Invalid opening payable: {e}'}), 400

    vendor = Vendor(
        name=name,
        phone=phone,
        address=address,
        contact_person=contact_person,
        previous_payable=prev_payable if prev_payable > 0 else None,
        current_balance_payable=prev_payable,
    )
    db.session.add(vendor)
    db.session.commit()
    return jsonify(_vendor_option_payload(vendor))


@vendors_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'create':
            name = normalize_vendor_name(request.form.get('name'))
            phone = (request.form.get('phone') or '').strip() or None
            address = (request.form.get('address') or '').strip() or None
            contact_person = (request.form.get('contact_person') or '').strip() or None
            prev_raw = (request.form.get('previous_payable') or '').strip()
            entry_date = parse_form_date(request.form.get('entry_date'))

            if not name:
                flash('Vendor name is required.', 'danger')
                return redirect(url_for('vendors.index'))

            if Vendor.query.filter(db.func.lower(Vendor.name) == name.lower()).first():
                flash(f"Vendor '{name}' already exists.", 'danger')
                return redirect(url_for('vendors.index'))

            prev_payable = 0.0
            if prev_raw:
                try:
                    prev_payable = float(prev_raw)
                    if prev_payable < 0:
                        raise ValueError('Opening payable cannot be negative.')
                except ValueError as e:
                    flash(f'Invalid opening payable: {e}', 'danger')
                    return redirect(url_for('vendors.index'))

            vendor = Vendor(
                name=name,
                phone=phone,
                address=address,
                contact_person=contact_person,
                previous_payable=prev_payable if prev_payable > 0 else None,
                current_balance_payable=prev_payable,
            )
            db.session.add(vendor)
            db.session.commit()
            flash(f"Vendor '{name}' registered successfully.", 'success')

        elif action == 'edit':
            vendor_id = request.form.get('vendor_id')
            vendor = Vendor.query.get(vendor_id)
            if vendor:
                new_name = normalize_vendor_name(request.form.get('name'))
                if not new_name:
                    flash('Vendor name is required.', 'danger')
                    return redirect(url_for('vendors.index'))

                duplicate = Vendor.query.filter(
                    db.func.lower(Vendor.name) == new_name.lower(),
                    Vendor.id != vendor.id,
                ).first()
                if duplicate:
                    flash(f"Another vendor named '{new_name}' already exists.", 'danger')
                    return redirect(url_for('vendors.index'))

                vendor.name = new_name
                vendor.phone = (request.form.get('phone') or '').strip() or None
                vendor.address = (request.form.get('address') or '').strip() or None
                vendor.contact_person = (request.form.get('contact_person') or '').strip() or None

                prev_raw = (request.form.get('previous_payable') or '').strip()
                if prev_raw != '' or 'previous_payable' in request.form:
                    try:
                        prev_payable = float(prev_raw or 0)
                        if prev_payable < 0:
                            raise ValueError('Opening payable cannot be negative.')
                    except ValueError as e:
                        flash(f'Invalid opening payable: {e}', 'danger')
                        return redirect(url_for('vendors.index'))
                    vendor.previous_payable = prev_payable if prev_payable > 0 else None
                    recalculate_vendor_balance(vendor)

                db.session.commit()
                flash(f"Vendor details updated for '{vendor.name}'. Payable recalculated.", 'success')

        elif action == 'delete':
            vendor = Vendor.query.get(request.form.get('vendor_id'))
            if not vendor:
                flash('Vendor not found.', 'danger')
                return redirect(url_for('vendors.index'))
            if vendor_has_linked_activity(vendor):
                flash(
                    f'Cannot delete “{vendor.name}”. This vendor already has purchases or payments. '
                    f'Clear linked ledger activity first.',
                    'danger',
                )
                return redirect(url_for('vendors.index'))
            name = vendor.name
            db.session.delete(vendor)
            db.session.commit()
            flash(f'Deleted vendor “{name}”.', 'success')

        return redirect(url_for('vendors.index'))

    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('filter', 'all')

    query = Vendor.query
    if search_query:
        query = query.filter(
            Vendor.name.like(f'%{search_query}%')
            | Vendor.phone.like(f'%{search_query}%')
            | Vendor.contact_person.like(f'%{search_query}%')
        )

    if status_filter == 'payable':
        query = query.filter(Vendor.current_balance_payable > 0)
    elif status_filter == 'settled':
        query = query.filter(Vendor.current_balance_payable <= 0)

    vendors, vendors_pagination = paginate(
        query.order_by(Vendor.name.asc()),
        request.args.get('page', 1),
        PER_PAGE,
    )

    vendor_linked = {v.id: vendor_has_linked_activity(v) for v in vendors}

    from app.charts_data import vendors_listing_series

    return render_template(
        'vendors/index.html',
        vendors=vendors,
        vendors_pagination=vendors_pagination,
        vendor_linked=vendor_linked,
        search=search_query,
        filter=status_filter,
        today=datetime.utcnow().date().isoformat(),
        chart_series=vendors_listing_series(),
    )


@vendors_bp.route('/ledger/<int:vendor_id>', methods=['GET', 'POST'])
@login_required
def ledger(vendor_id):
    vendor = Vendor.query.get_or_404(vendor_id)

    if request.method == 'POST':
        action = (request.form.get('action') or 'payment').strip().lower()

        if action == 'edit_payment':
            payment = VendorPayment.query.filter_by(
                id=request.form.get('payment_id'), vendor_id=vendor.id
            ).first()
            if not payment:
                flash('Payment not found.', 'danger')
                return redirect(url_for('vendors.ledger', vendor_id=vendor.id))
            try:
                edit_vendor_payment(payment, request.form)
                db.session.commit()
                flash('Payment updated. Payable recalculated.', 'success')
            except ValueError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return redirect(url_for('vendors.ledger', vendor_id=vendor.id))

        if action == 'delete_payment':
            payment = VendorPayment.query.filter_by(
                id=request.form.get('payment_id'), vendor_id=vendor.id
            ).first()
            if not payment:
                flash('Payment not found.', 'danger')
                return redirect(url_for('vendors.ledger', vendor_id=vendor.id))
            delete_vendor_payment(payment)
            db.session.commit()
            flash('Payment deleted. Payable recalculated.', 'success')
            return redirect(url_for('vendors.ledger', vendor_id=vendor.id))

        amount = request.form.get('amount_paid')
        method = request.form.get('method', 'Cash')
        note = (request.form.get('note') or '').strip() or None
        entry_date = parse_form_date(request.form.get('entry_date'))

        if not amount:
            flash('Payment amount is required.', 'danger')
            return redirect(url_for('vendors.ledger', vendor_id=vendor.id))

        try:
            amt_val = float(amount)
            if amt_val <= 0:
                raise ValueError('Payment amount must be greater than zero.')
        except ValueError as e:
            flash(f'Invalid payment amount: {e}', 'danger')
            return redirect(url_for('vendors.ledger', vendor_id=vendor.id))

        payment = VendorPayment(
            vendor_id=vendor.id,
            amount_paid=amt_val,
            payment_date=datetime_from_date(entry_date),
            method=method,
            note=note,
        )
        db.session.add(payment)
        db.session.flush()
        recalculate_vendor_balance(vendor)
        db.session.commit()

        flash(
            f"Recorded payment of PKR {amt_val:,.2f} to {vendor.name}. "
            f"Balance payable: PKR {float(vendor.current_balance_payable):,.2f}.",
            'success',
        )
        return redirect(url_for('vendors.ledger', vendor_id=vendor.id))

    # Always rebuild stored payable so KPI matches the statement.
    recalculate_vendor_balance(vendor)
    db.session.commit()

    ledger_entries = []
    AUTO_PAY_NOTE = 'Paid with inventory purchase:'

    if vendor.previous_payable and float(vendor.previous_payable) > 0:
        ledger_entries.append({
            'date': vendor.created_at or datetime.min,
            'type': 'purchase',
            'desc': 'Previous / opening payable balance',
            'debit': float(vendor.previous_payable),
            'credit': 0.0,
            'ref_id': 'Opening',
            'pay_type': 'opening',
            'payment_status': 'opening',
        })

    purchases = ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).order_by(
        ItemPurchaseLog.entry_date.asc(), ItemPurchaseLog.id.asc()
    ).all()

    payments = VendorPayment.query.filter_by(vendor_id=vendor.id).order_by(
        VendorPayment.payment_date.asc(), VendorPayment.id.asc()
    ).all()

    # One statement line per product. Paid-with-purchase merges buy + settle
    # on that same row (no extra payment line). Products are never combined.
    used_payment_ids = set()

    def _qty_desc(log):
        if log.category == 'fuel' or log.category == 'ft_mobile':
            return f"{float(log.liters or 0):,.2f} L"
        return f"{int(log.quantity or 0)} pcs"

    def _purchase_desc(log):
        desc = f"{log.item_name} — {_qty_desc(log)} @ PKR {float(log.cost_price or 0):,.2f}"
        if log.company:
            desc = f"{log.company} {desc}"
        return desc

    def _match_auto_payment(log, amount):
        for pay in payments:
            if pay.id in used_payment_ids:
                continue
            if getattr(pay, 'purchase_log_id', None) == log.id:
                return pay
        item = (log.item_name or '').strip()
        candidates = []
        for pay in payments:
            if pay.id in used_payment_ids:
                continue
            note = (pay.note or '').strip()
            if not note.startswith(AUTO_PAY_NOTE):
                continue
            if abs(float(pay.amount_paid or 0) - amount) > 0.02:
                continue
            candidates.append(pay)
        if not candidates:
            return None
        if item:
            named = [p for p in candidates if item.lower() in (p.note or '').lower()]
            if named:
                return named[0]
        return candidates[0]

    def _pay_meta(pay):
        return {
            'payment_id': pay.id,
            'amount': float(pay.amount_paid or 0),
            'method': pay.method or 'Cash',
            'note': pay.note or '',
            'entry_date': (pay.payment_date.date().isoformat()
                           if pay.payment_date and hasattr(pay.payment_date, 'date')
                           else (pay.payment_date.isoformat() if pay.payment_date else '')),
            'can_edit': True,
        }

    for log in purchases:
        amount = purchase_log_total(log)
        desc = _purchase_desc(log)
        status = purchase_log_payment_status(log)
        auto_pay = _match_auto_payment(log, amount) if amount > 0 else None
        # Paid at purchase time = ONE statement line (never unpaid row + separate auto-pay)
        if status == 'paid' or auto_pay:
            if auto_pay:
                used_payment_ids.add(auto_pay.id)
            entry = {
                'date': (auto_pay.payment_date if auto_pay else None) or log.entry_date or datetime.min,
                'type': 'purchase',
                'desc': desc,
                'debit': amount,
                'credit': amount,
                'ref_id': f"Purchase #{log.id}",
                'pay_type': 'Cash',
                'payment_status': 'paid',
                'purchase_log_id': log.id,
            }
            if auto_pay:
                entry.update(_pay_meta(auto_pay))
            ledger_entries.append(entry)
        else:
            ledger_entries.append({
                'date': log.entry_date or datetime.min,
                'type': 'purchase',
                'desc': desc,
                'debit': amount,
                'credit': 0.0,
                'ref_id': f"Purchase #{log.id}",
                'pay_type': log.category,
                'payment_status': 'unpaid',
                'purchase_log_id': log.id,
            })

    for pay in payments:
        if pay.id in used_payment_ids:
            continue
        # Never list inventory auto-settle as its own row (would look like unpaid then paid)
        if getattr(pay, 'purchase_log_id', None):
            continue
        note = (pay.note or '').strip()
        if note.startswith(AUTO_PAY_NOTE):
            continue
        entry = {
            'date': pay.payment_date or datetime.min,
            'type': 'payment',
            'desc': f"Paid via {pay.method}{f' ({pay.note})' if pay.note else ''}",
            'debit': 0.0,
            'credit': float(pay.amount_paid or 0),
            'ref_id': f"Payment #{pay.id}",
            'pay_type': pay.method,
            'payment_status': 'paid',
        }
        entry.update(_pay_meta(pay))
        ledger_entries.append(entry)

    ledger_entries.sort(key=lambda x: (x['date'] or datetime.min, x.get('ref_id') or ''))

    running_balance = 0.0
    for entry in ledger_entries:
        running_balance += entry['debit'] - entry['credit']
        entry['running_balance'] = running_balance

    ledger_entries.reverse()

    total_purchased = sum(purchase_log_total(log) for log in purchases)
    if vendor.previous_payable:
        total_purchased += float(vendor.previous_payable)
    total_paid = sum(float(p.amount_paid or 0) for p in payments)

    entry_filter = (request.args.get('filter') or 'all').strip().lower()
    if entry_filter not in {'all', 'purchase', 'payment'}:
        entry_filter = 'all'
    search_q = (request.args.get('search') or '').strip().lower()

    visible_entries = ledger_entries
    if entry_filter != 'all':
        visible_entries = [
            e for e in visible_entries
            if (e.get('type') or '').lower() == entry_filter
        ]
    if search_q:
        visible_entries = [
            e for e in visible_entries
            if search_q in (e.get('desc') or '').lower()
            or search_q in (e.get('ref_id') or '').lower()
            or search_q in (e.get('pay_type') or '').lower()
        ]

    from app.charts_data import vendor_ledger_pie

    return render_template(
        'vendors/ledger.html',
        vendor=vendor,
        ledger_entries=visible_entries,
        total_purchased=total_purchased,
        total_paid=total_paid,
        entry_filter=entry_filter,
        search=search_q,
        today=datetime.utcnow().date().isoformat(),
        chart_series=vendor_ledger_pie(
            total_purchased,
            total_paid,
            vendor.current_balance_payable,
        ),
    )


@vendors_bp.route('/api/sync-balances', methods=['POST'])
@login_required
def sync_balances():
    """Admin utility: rebuild all vendor balances from purchase logs and payments."""
    if current_user.role != 'admin':
        flash('Only administrators can sync vendor balances.', 'danger')
        return redirect(url_for('vendors.index'))

    count = 0
    for vendor in Vendor.query.all():
        recalculate_vendor_balance(vendor)
        count += 1
    db.session.commit()
    flash(f'Recalculated balances for {count} vendor(s).', 'success')
    return redirect(url_for('vendors.index'))
