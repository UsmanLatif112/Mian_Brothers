"""Customer balance helpers — rebuild due from ledger sources of truth."""

from datetime import datetime

from app.models import db, Customer, CreditSale, Payment, Sale
from app.tenancy import apply_agency_filter, stamp_agency
from app.utils import business_today, parse_form_date


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


def _credit_row_still_owed(cs):
    """Unpaid receivable left on a CreditSale row (opening / loan / sale)."""
    et = (cs.entry_type or 'sale').lower()
    if et == 'advance':
        return 0.0
    amt = float(cs.amount or 0)
    paid = float(cs.amount_paid or 0)
    if et in ('loan', 'opening'):
        return max(amt - paid, 0.0) if paid > 0 else amt
    status = (cs.payment_status or 'unpaid').lower()
    if status == 'paid' and paid <= 0:
        paid = amt
    return max(amt - paid, 0.0)


def _as_date(value):
    if value is None:
        return None
    if hasattr(value, 'date') and callable(value.date):
        try:
            return value.date()
        except Exception:
            pass
    if isinstance(value, datetime):
        return value.date()
    return value


def build_recovery_list(min_amount=None):
    """
    Recovery / collect list for print.

    - min_amount is None / empty → every customer (none missing).
    - otherwise → customers whose receivable (incl. opening) is strictly greater
      than min_amount.

    Each row: name, address, phone, amount due, overdue/pending days from the
    oldest still-unpaid credit (opening, loan, sale, legacy).
    """
    today = business_today()
    query = apply_agency_filter(Customer.query, Customer)

    try:
        threshold = float(min_amount) if min_amount not in (None, '') else None
    except (TypeError, ValueError):
        threshold = None

    if threshold is not None:
        query = query.filter(Customer.current_balance_due > threshold)

    customers = query.order_by(
        Customer.current_balance_due.desc(),
        Customer.name.asc(),
    ).all()
    if not customers:
        return [], threshold

    ids = [c.id for c in customers]
    oldest_by_customer = {}

    credit_rows = (
        CreditSale.query
        .filter(
            CreditSale.customer_id.in_(ids),
            CreditSale.entry_type.in_(('sale', 'loan', 'opening')),
        )
        .all()
    )
    for cs in credit_rows:
        if _credit_row_still_owed(cs) <= 0:
            continue
        d = _as_date(cs.sale_date)
        if d is None:
            continue
        prev = oldest_by_customer.get(cs.customer_id)
        if prev is None or d < prev:
            oldest_by_customer[cs.customer_id] = d

    legacy_rows = (
        Sale.query
        .filter(
            Sale.customer_id.in_(ids),
            Sale.payment_type == 'credit',
        )
        .all()
    )
    for sale in legacy_rows:
        if float(sale.total_amount or 0) <= 0:
            continue
        d = _as_date(sale.sale_date)
        if d is None:
            continue
        prev = oldest_by_customer.get(sale.customer_id)
        if prev is None or d < prev:
            oldest_by_customer[sale.customer_id] = d

    rows = []
    for customer in customers:
        due = float(customer.current_balance_due or 0)
        oldest = oldest_by_customer.get(customer.id)
        if due > 0 and oldest is not None:
            days_overdue = max((today - oldest).days, 0)
        else:
            days_overdue = 0
        rows.append({
            'id': customer.id,
            'name': customer.name or '',
            'phone': customer.phone or '',
            'address': customer.address or '',
            'amount': due,
            'oldest_date': oldest,
            'days_overdue': days_overdue,
        })
    return rows, threshold
