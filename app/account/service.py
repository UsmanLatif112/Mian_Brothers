"""Till carry-forward helpers for Account and Sales pages."""
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func

from app.models import db, CashTaken, DailyTillBalance
from app.tenancy import apply_agency_filter, stamp_agency


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def previous_balance_for_date(day):
    """Opening previous for this day: stored value, else prior day’s remaining."""
    day = as_date(day)
    prior = (
        apply_agency_filter(
            DailyTillBalance.query.filter(DailyTillBalance.balance_date < day),
            DailyTillBalance,
        )
        .order_by(DailyTillBalance.balance_date.desc())
        .first()
    )
    prior_remaining = float(prior.remaining_balance or 0) if prior else 0.0

    current = apply_agency_filter(
        DailyTillBalance.query.filter_by(balance_date=day), DailyTillBalance
    ).first()
    if current:
        stored = float(current.previous_balance or 0)
        # Non-zero stored (or no prior day) wins; otherwise carry prior remaining
        # so an auto-created till row with previous=0 does not block carry-forward.
        if stored > 0 or prior is None:
            return stored
        return prior_remaining
    return prior_remaining


def day_account_figures(day, models_ns):
    """
    Figures for one calendar day (listing / till).
    pending = cash_in_hand − amount taken that day (next-day opening).
    """
    from app.utils import compute_period_stats

    day = as_date(day)
    stats = compute_period_stats(day, day, models_ns)
    previous = float(stats.get('previous_balance') or 0)
    cash_in_hand = float(stats.get('cash_in_hand') or 0)
    taken = float(stats.get('cash_taken_total') or 0)

    till = apply_agency_filter(
        DailyTillBalance.query.filter_by(balance_date=day), DailyTillBalance
    ).first()
    if till:
        # If live activity under-reports, rebuild from stored remaining + taken
        reconstructed = float(till.remaining_balance or 0) + taken
        if reconstructed > cash_in_hand:
            cash_in_hand = reconstructed

    pending = cash_in_hand - taken
    return {
        'previous_balance': previous,
        'cash_in_hand': cash_in_hand,
        'cash_taken_total': taken,
        'pending_balance': pending,
    }


def cash_taken_total(start, end=None):
    end = end or start
    return float(
        apply_agency_filter(
            CashTaken.query.with_entities(func.coalesce(func.sum(CashTaken.amount), 0))
            .filter(CashTaken.taken_date >= start, CashTaken.taken_date <= end),
            CashTaken,
        ).scalar() or 0
    )


def cash_taken_rows(start, end=None):
    end = end or start
    return (
        apply_agency_filter(
            CashTaken.query.filter(CashTaken.taken_date >= start, CashTaken.taken_date <= end),
            CashTaken,
        )
        .order_by(CashTaken.taken_date.desc(), CashTaken.id.desc())
        .all()
    )


def upsert_till_balance(day, cash_in_hand, user_id=None):
    """Persist remaining = computed cash in hand - cash taken for this day."""
    day = as_date(day)
    previous = previous_balance_for_date(day)
    taken = cash_taken_total(day)
    remaining = float(cash_in_hand or 0) - taken
    row = apply_agency_filter(
        DailyTillBalance.query.filter_by(balance_date=day), DailyTillBalance
    ).first()
    if not row:
        row = DailyTillBalance(balance_date=day)
        stamp_agency(row)
        db.session.add(row)
    row.previous_balance = previous
    row.remaining_balance = remaining
    if user_id:
        row.updated_by = user_id
    return row


def set_previous_balance(day, amount, user_id=None):
    """Set the opening previous balance for a day and keep remaining in sync."""
    day = as_date(day)
    row = apply_agency_filter(
        DailyTillBalance.query.filter_by(balance_date=day), DailyTillBalance
    ).first()
    if not row:
        row = DailyTillBalance(balance_date=day)
        stamp_agency(row)
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
