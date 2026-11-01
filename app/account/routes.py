from datetime import datetime
from types import SimpleNamespace

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.account import account_bp
from app.account.service import (
    cash_taken_rows, carry_previous_for_date, day_account_figures,
    previous_balance_for_date, set_previous_balance, set_remaining_balance,
    upsert_till_balance,
)
from app.models import (
    db, CashTaken, CreditSale, Customer, DailyCashCount, DailyTillBalance, Expense,
    FuelPrice, FuelType, MeterReading, Payment,
)
from app.tenancy import apply_agency_filter, require_agency_access, stamp_agency
from app.utils import PERIOD_CHOICES, compute_period_stats, parse_form_date, parse_period


def _enrich_taken_rows(rows):
    """
    Per date: previous balance, cash in hand, pending (= cash in hand − day’s takes).
    Row pending uses cash_in_hand − that row’s amount when it’s the only take;
    with multiple takes same day, pending is after all takes (next-day balance).
    """
    cache = {}
    enriched = []
    models = _models_ns()
    for row in rows:
        day = _as_date(row.taken_date)
        if day not in cache:
            cache[day] = day_account_figures(day, models)
        ctx = cache[day]
        amount = float(row.amount or 0)
        # Prefer saved remaining (e.g. user set 5005) — that becomes next day’s previous
        pending = float(ctx.get('pending_balance') or 0)
        enriched.append(SimpleNamespace(
            id=row.id,
            taken_date=row.taken_date,
            person_name=row.person_name,
            note=row.note,
            amount=amount,
            previous_balance=ctx['previous_balance'],
            cash_in_hand=ctx['cash_in_hand'],
            pending_balance=pending,
        ))
    return enriched


def _today():
    from app.utils import business_today
    return business_today()


def _models_ns():
    return SimpleNamespace(
        MeterReading=MeterReading,
        FuelType=FuelType,
        FuelPrice=FuelPrice,
        CreditSale=CreditSale,
        Expense=Expense,
        Payment=Payment,
        DailyCashCount=DailyCashCount,
        Customer=Customer,
    )


def _filter_args():
    """Preserve period filter after create/edit."""
    args = {}
    period = (request.args.get('period') or request.form.get('period') or '').strip()
    if period:
        args['period'] = period
    start_date = (request.args.get('start_date') or request.form.get('start_date') or '').strip()
    end_date = (request.args.get('end_date') or request.form.get('end_date') or '').strip()
    if start_date:
        args['start_date'] = start_date
    if end_date:
        args['end_date'] = end_date
    return args


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def _refresh_till(day):
    stats = compute_period_stats(day, day, _models_ns())
    upsert_till_balance(day, stats['cash_in_hand'], current_user.id)
    return stats


def _parse_cash_taken_form(today):
    taken_date = parse_form_date(request.form.get('taken_date'), today)
    person_name = (request.form.get('person_name') or '').strip()
    note = (request.form.get('note') or '').strip() or None
    try:
        amount = float(request.form.get('amount') or 0)
        if amount <= 0:
            raise ValueError('Amount must be greater than zero.')
    except (TypeError, ValueError) as e:
        raise ValueError(f'Invalid cash taken amount: {e}') from e
    if not person_name:
        raise ValueError('Person name is required.')
    return taken_date, person_name, note, amount


def _parse_optional_money(field, label):
    """Optional money field from cash-taken forms. None = leave as-is."""
    raw = request.form.get(field)
    if raw in (None, ''):
        return None
    try:
        amount = float(raw)
        if amount < 0:
            raise ValueError(f'{label} cannot be negative.')
    except (TypeError, ValueError) as e:
        raise ValueError(f'Invalid {label.lower()}: {e}') from e
    return amount


def _available_to_take(taken_date, exclude_row=None, previous_override=None):
    """Remaining cash on taken_date, adding back exclude_row if it falls on that day."""
    stats = compute_period_stats(taken_date, taken_date, _models_ns())
    available = float(stats.get('remaining_balance') or 0)
    system_prev = float(stats.get('previous_balance') or 0)
    if previous_override is not None:
        available += float(previous_override) - system_prev
    if exclude_row is not None and _as_date(exclude_row.taken_date) == taken_date:
        available += float(exclude_row.amount or 0)
    return available, stats


@account_bp.route('/day-figures')
@login_required
def day_figures():
    """JSON: previous / cash in hand / taken / remaining for a date (live cash-taken UI)."""
    today = _today()
    day = parse_form_date(request.args.get('date'), today)
    exclude_id = request.args.get('exclude_taken_id', type=int)
    exclude_row = CashTaken.query.get(exclude_id) if exclude_id else None
    if exclude_row:
        require_agency_access(exclude_row)

    figures = day_account_figures(day, _models_ns())
    previous = float(figures['previous_balance'] or 0)
    cash_in_hand = float(figures['cash_in_hand'] or 0)
    taken = float(figures['cash_taken_total'] or 0)
    if exclude_row is not None and _as_date(exclude_row.taken_date) == day:
        taken = max(0.0, taken - float(exclude_row.amount or 0))
        # Recompute live remaining without this row; keep saved pending if no exclude adjust needed
        remaining = cash_in_hand - taken
    else:
        # Use saved “after cash taken” when present (e.g. 5005), else cash − taken
        remaining = float(figures.get('pending_balance') if figures.get('pending_balance') is not None
                          else (cash_in_hand - taken))
    carry_prev = carry_previous_for_date(day)
    till = apply_agency_filter(
        DailyTillBalance.query.filter_by(balance_date=day), DailyTillBalance
    ).first()
    saved_remaining = float(till.remaining_balance) if till is not None else None
    return jsonify({
        'date': day.isoformat(),
        'previous_balance': previous,
        'system_previous_balance': float(carry_prev or 0),
        'effective_previous_balance': float(previous_balance_for_date(day) or 0),
        'cash_in_hand': cash_in_hand,
        'cash_taken_total': taken,
        'remaining_balance': remaining,
        'saved_remaining': saved_remaining,
    })


