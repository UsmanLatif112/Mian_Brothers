from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.customers import customers_bp
from app.models import db, Customer, Sale, Payment, CreditSale
from app.utils import paginate, parse_form_date, datetime_from_date
from app.services.entries import (
    EntryError, edit_credit_sale, delete_credit_sale, edit_payment, delete_payment,
)
from app.customers.service import recalculate_customer_balance
from datetime import datetime

PER_PAGE = 15


@customers_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'create':
            name = request.form.get('name')
            phone = request.form.get('phone')
            address = request.form.get('address')
            old_book_no = (request.form.get('old_book_no') or '').strip() or None
            limit = request.form.get('credit_limit')
            prev_raw = (request.form.get('previous_credit') or '').strip()
            entry_date = parse_form_date(request.form.get('entry_date'))
            
            if not name:
                flash('Customer name is required.', 'danger')
                return redirect(url_for('customers.index'))
                
            limit_val = None
            if limit:
                try:
                    limit_val = float(limit)
                    if limit_val <= 0:
                        raise ValueError("Limit must be greater than zero.")
                except ValueError as e:
                    flash(f"Invalid credit limit: {e}", 'danger')
                    return redirect(url_for('customers.index'))

            prev_credit = 0.0
            if prev_raw:
                try:
                    prev_credit = float(prev_raw)
                    if prev_credit < 0:
                        raise ValueError('Previous credit cannot be negative.')
                except ValueError as e:
                    flash(f'Invalid previous credit: {e}', 'danger')
                    return redirect(url_for('customers.index'))
                    
            customer = Customer(
                name=name,
                phone=phone,
                address=address,
                old_book_no=old_book_no,
                previous_credit=prev_credit if prev_credit > 0 else None,
                credit_limit=limit_val,
                current_balance_due=prev_credit,
            )
            db.session.add(customer)
            db.session.flush()

            # Opening book credit affects customer balance only — excluded from period cash KPIs.
            if prev_credit > 0:
                db.session.add(CreditSale(
                    customer_id=customer.id,
                    sale_date=entry_date,
                    liters=0,
                    rate=0,
                    amount=prev_credit,
                    amount_paid=0,
                    entry_type='opening',
                    payment_status='unpaid',
                    remarks='Previous / opening book credit',
                    recorded_by=current_user.id,
                ))

            db.session.commit()
            flash(f"Customer '{name}' registered successfully.", 'success')
            
        elif action == 'edit':
            customer_id = request.form.get('customer_id')
            customer = Customer.query.get(customer_id)
            if customer:
                customer.name = request.form.get('name')
                customer.phone = request.form.get('phone')
                customer.address = request.form.get('address')
                customer.old_book_no = (request.form.get('old_book_no') or '').strip() or None
                
                limit = request.form.get('credit_limit')
                limit_val = None
                if limit:
                    try:
                        limit_val = float(limit)
                    except ValueError:
                        pass
                customer.credit_limit = limit_val
                
                db.session.commit()
                flash(f"Customer details updated for '{customer.name}'.", 'success')
                
        return redirect(url_for('customers.index'))
        
    # GET request
    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('filter', 'all') # 'all', 'due', 'clear'
    
    query = Customer.query
    
    if search_query:
        query = query.filter(
            Customer.name.like(f"%{search_query}%")
            | Customer.phone.like(f"%{search_query}%")
            | Customer.old_book_no.like(f"%{search_query}%")
        )
        
    if status_filter == 'due':
        query = query.filter(Customer.current_balance_due > 0)
    elif status_filter == 'clear':
        query = query.filter(Customer.current_balance_due <= 0)

    customers, customers_pagination = paginate(
        query.order_by(Customer.name.asc()),
        request.args.get('page', 1),
        PER_PAGE,
    )

    return render_template(
        'customers/index.html',
        customers=customers,
        customers_pagination=customers_pagination,
        search=search_query,
        filter=status_filter,
        today=datetime.utcnow().date().isoformat(),
    )

