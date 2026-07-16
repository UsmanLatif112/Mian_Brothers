from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.expenses import expenses_bp
from app.models import db, Expense
from app.utils import parse_period, PERIOD_CHOICES, paginate
from datetime import datetime

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
            ))
            db.session.commit()
            flash(f'Expense "{name}" recorded ({amount:,.2f} PKR).', 'success')
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'delete':
            expense = Expense.query.get(request.form.get('expense_id'))
            if expense:
                db.session.delete(expense)
                db.session.commit()
                flash('Expense deleted.', 'success')
            return redirect(url_for('expenses.index', **_filter_args()))

        flash('Unknown action.', 'danger')
        return redirect(url_for('expenses.index'))

    period, start, end = parse_period(request.args)
    from sqlalchemy import func

    # Sum must use a separate query — Query.with_entities() mutates in place
    # and would break the list/pagination query if chained on the same object.
    total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.expense_date >= start, Expense.expense_date <= end)
        .scalar()
        or 0
    )
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
