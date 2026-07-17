"""Shared edit/delete for CreditSale, Payment, and Expense with stock + balance side effects."""

from app.models import db, CreditSale, Payment, Expense, Customer, OtherItem
from app.utils import parse_form_date, datetime_from_date


class EntryError(ValueError):
    """Validation / business-rule failure for entry edits."""


def _recalc_balance(customer):
    # Lazy import avoids circular import: customers.routes ↔ entries via customers package init
    from app.customers.service import recalculate_customer_balance
    return recalculate_customer_balance(customer)


def _payment_status(amount, amount_paid):
    if amount_paid <= 0:
        return 'unpaid'
    if amount_paid >= amount:
        return 'paid'
    return 'partial'


def _is_ft_item(item):
    return item is not None and (item.category or '') == 'ft_mobile'


def restore_sale_stock(cs):
    """Put back OtherItem stock consumed by a sale CreditSale."""
    if (cs.entry_type or 'sale').lower() != 'sale' or not cs.other_item_id:
        return
    item = OtherItem.query.get(cs.other_item_id)
    if not item:
        return
    qty = float(cs.liters or 0)
    if qty <= 0:
        return
    if _is_ft_item(item):
        item.liters = float(item.liters or 0) + qty
    else:
        item.quantity = int(item.quantity or 0) + int(round(qty))


def apply_sale_stock_delta(item, old_qty, new_qty):
    """Adjust stock when sale quantity changes (new - old consumed)."""
    if not item:
        return
    delta = float(new_qty) - float(old_qty)
    if abs(delta) < 1e-9:
        return
    if _is_ft_item(item):
        available = float(item.liters or 0)
        # Positive delta = more sold → need more stock out
        if delta > 0 and available < delta:
            raise EntryError(
                f'Not enough stock. Available {available:.2f} L, need {delta:.2f} L more.'
            )
        item.liters = available - delta
    else:
        available = int(item.quantity or 0)
        units = int(round(delta))
        if units > 0 and available < units:
            raise EntryError(
                f'Not enough stock. Available {available}, need {units} more.'
            )
        item.quantity = available - units


def delete_credit_sale(cs):
    """Delete a CreditSale, restore stock if needed, recalc customer balance."""
    customer = Customer.query.get(cs.customer_id) if cs.customer_id else None
    restore_sale_stock(cs)
    db.session.delete(cs)
    db.session.flush()
    if customer:
        _recalc_balance(customer)


def delete_payment(payment):
    """Delete a Payment and recalc customer balance."""
    customer = Customer.query.get(payment.customer_id)
    db.session.delete(payment)
    db.session.flush()
    if customer:
        _recalc_balance(customer)


def delete_expense(expense):
    """Delete an Expense (settle fields go with the row)."""
    db.session.delete(expense)


