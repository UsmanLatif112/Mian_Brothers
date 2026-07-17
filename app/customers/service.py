"""Customer balance helpers — rebuild due from ledger sources of truth."""

from app.models import CreditSale, Payment, Sale


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
