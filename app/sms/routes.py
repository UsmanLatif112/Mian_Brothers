from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.sms import sms_bp
from app.models import db, SMSTemplate, SMSLog, Customer, FuelType, FuelPrice
from app.sms.sms_service import send_sms
from app.decorators import role_required
from datetime import datetime

@sms_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        action = request.form.get('action')
        
        # 1. Update Template (Admin only)
        if action == 'update_template':
            if current_user.role != 'admin':
                flash('Only Administrators can modify SMS templates.', 'danger')
                return redirect(url_for('sms.index'))
                
            template_id = request.form.get('template_id')
            template_text = request.form.get('template_text')
            
            if not template_id or not template_text:
                flash('Template and text are required.', 'danger')
                return redirect(url_for('sms.index'))
                
            template = SMSTemplate.query.get(template_id)
            if template:
                template.template_text = template_text
                db.session.commit()
                flash(f"SMS Template for '{template.type}' updated successfully.", 'success')
                
        # 2. Dispatch Manual Message
        elif action == 'send_manual':
            customer_id = request.form.get('customer_id')
            msg_type = request.form.get('message_type')
            
            if not customer_id or not msg_type:
                flash('Please select a customer and message type.', 'danger')
                return redirect(url_for('sms.index'))
                
            customer = Customer.query.get(customer_id)
            if not customer:
                flash('Customer not found.', 'danger')
                return redirect(url_for('sms.index'))
                
            # Create a contextual mapping based on message type
            # We'll pull the active prices for placeholders in the price_update or receipt
            fuels = FuelType.query.all()
            prices = {}
            for f in fuels:
                lp = FuelPrice.query.filter_by(fuel_type_id=f.id).order_by(FuelPrice.created_at.desc()).first()
                prices[f.name.lower()] = float(lp.price_per_liter) if lp else 0.0
                
            context = {
                'name': customer.name,
                'due': f"{float(customer.current_balance_due):.2f}",
                'amount': "0.00", # Default for manual reminder
                'liters': "0.00",
                'fuel': "N/A",
                'date': datetime.utcnow().strftime('%Y-%m-%d'),
                'petrol': f"{prices.get('petrol', 1.45):.2f}",
                'diesel': f"{prices.get('diesel', 1.32):.2f}",
                'price': "0.00"
            }
            
            # Send
            log_rec = send_sms(customer, msg_type, context)
            if log_rec.status == 'sent':
                flash(f"SMS successfully dispatched to {customer.name} ({customer.phone or 'No Phone'}). Check logs below.", 'success')
            else:
                flash(f"Failed to dispatch SMS to {customer.name}. Check configurations.", 'danger')
                
        return redirect(url_for('sms.index'))
        
    # GET request
    templates = SMSTemplate.query.all()
    customers = Customer.query.all()
    sms_logs = SMSLog.query.order_by(SMSLog.sent_at.desc()).limit(100).all()
    
    return render_template('sms/index.html',
                           templates=templates,
                           customers=customers,
                           sms_logs=sms_logs)
