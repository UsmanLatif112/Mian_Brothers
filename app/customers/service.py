"""Customer balance helpers — rebuild due from ledger sources of truth."""

from app.models import db, Customer, CreditSale, Payment, Sale
from app.tenancy import stamp_agency
from app.utils import parse_form_date


def customer_option_label(customer):
    due = float(customer.current_balance_due or 0)
    label = f"{customer.name}"
    if customer.phone:
        label += f" · {customer.phone}"
    label += f" (Due: PKR {due:,.2f})"
    return label


def register_customer(data, recorded_by=None):
    """
    Create a customer from form/JSON payload (same fields as Register Customer modal).
    Returns (customer, None) or (None, error_message).
    """
    name = (data.get('name') or '').strip()
    phone = (data.get('phone') or '').strip() or None
    address = (data.get('address') or '').strip() or None
    old_book_no = (data.get('old_book_no') or '').strip() or None
    limit = data.get('credit_limit')
    prev_raw = (data.get('previous_credit') or '').strip()
    entry_date = parse_form_date(data.get('entry_date'))

    if not name:
        return None, 'Customer name is required'

    limit_val = None
    if limit not in (None, ''):
        try:
            limit_val = float(limit)
            if limit_val <= 0:
                raise ValueError('Limit must be greater than zero.')
        except ValueError as e:
            return None, f'Invalid credit limit: {e}'

    prev_credit = 0.0
    if prev_raw:
        try:
            prev_credit = float(prev_raw)
        except ValueError as e:
            return None, f'Invalid previous balance: {e}'

    customer = Customer(
        name=name,
        phone=phone,
        address=address,
        old_book_no=old_book_no,
        previous_credit=prev_credit if prev_credit != 0 else None,
        credit_limit=limit_val,
        current_balance_due=0,
    )
    stamp_agency(customer)
    db.session.add(customer)
    db.session.flush()

    if prev_credit > 0:
        db.session.add(stamp_agency(CreditSale(
            customer_id=customer.id,
            sale_date=entry_date,
            liters=0,
            rate=0,
            amount=prev_credit,
            amount_paid=0,
            entry_type='opening',
            payment_status='unpaid',
            remarks='Previous / opening book credit',
            recorded_by=recorded_by,
        )))
    elif prev_credit < 0:
        db.session.add(stamp_agency(CreditSale(
            customer_id=customer.id,
            sale_date=entry_date,
            liters=0,
            rate=0,
            amount=abs(prev_credit),
            amount_paid=0,
            entry_type='advance',
            payment_status='paid',
            remarks='Previous / opening advance',
            recorded_by=recorded_by,
        )))

    db.session.flush()
    recalculate_customer_balance(customer)
    db.session.commit()
    return customer, None


def customer_has_linked_activity(customer):
    """True if customer has sales, payments, or balance — block hard delete."""
    if CreditSale.query.filter_by(customer_id=customer.id).first():
        return True
    if Payment.query.filter_by(customer_id=customer.id).first():
        return True
    if Sale.query.filter_by(customer_id=customer.id).first():
        return True
    if float(customer.current_balance_due or 0) != 0:
        return True
    if customer.previous_credit is not None and float(customer.previous_credit or 0) != 0:
        return True
    return False


def recalculate_customer_balance(customer):
    """
    Rebuild current_balance_due from CreditSale + Payment + legacy Sale.
    Matches customer ledger running-balance semantics.
    """
    balance = 0.0

    credit_sales = (
        CreditSale.query
        .filter_by(customer_id=customer.id)
        .order_by(CreditSale.sale_date.asc(), CreditSale.id.asc())
        .all()
    )
    for cs in credit_sales:
        et = (cs.entry_type or 'sale').lower()
        amt = float(cs.amount or 0)
        if et == 'advance':
            balance -= amt
        elif et in ('loan', 'opening'):
            balance += amt
        else:
            # sale: only unpaid portion increases due
            paid = float(cs.amount_paid or 0)
            status = (cs.payment_status or 'unpaid').lower()
            if status == 'paid' and paid <= 0:
                paid = amt
            balance += max(amt - paid, 0.0)

    for pay in Payment.query.filter_by(customer_id=customer.id).all():
        balance -= float(pay.amount_paid or 0)

    for legacy in Sale.query.filter_by(customer_id=customer.id).all():
        if (legacy.payment_type or '').lower() == 'credit':
            balance += float(legacy.total_amount or 0)

    customer.current_balance_due = balance
    return balance