@customers_bp.route('/ledger/<int:customer_id>', methods=['GET', 'POST'])
@login_required
def ledger(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    
    if request.method == 'POST':
        action = (request.form.get('action') or 'payment').strip().lower()

        # ---------- Edit / delete ledger rows ----------
        if action == 'edit_entry':
            source_type = (request.form.get('source_type') or '').strip().lower()
            source_id = request.form.get('source_id')
            try:
                if source_type == 'payment':
                    payment = Payment.query.filter_by(id=source_id, customer_id=customer.id).first()
                    if not payment:
                        raise EntryError('Payment not found.')
                    edit_payment(payment, request.form)
                elif source_type == 'credit_sale':
                    cs = CreditSale.query.filter_by(id=source_id, customer_id=customer.id).first()
                    if not cs:
                        raise EntryError('Entry not found.')
                    edit_credit_sale(cs, request.form)
                else:
                    raise EntryError('Unknown entry type.')
                db.session.commit()
                flash('Ledger entry updated. Balance recalculated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return redirect(url_for('customers.ledger', customer_id=customer.id))

        if action == 'delete_entry':
            source_type = (request.form.get('source_type') or '').strip().lower()
            source_id = request.form.get('source_id')
            try:
                if source_type == 'payment':
                    payment = Payment.query.filter_by(id=source_id, customer_id=customer.id).first()
                    if not payment:
                        raise EntryError('Payment not found.')
                    delete_payment(payment)
                elif source_type == 'credit_sale':
                    cs = CreditSale.query.filter_by(id=source_id, customer_id=customer.id).first()
                    if not cs:
                        raise EntryError('Entry not found.')
                    delete_credit_sale(cs)
                elif source_type == 'legacy_sale':
                    raise EntryError('Legacy fuel sales cannot be deleted here.')
                else:
                    raise EntryError('Unknown entry type.')
                db.session.commit()
                flash('Ledger entry deleted. Balance recalculated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return redirect(url_for('customers.ledger', customer_id=customer.id))

        # ---------- Advance / Loan ----------
        if action == 'advance_loan':
            kind = (request.form.get('entry_kind') or '').strip().lower()
            amount = request.form.get('amount')
            note = (request.form.get('note') or '').strip() or None
            entry_date = parse_form_date(request.form.get('entry_date'))

            if kind not in ('advance', 'loan'):
                flash('Please select Advance or Loan.', 'danger')
                return redirect(url_for('customers.ledger', customer_id=customer.id))

            try:
                amt_val = float(amount)
                if amt_val <= 0:
                    raise ValueError('Amount must be greater than zero.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid amount: {e}', 'danger')
                return redirect(url_for('customers.ledger', customer_id=customer.id))

            if kind == 'advance':
                db.session.add(CreditSale(
                    customer_id=customer.id,
                    sale_date=entry_date,
                    liters=0,
                    rate=0,
                    amount=amt_val,
                    amount_paid=amt_val,
                    entry_type='advance',
                    payment_status='paid',
                    remarks=note,
                    recorded_by=current_user.id,
                ))
                db.session.flush()
                recalculate_customer_balance(customer)
                db.session.commit()
                flash(
                    f"Advance of PKR {amt_val:,.2f} saved for {customer.name}. "
                    f"Balance: PKR {float(customer.current_balance_due):,.2f}"
                    f"{' (advance / prepaid)' if float(customer.current_balance_due) < 0 else ''}.",
                    'success'
                )
            else:
                db.session.add(CreditSale(
                    customer_id=customer.id,
                    sale_date=entry_date,
                    liters=0,
                    rate=0,
                    amount=amt_val,
                    amount_paid=0,
                    entry_type='loan',
                    payment_status='unpaid',
                    remarks=note,
                    recorded_by=current_user.id,
                ))
                db.session.flush()
                recalculate_customer_balance(customer)
                db.session.commit()
                flash(
                    f"Loan of PKR {amt_val:,.2f} saved for {customer.name}. "
                    f"Balance due: PKR {float(customer.current_balance_due):,.2f}.",
                    'success'
                )
            return redirect(url_for('customers.ledger', customer_id=customer.id))

        # ---------- Payment / credit clear ----------
        if action != 'payment':
            flash('Unknown action.', 'danger')
            return redirect(url_for('customers.ledger', customer_id=customer.id))

        amount = request.form.get('amount_paid')
        method = request.form.get('method', 'Cash')
        note = request.form.get('note')
        entry_date = parse_form_date(request.form.get('entry_date'))
        
        if not amount:
            flash('Payment amount is required.', 'danger')
            return redirect(url_for('customers.ledger', customer_id=customer.id))
            
        try:
            amt_val = float(amount)
            if amt_val <= 0:
                raise ValueError("Payment amount must be greater than zero.")
        except ValueError as e:
            flash(f"Invalid payment amount: {e}", 'danger')
            return redirect(url_for('customers.ledger', customer_id=customer.id))
            
        payment = Payment(
            customer_id=customer.id,
            amount_paid=amt_val,
            payment_date=datetime_from_date(entry_date),
            method=method,
            note=note
        )
        db.session.add(payment)
        db.session.flush()
        recalculate_customer_balance(customer)
        db.session.commit()
        
        bal = float(customer.current_balance_due)
        bal_note = ' (advance / prepaid)' if bal < 0 else ''
        flash(
            f"Recorded payment of PKR {amt_val:,.2f} from {customer.name}. "
            f"Balance: PKR {bal:,.2f}{bal_note}.",
            'success'
        )
        return redirect(url_for('customers.ledger', customer_id=customer.id))
        
    # GET request — credit sales are the customer receivable source of truth
    purchases = CreditSale.query.filter_by(customer_id=customer.id).all()
    legacy_sales = Sale.query.filter_by(customer_id=customer.id).all()
    payments = Payment.query.filter_by(customer_id=customer.id).all()
    
    ledger_entries = []
    
    for p in purchases:
        unit = 'L' if p.is_fuel else 'pcs'
        et = (p.entry_type or 'sale').lower()
        sale_date_str = p.sale_date.isoformat() if p.sale_date else ''
        base = {
            'source_type': 'credit_sale',
            'source_id': p.id,
            'entry_kind': et,
            'can_edit': True,
            'sale_date': sale_date_str,
            'amount': float(p.amount or 0),
            'amount_paid': float(p.amount_paid or 0),
            'liters': float(p.liters or 0),
            'rate': float(p.rate or 0),
            'discount': float(getattr(p, 'discount', 0) or 0),
            'payment_status': p.payment_status or 'unpaid',
            'remarks': p.remarks or '',
            'method': '',
        }
        if et == 'advance':
            ledger_entries.append({
                **base,
                'date': datetime.combine(p.sale_date, datetime.min.time()) if p.sale_date else p.created_at,
                'type': 'payment',
                'desc': f"Advance / prepaid {f'({p.remarks})' if p.remarks else ''}",
                'debit': 0.0,
                'credit': float(p.amount or 0),
                'ref_id': f"Advance #{p.id}",
                'pay_type': 'advance',
            })
        elif et == 'loan':
            ledger_entries.append({
                **base,
                'date': datetime.combine(p.sale_date, datetime.min.time()) if p.sale_date else p.created_at,
                'type': 'purchase',
                'desc': f"Loan / borrow {f'({p.remarks})' if p.remarks else ''}",
                'debit': float(p.amount or 0),
                'credit': 0.0,
                'ref_id': f"Loan #{p.id}",
                'pay_type': 'loan',
            })
        elif et == 'opening':
            ledger_entries.append({
                **base,
                'date': datetime.combine(p.sale_date, datetime.min.time()) if p.sale_date else p.created_at,
                'type': 'purchase',
                'desc': f"Previous / opening credit {f'({p.remarks})' if p.remarks else ''}",
                'debit': float(p.amount or 0),
                'credit': 0.0,
                'ref_id': f"Opening #{p.id}",
                'pay_type': 'opening',
            })
        else:
            paid = float(p.amount_paid or 0)
            credit = max(float(p.amount or 0) - paid, 0.0)
            disc = float(getattr(p, 'discount', 0) or 0)
            desc = f"{float(p.liters):.2f} {unit} of {p.item_name} @ PKR {float(p.rate):,.2f}"
            if disc > 0:
                desc += f" (−{disc:,.2f} discount)"
            if paid > 0 and credit > 0:
                desc += f" (paid {paid:,.2f}, credit {credit:,.2f})"
            ledger_entries.append({
                **base,
                'date': datetime.combine(p.sale_date, datetime.min.time()) if p.sale_date else p.created_at,
                'type': 'purchase',
                'desc': desc,
                'debit': credit,
                'credit': 0.0,
                'ref_id': f"Sale #{p.id}",
                'pay_type': p.payment_status,
            })

    for p in legacy_sales:
        ledger_entries.append({
            'date': p.sale_date,
            'type': 'purchase',
            'desc': f"{p.liters:.2f}L of {p.fuel_type.name} @ PKR {float(p.price_per_liter):,.2f}/L (legacy)",
            'debit': float(p.total_amount) if p.payment_type == 'credit' else 0.0,
            'credit': 0.0,
            'ref_id': f"Legacy Sale #{p.id}",
            'pay_type': p.payment_type,
            'source_type': 'legacy_sale',
            'source_id': p.id,
            'entry_kind': 'legacy',
            'can_edit': False,
            'sale_date': '',
            'amount': float(p.total_amount or 0),
            'amount_paid': 0,
            'liters': float(p.liters or 0),
            'rate': float(p.price_per_liter or 0),
            'discount': 0,
            'payment_status': p.payment_type or '',
            'remarks': '',
            'method': '',
        })
        
    for pay in payments:
        pay_date = pay.payment_date
        sale_date_str = pay_date.date().isoformat() if hasattr(pay_date, 'date') else str(pay_date)[:10]
        ledger_entries.append({
            'date': pay.payment_date,
            'type': 'payment',
            'desc': f"Cleared credit via {pay.method} {f'({pay.note})' if pay.note else ''}",
            'debit': 0.0,
            'credit': float(pay.amount_paid),
            'ref_id': f"Payment #{pay.id}",
            'pay_type': 'payment',
            'source_type': 'payment',
            'source_id': pay.id,
            'entry_kind': 'payment',
            'can_edit': True,
            'sale_date': sale_date_str,
            'amount': float(pay.amount_paid or 0),
            'amount_paid': float(pay.amount_paid or 0),
            'liters': 0,
            'rate': 0,
            'discount': 0,
            'payment_status': 'paid',
            'remarks': pay.note or '',
            'method': pay.method or 'Cash',
        })
        
    # Sort by date ascending to calculate running balance correctly
    ledger_entries.sort(key=lambda x: x['date'] or datetime.min)
    
    # Calculate running balance
    running_balance = 0.0
    for entry in ledger_entries:
        running_balance += entry['debit'] - entry['credit']
        entry['running_balance'] = running_balance
        
    # Reverse list for displaying newest first
    ledger_entries.reverse()
    
    return render_template('customers/ledger.html', 
                           customer=customer, 
                           ledger_entries=ledger_entries,
                           today=datetime.utcnow().date().isoformat())
