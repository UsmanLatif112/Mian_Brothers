from datetime import datetime

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.models import db, Vendor, VendorPayment, ItemPurchaseLog
from app.utils import paginate, parse_form_date, datetime_from_date
from app.vendors import vendors_bp
from app.vendors.service import (
    normalize_vendor_name,
    purchase_log_total,
    recalculate_vendor_balance,
    get_or_create_vendor,
)

PER_PAGE = 15


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
                db.session.commit()
                flash(f"Vendor details updated for '{vendor.name}'.", 'success')

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

    return render_template(
        'vendors/index.html',
        vendors=vendors,
        vendors_pagination=vendors_pagination,
        search=search_query,
        filter=status_filter,
        today=datetime.utcnow().date().isoformat(),
    )


@vendors_bp.route('/ledger/<int:vendor_id>', methods=['GET', 'POST'])
@login_required
def ledger(vendor_id):
    vendor = Vendor.query.get_or_404(vendor_id)

    if request.method == 'POST':
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
        vendor.current_balance_payable = float(vendor.current_balance_payable or 0) - amt_val
        db.session.commit()

        flash(
            f"Recorded payment of PKR {amt_val:,.2f} to {vendor.name}. "
            f"Balance payable: PKR {float(vendor.current_balance_payable):,.2f}.",
            'success',
        )
        return redirect(url_for('vendors.ledger', vendor_id=vendor.id))

    ledger_entries = []

    if vendor.previous_payable and float(vendor.previous_payable) > 0:
        ledger_entries.append({
            'date': vendor.created_at or datetime.min,
            'type': 'purchase',
            'desc': 'Previous / opening payable balance',
            'debit': float(vendor.previous_payable),
            'credit': 0.0,
            'ref_id': 'Opening',
            'pay_type': 'opening',
        })

    purchases = ItemPurchaseLog.query.filter_by(vendor_id=vendor.id).order_by(
        ItemPurchaseLog.entry_date.asc(), ItemPurchaseLog.id.asc()
    ).all()

    for log in purchases:
        amount = purchase_log_total(log)
        if log.category == 'fuel':
            qty_desc = f"{float(log.liters or 0):,.2f} L"
        elif log.category == 'ft_mobile':
            qty_desc = f"{float(log.liters or 0):,.2f} L"
        else:
            qty_desc = f"{int(log.quantity or 0)} pcs"

        desc = f"{log.item_name} — {qty_desc} @ PKR {float(log.cost_price or 0):,.2f}"
        if log.company:
            desc = f"{log.company} {desc}"

        ledger_entries.append({
            'date': log.entry_date or datetime.min,
            'type': 'purchase',
            'desc': desc,
            'debit': amount,
            'credit': 0.0,
            'ref_id': f"Purchase #{log.id}",
            'pay_type': log.category,
        })

    payments = VendorPayment.query.filter_by(vendor_id=vendor.id).order_by(
        VendorPayment.payment_date.asc(), VendorPayment.id.asc()
    ).all()

    for pay in payments:
        ledger_entries.append({
            'date': pay.payment_date or datetime.min,
            'type': 'payment',
            'desc': f"Paid via {pay.method}{f' ({pay.note})' if pay.note else ''}",
            'debit': 0.0,
            'credit': float(pay.amount_paid or 0),
            'ref_id': f"Payment #{pay.id}",
            'pay_type': pay.method,
        })

    ledger_entries.sort(key=lambda x: x['date'] or datetime.min)

    running_balance = 0.0
    for entry in ledger_entries:
        running_balance += entry['debit'] - entry['credit']
        entry['running_balance'] = running_balance

    ledger_entries.reverse()

    total_purchased = sum(purchase_log_total(log) for log in purchases)
    if vendor.previous_payable:
        total_purchased += float(vendor.previous_payable)
    total_paid = sum(float(p.amount_paid or 0) for p in payments)

    return render_template(
        'vendors/ledger.html',
        vendor=vendor,
        ledger_entries=ledger_entries,
        total_purchased=total_purchased,
        total_paid=total_paid,
        today=datetime.utcnow().date().isoformat(),
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
