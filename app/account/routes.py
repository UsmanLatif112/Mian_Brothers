from datetime import datetime
from types import SimpleNamespace

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.account import account_bp
from app.account.service import cash_taken_rows, set_previous_balance, upsert_till_balance
from app.models import (
    db, CashTaken, CreditSale, Customer, DailyCashCount, Expense,
    FuelPrice, FuelType, MeterReading, Payment,
)
from app.utils import PERIOD_CHOICES, compute_period_stats, parse_form_date, parse_period


def _today():
    return datetime.utcnow().date()


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
    """Preserve period filter after create."""
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
            stats = compute_period_stats(balance_date, balance_date, _models_ns())
            upsert_till_balance(balance_date, stats['cash_in_hand'], current_user.id)
            db.session.commit()
            flash(f'Previous balance set for {balance_date}: PKR {previous_amount:,.2f}.', 'success')
            return redirect(url_for('account.index', **_filter_args()))

        taken_date = parse_form_date(request.form.get('taken_date'), today)
        person_name = (request.form.get('person_name') or '').strip()
        note = (request.form.get('note') or '').strip() or None
        try:
            amount = float(request.form.get('amount') or 0)
            if amount <= 0:
                raise ValueError('Amount must be greater than zero.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid cash taken amount: {e}', 'danger')
            return redirect(url_for('account.index', **_filter_args()))

        if not person_name:
            flash('Person name is required.', 'danger')
            return redirect(url_for('account.index', **_filter_args()))

        stats = compute_period_stats(taken_date, taken_date, _models_ns())
        available = float(stats.get('remaining_balance') or 0)
        if amount > available + 0.01:
            flash(
                f'Amount PKR {amount:,.2f} exceeds available cash PKR {available:,.2f}.',
                'danger',
            )
            return redirect(url_for('account.index', **_filter_args()))

        db.session.add(CashTaken(
            taken_date=taken_date,
            amount=amount,
            person_name=person_name,
            note=note,
            recorded_by=current_user.id,
        ))
        db.session.flush()

        stats = compute_period_stats(taken_date, taken_date, _models_ns())
        upsert_till_balance(taken_date, stats['cash_in_hand'], current_user.id)
        db.session.commit()
        flash(f'Cash taken recorded for {person_name}: PKR {amount:,.2f}.', 'success')
        return redirect(url_for('account.index', **_filter_args()))

    args = request.args.to_dict()
    if not (args.get('period') or '').strip():
        args['period'] = 'today'
    period, start, end = parse_period(args)
    stats = compute_period_stats(start, end, _models_ns())

    return render_template(
        'account/index.html',
        today=today.isoformat(),
        stats=stats,
        taken_rows=cash_taken_rows(start, end),
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
    )
