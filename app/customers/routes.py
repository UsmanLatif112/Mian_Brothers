from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.customers import customers_bp
from app.models import db, Customer, Sale, Payment, CreditSale
from datetime import datetime

@customers_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'create':
            name = request.form.get('name')
            phone = request.form.get('phone')
            address = request.form.get('address')
            limit = request.form.get('credit_limit')
            
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
                    
            customer = Customer(
                name=name,
                phone=phone,
                address=address,
                credit_limit=limit_val,
                current_balance_due=0.00
            )
            db.session.add(customer)
            db.session.commit()
            
            flash(f"Customer '{name}' registered successfully.", 'success')
            
        elif action == 'edit':
            customer_id = request.form.get('customer_id')
            customer = Customer.query.get(customer_id)
            if customer:
                customer.name = request.form.get('name')
                customer.phone = request.form.get('phone')
                customer.address = request.form.get('address')
                
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
        query = query.filter(Customer.name.like(f"%{search_query}%") | Customer.phone.like(f"%{search_query}%"))
        
    if status_filter == 'due':
        query = query.filter(Customer.current_balance_due > 0)
    elif status_filter == 'clear':
        query = query.filter(Customer.current_balance_due <= 0)
        
    customers = query.order_by(Customer.name.asc()).all()
    
    return render_template('customers/index.html', 
                           customers=customers, 
                           search=search_query, 
                           filter=status_filter)

@customers_bp.route('/ledger/<int:customer_id>', methods=['GET', 'POST'])
@login_required
def ledger(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    
    if request.method == 'POST':
        # Log payment clearance
        amount = request.form.get('amount_paid')
        method = request.form.get('method', 'Cash')
        note = request.form.get('note')
        
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
            
        # 1. Log Payment
        payment = Payment(
            customer_id=customer.id,
            amount_paid=amt_val,
            payment_date=datetime.utcnow(),
            method=method,
            note=note
        )
        db.session.add(payment)
        
        # 2. Update outstanding balance
        customer.current_balance_due = float(customer.current_balance_due) - amt_val
        db.session.commit()
        
        flash(f"Recorded payment of PKR {amt_val:,.2f} from {customer.name}. New Balance Due: PKR {float(customer.current_balance_due):,.2f}.", 'success')
        return redirect(url_for('customers.ledger', customer_id=customer.id))
        
    # GET request — credit sales are the customer receivable source of truth
    purchases = CreditSale.query.filter_by(customer_id=customer.id).all()
    legacy_sales = Sale.query.filter_by(customer_id=customer.id).all()
    payments = Payment.query.filter_by(customer_id=customer.id).all()
    
    ledger_entries = []
    
    for p in purchases:
        unit = 'L' if p.is_fuel else 'pcs'
        ledger_entries.append({
            'date': datetime.combine(p.sale_date, datetime.min.time()) if p.sale_date else p.created_at,
            'type': 'purchase',
            'desc': f"{float(p.liters):.2f} {unit} of {p.item_name} @ PKR {float(p.rate):,.2f}",
            'debit': float(p.amount) if p.payment_status == 'unpaid' else 0.0,
            'credit': 0.0,
            'ref_id': f"Credit #{p.id}",
            'pay_type': p.payment_status
        })

    for p in legacy_sales:
        ledger_entries.append({
            'date': p.sale_date,
            'type': 'purchase',
            'desc': f"{p.liters:.2f}L of {p.fuel_type.name} @ PKR {float(p.price_per_liter):,.2f}/L (legacy)",
            'debit': float(p.total_amount) if p.payment_type == 'credit' else 0.0,
            'credit': 0.0,
            'ref_id': f"Sale #{p.id}",
            'pay_type': p.payment_type
        })
        
    for pay in payments:
        ledger_entries.append({
            'date': pay.payment_date,
            'type': 'payment',
            'desc': f"Cleared credit via {pay.method} {f'({pay.note})' if pay.note else ''}",
            'debit': 0.0,
            'credit': float(pay.amount_paid),
            'ref_id': f"Payment #{pay.id}",
            'pay_type': ''
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
                           ledger_entries=ledger_entries)
