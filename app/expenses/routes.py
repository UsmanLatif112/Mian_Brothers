from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.expenses import expenses_bp
from app.models import db, Expense
from app.utils import parse_period, PERIOD_CHOICES, paginate
from app.services.entries import EntryError, edit_expense, delete_expense
from datetime import datetime
from sqlalchemy import func

PER_PAGE = 15


@expenses_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        action = request.form.get('action', 'create')

        if action == 'create':
            name = (request.form.get('name') or '').strip()
            description = (request.form.get('description') or '').strip() or None
            amount_raw = request.form.get('amount')
            date_raw = (request.form.get('expense_date') or '').strip()

            if not name:
                flash('Expense name is required.', 'danger')
                return redirect(url_for('expenses.index'))

            try:
                amount = float(amount_raw)
                if amount <= 0:
                    raise ValueError('Amount must be greater than zero.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid amount: {e}', 'danger')
                return redirect(url_for('expenses.index'))

            try:
                expense_date = (
                    datetime.strptime(date_raw, '%Y-%m-%d').date()
                    if date_raw else datetime.utcnow().date()
                )
            except ValueError:
                flash('Invalid expense date.', 'danger')
                return redirect(url_for('expenses.index'))

            db.session.add(Expense(
                name=name,
                description=description,
                amount=amount,
                expense_date=expense_date,
                recorded_by=current_user.id,
                is_settled=False,
            ))
            db.session.commit()
            flash(f'Expense "{name}" recorded ({amount:,.2f} PKR).', 'success')
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'settle':
            expense = Expense.query.get(request.form.get('expense_id'))
            if not expense:
                flash('Expense not found.', 'danger')
                return redirect(url_for('expenses.index', **_filter_args()))

            if expense.is_settled:
                flash('Expense is already settled.', 'warning')
                return redirect(url_for('expenses.index', **_filter_args()))

            date_raw = (request.form.get('settled_date') or '').strip()
            settle_note = (request.form.get('settle_note') or '').strip() or None
            try:
                settled_date = (
                    datetime.strptime(date_raw, '%Y-%m-%d').date()
                    if date_raw else datetime.utcnow().date()
                )
            except ValueError:
                flash('Invalid settle date.', 'danger')
                return redirect(url_for('expenses.index', **_filter_args()))

            expense.is_settled = True
            expense.settled_date = settled_date
            expense.settled_by = current_user.id
            expense.settle_note = settle_note
            db.session.commit()
            flash(
                f'Settled "{expense.name}" — {float(expense.amount):,.2f} PKR returned to cash '
                f'({settled_date.isoformat()}).',
                'success',
            )
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'unsettle':
            expense = Expense.query.get(request.form.get('expense_id'))
            if not expense:
                flash('Expense not found.', 'danger')
                return redirect(url_for('expenses.index', **_filter_args()))

            if not expense.is_settled:
                flash('Expense is not settled.', 'warning')
                return redirect(url_for('expenses.index', **_filter_args()))

            expense.is_settled = False
            expense.settled_date = None
            expense.settled_by = None
            expense.settle_note = None
            db.session.commit()
            flash(
                f'Unsettled "{expense.name}" — amount again deducted from cash in hand.',
                'success',
            )
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'edit':
            expense = Expense.query.get(request.form.get('expense_id'))
            if not expense:
                flash('Expense not found.', 'danger')
                return redirect(url_for('expenses.index', **_filter_args()))
            try:
                edit_expense(expense, request.form, current_user_id=current_user.id)
                db.session.commit()
                flash(f'Expense "{expense.name}" updated.', 'success')
            except EntryError as e:
                db.session.rollback()
                flash(str(e), 'danger')
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'delete':
            expense = Expense.query.get(request.form.get('expense_id'))
            if expense:
                delete_expense(expense)
                db.session.commit()
                flash('Expense deleted.', 'success')
            return redirect(url_for('expenses.index', **_filter_args()))

        flash('Unknown action.', 'danger')
        return redirect(url_for('expenses.index'))

    period, start, end = parse_period(request.args)

    total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.expense_date >= start, Expense.expense_date <= end)
        .scalar()
        or 0
    )
    unsettled_total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(
            Expense.expense_date >= start,
            Expense.expense_date <= end,
            Expense.is_settled.is_(False),
        )
        .scalar()
        or 0
    )
    settled_total = total - unsettled_total

    expenses_q = (
        Expense.query
        .filter(Expense.expense_date >= start, Expense.expense_date <= end)
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
    )
    expenses, expenses_pagination = paginate(expenses_q, request.args.get('page', 1), PER_PAGE)

    return render_template(
        'expenses/index.html',
        expenses=expenses,
        expenses_pagination=expenses_pagination,
        total=total,
        unsettled_total=unsettled_total,
        settled_total=settled_total,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        today=datetime.utcnow().date().isoformat(),
    )


def _filter_args():
    """Preserve period filter after create/delete (from query string or referrer)."""
    args = {}
    period = (request.args.get('period') or request.form.get('period') or '').strip()
    start = (request.args.get('start_date') or request.form.get('start_date') or '').strip()
    end = (request.args.get('end_date') or request.form.get('end_date') or '').strip()

    # POST forms don't include period fields — fall back to referrer query
    if not period and request.referrer:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(request.referrer).query)
        period = (qs.get('period') or [''])[0]
        start = start or (qs.get('start_date') or [''])[0]
        end = end or (qs.get('end_date') or [''])[0]

    if period:
        args['period'] = period
    if period == 'custom':
        if start:
            args['start_date'] = start
        if end:
            args['end_date'] = end
    return args