@account_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    today = _today()

    if request.method == 'POST':
        action = request.form.get('action', 'cash_taken')
        if action == 'set_previous':
            balance_date = parse_form_date(request.form.get('balance_date'), today)
            try:
                previous_amount = float(request.form.get('previous_amount') or 0)
                if previous_amount < 0:
                    raise ValueError('Amount cannot be negative.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid previous amount: {e}', 'danger')
                return redirect(url_for('account.index', **_filter_args()))

            set_previous_balance(balance_date, previous_amount, current_user.id)
            db.session.flush()
            _refresh_till(balance_date)
            db.session.commit()
            flash(f'Previous balance set for {balance_date}: PKR {previous_amount:,.2f}.', 'success')
            return redirect(url_for('account.index', **_filter_args()))

        if action == 'edit_cash_taken':
            try:
                taken_id = int(request.form.get('taken_id') or 0)
            except (TypeError, ValueError):
                taken_id = 0
            row = CashTaken.query.get(taken_id)
            if not row:
                flash('Cash taken entry not found.', 'danger')
                return redirect(url_for('account.index', **_filter_args()))
            require_agency_access(row)

            try:
                taken_date, person_name, note, amount = _parse_cash_taken_form(today)
                previous_override = _parse_optional_money('previous_amount', 'Previous balance')
                remaining_after = _parse_optional_money('remaining_after', 'Balance after cash taken')
            except ValueError as e:
                flash(str(e), 'danger')
                return redirect(url_for('account.index', **_filter_args()))

            available, _ = _available_to_take(
                taken_date, exclude_row=row, previous_override=previous_override,
            )
            if amount > available + 0.01:
                flash(
                    f'Amount PKR {amount:,.2f} exceeds available cash PKR {available:,.2f}.',
                    'danger',
                )
                return redirect(url_for('account.index', **_filter_args()))

            old_date = _as_date(row.taken_date)
            row.taken_date = taken_date
            row.amount = amount
            row.person_name = person_name
            row.note = note
            db.session.flush()

            if previous_override is not None:
                set_previous_balance(taken_date, previous_override, current_user.id)
                db.session.flush()
            _refresh_till(taken_date)
            if remaining_after is not None:
                set_remaining_balance(taken_date, remaining_after, current_user.id)
            if old_date != taken_date:
                _refresh_till(old_date)
            db.session.commit()
            flash(f'Cash taken updated for {person_name}: PKR {amount:,.2f}.', 'success')
            return redirect(url_for('account.index', **_filter_args()))

        # Create cash taken
        try:
            taken_date, person_name, note, amount = _parse_cash_taken_form(today)
            previous_override = _parse_optional_money('previous_amount', 'Previous balance')
            remaining_after = _parse_optional_money('remaining_after', 'Balance after cash taken')
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(url_for('account.index', **_filter_args()))

        available, _ = _available_to_take(taken_date, previous_override=previous_override)
        if amount > available + 0.01:
            flash(
                f'Amount PKR {amount:,.2f} exceeds available cash PKR {available:,.2f}.',
                'danger',
            )
            return redirect(url_for('account.index', **_filter_args()))

        if previous_override is not None:
            set_previous_balance(taken_date, previous_override, current_user.id)
            db.session.flush()
        db.session.add(stamp_agency(CashTaken(
            taken_date=taken_date,
            amount=amount,
            person_name=person_name,
            note=note,
            recorded_by=current_user.id,
        )))
        db.session.flush()
        _refresh_till(taken_date)
        if remaining_after is not None:
            set_remaining_balance(taken_date, remaining_after, current_user.id)
        db.session.commit()
        flash(f'Cash taken recorded for {person_name}: PKR {amount:,.2f}.', 'success')
        return redirect(url_for('account.index', **_filter_args()))

    args = request.args.to_dict()
    if not (args.get('period') or '').strip():
        args['period'] = 'today'
    period, start, end = parse_period(args)
    stats = compute_period_stats(start, end, _models_ns())
    search_q = (request.args.get('search') or '').strip().lower()
    taken_rows = cash_taken_rows(start, end)
    if search_q:
        taken_rows = [
            r for r in taken_rows
            if search_q in (getattr(r, 'person_name', '') or '').lower()
            or search_q in (getattr(r, 'note', '') or '').lower()
            or search_q in str(getattr(r, 'taken_date', '') or '').lower()
        ]
    taken_rows = _enrich_taken_rows(taken_rows)

    return render_template(
        'account/index.html',
        today=today.isoformat(),
        stats=stats,
        taken_rows=taken_rows,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        search=search_q,
    )
