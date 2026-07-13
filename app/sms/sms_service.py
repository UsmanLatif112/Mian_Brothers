import re
import requests
from flask import current_app
from app.models import db, SMSTemplate, SMSLog

def render_template_text(template_text, context):
    """
    Replaces placeholders like {{placeholder_name}} with values from context dict.
    """
    rendered = template_text
    for key, val in context.items():
        placeholder = f"{{{{{key}}}}}"  # Matches {{key}}
        rendered = rendered.replace(placeholder, str(val))
    # Replace any leftover placeholders with empty string or keep them
    rendered = re.sub(r'\{\{[a-zA-Z0-9_]+\}\}', '', rendered)
    return rendered

def send_sms(customer, message_type, context):
    """
    Renders the template, logs the message to the DB,
    and attempts to send via Twilio if configuration is present (otherwise simulates/logs to console).
    
    :param customer: Customer object (or None for general/non-customer messages)
    :param message_type: 'receipt', 'due_reminder', 'offer', 'price_update'
    :param context: dict containing keys for replacement in template (e.g. name, liters, price, amount, due)
    """
    # Fetch template
    template = SMSTemplate.query.filter_by(type=message_type).first()
    if not template:
        # Default fallback messages if template doesn't exist
        defaults = {
            'receipt': "Dear {{name}}, thank you for purchasing {{liters}}L of {{fuel}} for PKR {{amount}}. Your outstanding balance is PKR {{due}}.",
            'due_reminder': "Dear {{name}}, this is a friendly reminder that a payment of PKR {{due}} is outstanding on your account. Please settle soon.",
            'offer': "Special offer for our valued customer {{name}}! Visit us today for premium quality fuel.",
            'price_update': "Fuel price update: {{fuel}} is now PKR {{price}}/L effective {{date}}."
        }
        template_text = defaults.get(message_type, "Message from Fuel Station Management.")
    else:
        template_text = template.template_text
        
    # Render body
    rendered_body = render_template_text(template_text, context)
    phone_number = customer.phone if customer else None
    
    # Try sending via Twilio if config is set
    sid = current_app.config.get('TWILIO_ACCOUNT_SID')
    token = current_app.config.get('TWILIO_AUTH_TOKEN')
    from_number = current_app.config.get('TWILIO_PHONE_NUMBER')
    
    sent_status = 'sent'
    if sid and token and from_number and phone_number:
        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
            data = {
                'To': phone_number,
                'From': from_number,
                'Body': rendered_body
            }
            response = requests.post(url, data=data, auth=(sid, token), timeout=5)
            if response.status_code != 201:
                sent_status = 'failed'
                print(f"[SMS API Error] Twilio response: {response.text}")
        except Exception as e:
            sent_status = 'failed'
            print(f"[SMS API Exception] Failed to send: {e}")
    else:
        # Simulation
        print("\n" + "="*50)
        print(f"--- [SIMULATED SMS SENT] ---")
        print(f"To: {customer.name if customer else 'General'} ({phone_number if phone_number else 'No Phone'})")
        print(f"Type: {message_type}")
        print(f"Message: {rendered_body}")
        print("="*50 + "\n")
        sent_status = 'sent' # Marked as sent in simulation
        
    # Log in DB
    log_entry = SMSLog(
        customer_id=customer.id if customer else None,
        message_type=message_type,
        message_body=rendered_body,
        status=sent_status
    )
    db.session.add(log_entry)
    db.session.commit()
    return log_entry