def edit_credit_sale(cs, form):
    """
    Update a CreditSale from form data.
    form: werkzeug MultiDict / request.form-like.
    """
    et = (cs.entry_type or 'sale').lower()
    entry_date = parse_form_date(form.get('entry_date') or form.get('sale_date'), cs.sale_date)
    note = (form.get('remarks') or form.get('note') or '').strip() or None

    if et in ('advance', 'loan', 'opening'):
        try:
            amt = float(form.get('amount'))
            if amt <= 0:
                raise ValueError('Amount must be greater than zero.')
        except (TypeError, ValueError) as e:
            raise EntryError(f'Invalid amount: {e}') from e

        cs.sale_date = entry_date
        cs.amount = amt
        cs.remarks = note
        if et == 'advance':
            cs.amount_paid = amt
            cs.payment_status = 'paid'
        elif et == 'loan':
            cs.amount_paid = 0
            cs.payment_status = 'unpaid'
        else:  # opening
            cs.amount_paid = 0
            cs.payment_status = 'unpaid'
            if cs.customer_id:
                customer = Customer.query.get(cs.customer_id)
                if customer:
                    customer.previous_credit = amt

        db.session.flush()
        if cs.customer_id:
            customer = Customer.query.get(cs.customer_id)
            if customer:
                _recalc_balance(customer)
        return cs

    # --- sale ---
    try:
        qty = float(form.get('liters') or form.get('qty') or cs.liters or 0)
        if qty < 0:
            raise ValueError('Quantity cannot be negative.')
    except (TypeError, ValueError) as e:
        raise EntryError(f'Invalid quantity: {e}') from e

    try:
        rate = float(form.get('rate') if form.get('rate') not in (None, '') else cs.rate or 0)
        if rate < 0:
            raise ValueError('Rate cannot be negative.')
    except (TypeError, ValueError) as e:
        raise EntryError(f'Invalid rate: {e}') from e

    try:
        discount = float(form.get('discount') or cs.discount or 0)
        if discount < 0:
            raise ValueError('Discount cannot be negative.')
    except (TypeError, ValueError) as e:
        raise EntryError(f'Invalid discount: {e}') from e

    gross = qty * rate
    if discount > gross:
        raise EntryError(f'Discount cannot exceed sale total PKR {gross:,.2f}.')
    amount = max(gross - discount, 0.0)

    payment_status = (form.get('payment_status') or cs.payment_status or 'paid').strip().lower()
    if payment_status == 'paid':
        amount_paid = amount
    elif payment_status == 'partial':
        try:
            amount_paid = float(form.get('amount_paid') or 0)
        except (TypeError, ValueError) as e:
            raise EntryError(f'Invalid amount paid: {e}') from e
        if amount_paid <= 0 or amount_paid >= amount:
            raise EntryError('Partial payment must be greater than 0 and less than total amount.')
    else:
        amount_paid = 0.0
        payment_status = 'unpaid'

    payment_status = _payment_status(amount, amount_paid)
    credit_amt = max(amount - amount_paid, 0.0)

    customer_id_raw = (form.get('customer_id') or '').strip()
    if customer_id_raw:
        try:
            new_customer_id = int(customer_id_raw)
        except (TypeError, ValueError) as e:
            raise EntryError('Invalid customer.') from e
    else:
        new_customer_id = cs.customer_id

    if credit_amt > 0 and not new_customer_id:
        raise EntryError('Customer is required when any amount is on credit.')

    customer = Customer.query.get(new_customer_id) if new_customer_id else None
    if credit_amt > 0 and customer and customer.credit_limit is not None:
        # Approximate check: rebuild would be exact after save; use provisional
        other = float(customer.current_balance_due or 0) - float(cs.credit_amount or 0) + credit_amt
        if other > float(customer.credit_limit):
            raise EntryError(
                f"Exceeds credit limit of PKR {float(customer.credit_limit):,.2f}."
            )

    old_qty = float(cs.liters or 0)
    item = OtherItem.query.get(cs.other_item_id) if cs.other_item_id else None
    if item:
        apply_sale_stock_delta(item, old_qty, qty)

    old_customer_id = cs.customer_id
    cs.sale_date = entry_date
    cs.liters = qty
    cs.rate = rate
    cs.discount = discount
    cs.amount = amount
    cs.amount_paid = amount_paid
    cs.payment_status = payment_status
    cs.remarks = note
    cs.customer_id = new_customer_id

    db.session.flush()

    # Recalc both old and new customer if changed
    ids = {cid for cid in (old_customer_id, new_customer_id) if cid}
    for cid in ids:
        c = Customer.query.get(cid)
        if c:
            _recalc_balance(c)
    return cs


def edit_payment(payment, form):
    """Update a Payment and recalc customer balance."""
    try:
        amt = float(form.get('amount') or form.get('amount_paid'))
        if amt <= 0:
            raise ValueError('Amount must be greater than zero.')
    except (TypeError, ValueError) as e:
        raise EntryError(f'Invalid amount: {e}') from e

    entry_date = parse_form_date(form.get('entry_date') or form.get('payment_date'))
    method = (form.get('method') or payment.method or 'Cash').strip() or 'Cash'
    note = (form.get('note') or '').strip() or None

    payment.amount_paid = amt
    payment.payment_date = datetime_from_date(entry_date)
    payment.method = method
    payment.note = note

    db.session.flush()
    customer = Customer.query.get(payment.customer_id)
    if customer:
        _recalc_balance(customer)
    return payment


def edit_expense(expense, form, current_user_id=None):
    """Update an Expense (including settle fields when settled)."""
    name = (form.get('name') or '').strip()
    if not name:
        raise EntryError('Expense name is required.')

    try:
        amount = float(form.get('amount'))
        if amount <= 0:
            raise ValueError('Amount must be greater than zero.')
    except (TypeError, ValueError) as e:
        raise EntryError(f'Invalid amount: {e}') from e

    description = (form.get('description') or '').strip() or None
    expense_date = parse_form_date(form.get('expense_date'), expense.expense_date)

    expense.name = name
    expense.description = description
    expense.amount = amount
    expense.expense_date = expense_date

    if expense.is_settled:
        settle_raw = (form.get('settled_date') or '').strip()
        if settle_raw:
            settled_date = parse_form_date(settle_raw, expense.settled_date)
            expense.settled_date = settled_date
        settle_note = (form.get('settle_note') or '').strip() or None
        if 'settle_note' in form:
            expense.settle_note = settle_note
        if current_user_id and not expense.settled_by:
            expense.settled_by = current_user_id

    return expense
