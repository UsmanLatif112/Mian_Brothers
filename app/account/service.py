"""Till carry-forward helpers for Account and Sales pages."""
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func

from app.models import db, CashTaken, DailyTillBalance


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def previous_balance_for_date(day):
    """Use a manual previous balance for this day, otherwise prior remaining."""
    day = as_date(day)
    current = DailyTillBalance.query.filter_by(balance_date=day).first()
    if current:
        return float(current.previous_balance or 0)

    prior = (
        DailyTillBalance.query
        .filter(DailyTillBalance.balance_date < day)
        .order_by(DailyTillBalance.balance_date.desc())
        .first()
    )
    return float(prior.remaining_balance or 0) if prior else 0.0


def cash_taken_total(start, end=None):
    end = end or start
    return float(
        CashTaken.query.with_entities(func.coalesce(func.sum(CashTaken.amount), 0))
        .filter(CashTaken.taken_date >= start, CashTaken.taken_date <= end)
        .scalar() or 0
    )


def cash_taken_rows(start, end=None):
    end = end or start
    return (
        CashTaken.query
        .filter(CashTaken.taken_date >= start, CashTaken.taken_date <= end)
        .order_by(CashTaken.taken_date.desc(), CashTaken.id.desc())
        .all()
    )


def upsert_till_balance(day, cash_in_hand, user_id=None):
    """Persist remaining = computed cash in hand - cash taken for this day."""
    day = as_date(day)
    previous = previous_balance_for_date(day)
    taken = cash_taken_total(day)
    remaining = float(cash_in_hand or 0) - taken
    row = DailyTillBalance.query.filter_by(balance_date=day).first()
    if not row:
        row = DailyTillBalance(balance_date=day)
        db.session.add(row)
    row.previous_balance = previous
    row.remaining_balance = remaining
    if user_id:
        row.updated_by = user_id
    return row


def set_previous_balance(day, amount, user_id=None):
    """Set the opening previous balance for a day and keep remaining in sync."""
    day = as_date(day)
    row = DailyTillBalance.query.filter_by(balance_date=day).first()
    if not row:
        row = DailyTillBalance(balance_date=day)
        db.session.add(row)
    row.previous_balance = float(amount or 0)
    row.remaining_balance = float(amount or 0) - cash_taken_total(day)
    if user_id:
        row.updated_by = user_id
    return row


def cash_taken_journal_rows(stats):
    rows = []
    for item in stats.get('cash_taken_rows') or []:
        note = (item.note or '').strip()
        desc = f"Taken by {item.person_name}"
        if note:
            desc = f"{desc} — {note}"
        rows.append(SimpleNamespace(
            id=item.id,
            sale_date=item.taken_date,
            entry_type='cash_taken',
            customer=None,
            vendor=None,
            vendor_name='—',
            item_name=desc,
            liters=0,
            amount=float(item.amount or 0),
            discount=0,
            amount_paid=0,
            overpayment=0,
            credit_amount=float(item.amount or 0),
            other_amount=float(item.amount or 0),
            other_kind='taken',
            payment_status='taken',
            cash_direction='out',
            is_fuel=False,
            other_item=None,
        ))
    return rows


def previous_balance_journal_row(stats):
    previous = float(stats.get('previous_balance') or 0)
    if previous <= 0:
        return None
    return SimpleNamespace(
        id=0,
        sale_date=stats.get('start_date'),
        entry_type='prev_balance',
        customer=None,
        vendor=None,
        vendor_name='—',
        item_name='Previous balance carried forward',
        liters=0,
        amount=previous,
        discount=0,
        amount_paid=previous,
        overpayment=0,
        credit_amount=0,
        other_amount=0,
        other_kind='none',
        payment_status='carried',
        cash_direction='in',
        is_fuel=False,
        other_item=None,
    )
