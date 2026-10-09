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
    return 'paid' if amount_paid >= amount else 'unpaid'


def _is_ft_item(item):
    return item is not None and (item.category or '') == 'ft_mobile'


def restore_sale_stock(cs):
    """Put back shop/FT stock consumed by a sale CreditSale."""
    if (cs.entry_type or 'sale').lower() != 'sale':
        return
    qty = float(cs.liters or 0)
    if qty <= 0:
        return

    if not cs.other_item_id:
        # Fuel credit sales are ledger-only; tank stock comes from meter sales.
        return

    item = OtherItem.query.get(cs.other_item_id)
    if not item:
        return
    if _is_ft_item(item):
        item.liters = float(item.liters or 0) + qty
    else:
        item.quantity = int(item.quantity or 0) + int(round(qty))


def apply_sale_stock_delta(item, old_qty, new_qty):
    """Adjust shop/FT stock when sale quantity changes (new - old consumed).

    Fuel tank stock is owned by meter sales — credit fuel ledger edits do not touch it.
    """
    delta = float(new_qty) - float(old_qty)
    if abs(delta) < 1e-9 or not item:
        return

    if _is_ft_item(item):
        available = float(item.liters or 0)
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
    """Delete a CreditSale, restore stock, sync previous_credit, recalc balance.

    Also removes same-day overpayment Payment / Advance rows created from this sale.
    """
    customer = Customer.query.get(cs.customer_id) if cs.customer_id else None
    et = (cs.entry_type or 'sale').lower()
    restore_sale_stock(cs)

    # Opening row owns customer.previous_credit — clear when deleted
    if et == 'opening' and customer:
        customer.previous_credit = None
    elif et == 'advance' and customer and (cs.remarks or '') == 'Previous / opening advance':
        customer.previous_credit = None

    # Cascade: overpayment Payment / Advance created with this sale (same day + item label)
    if et == 'sale' and customer and cs.sale_date:
        item_label = ''
        try:
            item_label = (cs.item_name or '').strip()
        except Exception:
            item_label = ''
        overpay_hint = f'Overpayment from sale ({item_label})' if item_label else 'Overpayment from sale'
        Payment.query.filter(
            Payment.customer_id == customer.id,
            db.func.date(Payment.payment_date) == cs.sale_date,
            Payment.note.isnot(None),
            Payment.note.ilike(f'%{overpay_hint}%'),
        ).delete(synchronize_session=False)
        CreditSale.query.filter(
            CreditSale.customer_id == customer.id,
            CreditSale.id != cs.id,
            CreditSale.sale_date == cs.sale_date,
            CreditSale.entry_type == 'advance',
            CreditSale.remarks.isnot(None),
            CreditSale.remarks.ilike(f'%{overpay_hint}%'),
        ).delete(synchronize_session=False)

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
                    # Keep customer.previous_credit in sync with opening row
                    if (cs.remarks or '') == 'Previous / opening advance' or float(cs.amount or 0) < 0:
                        customer.previous_credit = -abs(amt)
                    else:
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

    gross = round(qty * rate, 2)
    amount_raw = (form.get('amount') or '').strip()
    try:
        if amount_raw:
            amount = round(float(amount_raw), 2)
            if amount <= 0:
                raise ValueError('Total amount must be greater than zero.')
            discount = round(max(gross - amount, 0.0), 2)
        else:
            discount = float(form.get('discount') or cs.discount or 0)
            if discount < 0:
                raise ValueError('Discount cannot be negative.')
            if discount > gross:
                raise ValueError(f'Discount cannot exceed sale total PKR {gross:,.2f}.')
            amount = round(max(gross - discount, 0.0), 2)
    except (TypeError, ValueError) as e:
        raise EntryError(f'Invalid total/discount: {e}') from e

    payment_status = (form.get('payment_status') or cs.payment_status or 'paid').strip().lower()
    if payment_status == 'paid':
        amount_paid = amount
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
