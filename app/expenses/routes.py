from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.expenses import expenses_bp
from app.models import db, Expense, Customer, CreditSale, Vendor
from app.tenancy import apply_agency_filter, require_agency_access, stamp_agency
from app.utils import (
    parse_period, PERIOD_CHOICES, paginate, parse_form_date, business_today,
    expense_settle_remark,
)
from app.services.entries import EntryError, edit_expense, delete_expense
from app.customers.service import recalculate_customer_balance
from app.vendors.service import (
    upsert_expense_vendor_payable, clear_expense_vendor_payable, recalculate_vendor_balance,
)
from sqlalchemy import func
from sqlalchemy.orm import joinedload

PER_PAGE = 15


@expenses_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    today = business_today()

    # First open → Today (bookmarkable URL), same as Sales / Journal
    if request.method == 'GET' and not request.args:
        return redirect(url_for('expenses.index', period='today'))

    if request.method == 'POST':
        action = request.form.get('action', 'create')

        if action == 'create':
            name = (request.form.get('name') or '').strip()
            description = (request.form.get('description') or '').strip() or None
            amount_raw = request.form.get('amount')

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

            expense_date = parse_form_date(request.form.get('expense_date'), today)

            db.session.add(stamp_agency(Expense(
                name=name,
                description=description,
                amount=amount,
                expense_date=expense_date,
                recorded_by=current_user.id,
                is_settled=False,
            )))
            db.session.commit()
            flash(f'Expense "{name}" recorded ({amount:,.2f} PKR).', 'success')
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'settle':
            expense = Expense.query.get(request.form.get('expense_id'))
            if not expense:
                flash('Expense not found.', 'danger')
                return redirect(url_for('expenses.index', **_filter_args()))
            require_agency_access(expense)

            if expense.is_settled or expense.settled_date:
                flash('Expense is already settled.', 'warning')
                return redirect(url_for('expenses.index', **_filter_args()))

            settle_note = (request.form.get('settle_note') or '').strip() or None
            settle_method = (request.form.get('settle_method') or 'cash').strip().lower()
            if settle_method not in ('cash', 'credit', 'vendor'):
                settle_method = 'cash'
            settled_date = parse_form_date(request.form.get('settled_date'), today)

            customer = None
            credit_sale = None
            vendor = None
            if settle_method == 'credit':
                try:
                    customer_id = int(request.form.get('settle_customer_id') or 0)
                except (TypeError, ValueError):
                    customer_id = 0
                customer = Customer.query.get(customer_id) if customer_id else None
                if not customer:
                    flash('Select a customer to settle this expense as credit.', 'danger')
                    return redirect(url_for('expenses.index', **_filter_args()))
                require_agency_access(customer)
                amount = float(expense.amount or 0)
                remark = expense_settle_remark(expense.name, settle_note)
                credit_sale = stamp_agency(CreditSale(
                    customer_id=customer.id,
                    sale_date=settled_date,
                    liters=0,
                    rate=0,
                    amount=amount,
                    amount_paid=0,
                    entry_type='loan',
                    payment_status='unpaid',
                    remarks=remark,
                    recorded_by=current_user.id,
                ))
                db.session.add(credit_sale)
                db.session.flush()
            elif settle_method == 'vendor':
                try:
                    vendor_id = int(request.form.get('settle_vendor_id') or 0)
                except (TypeError, ValueError):
                    vendor_id = 0
                vendor = Vendor.query.get(vendor_id) if vendor_id else None
                if not vendor:
                    flash('Select a vendor to settle this expense as unpaid.', 'danger')
                    return redirect(url_for('expenses.index', **_filter_args()))
                require_agency_access(vendor)

            expense.is_settled = True
            expense.settled_date = settled_date
            expense.settled_by = current_user.id
            expense.settle_note = settle_note
            expense.settle_method = settle_method
            expense.settle_customer_id = customer.id if customer else None
            expense.settle_credit_sale_id = credit_sale.id if credit_sale else None
            expense.settle_vendor_id = None
            expense.settle_purchase_log_id = None
            if customer:
                recalculate_customer_balance(customer)
            if vendor:
                upsert_expense_vendor_payable(
                    expense, vendor, settled_date, settle_note, current_user.id
                )
            db.session.commit()
            if settle_method == 'credit':
                flash(
                    f'Settled "{expense.name}" → credit on {customer.name}: '
                    f'PKR {float(expense.amount):,.2f} (no cash change). '
                    f'Balance due PKR {float(customer.current_balance_due):,.2f}.',
                    'success',
                )
            elif settle_method == 'vendor':
                flash(
                    f'Settled "{expense.name}" → unpaid on {vendor.name}: '
                    f'PKR {float(expense.amount):,.2f} (no cash change). '
                    f'Payable PKR {float(vendor.current_balance_payable):,.2f}.',
                    'success',
                )
            else:
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
            require_agency_access(expense)

            if not (expense.is_settled or expense.settled_date):
                flash('Expense is not settled.', 'warning')
                return redirect(url_for('expenses.index', **_filter_args()))

            customer = None
            if expense.settle_credit_sale_id:
                cs = CreditSale.query.get(expense.settle_credit_sale_id)
                if cs:
                    customer = Customer.query.get(cs.customer_id) if cs.customer_id else None
                    db.session.delete(cs)
            elif expense.settle_customer_id:
                customer = Customer.query.get(expense.settle_customer_id)

            vendor_ids = clear_expense_vendor_payable(expense)

            expense.is_settled = False
            expense.settled_date = None
            expense.settled_by = None
            expense.settle_note = None
            expense.settle_method = 'cash'
            expense.settle_customer_id = None
            expense.settle_credit_sale_id = None
            if customer:
                recalculate_customer_balance(customer)
            for vid in vendor_ids:
                v = Vendor.query.get(vid)
                if v:
                    recalculate_vendor_balance(v)
            db.session.commit()
            extra = []
            if customer:
                extra.append('customer credit removed')
            if vendor_ids:
                extra.append('vendor unpaid removed')
            flash(
                f'Unsettled "{expense.name}" — open again'
                f'{" (" + ", ".join(extra) + ")" if extra else ""}.',
                'success',
            )
            return redirect(url_for('expenses.index', **_filter_args()))

        if action == 'edit':
            expense = Expense.query.get(request.form.get('expense_id'))
            if not expense:
                flash('Expense not found.', 'danger')
                return redirect(url_for('expenses.index', **_filter_args()))
            require_agency_access(expense)
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
                require_agency_access(expense)
                delete_expense(expense)
                db.session.commit()
                flash('Expense deleted.', 'success')
            return redirect(url_for('expenses.index', **_filter_args()))

        flash('Unknown action.', 'danger')
        return redirect(url_for('expenses.index'))

    period, start, end = parse_period(request.args, default_period='today')

    total = float(
        apply_agency_filter(
            db.session.query(func.coalesce(func.sum(Expense.amount), 0))
            .filter(Expense.expense_date >= start, Expense.expense_date <= end),
            Expense,
        ).scalar() or 0
    )
    # Unsettled = no settle date and flag not set (settled_date is source of truth)
    unsettled_total = float(
        apply_agency_filter(
            db.session.query(func.coalesce(func.sum(Expense.amount), 0))
            .filter(
                Expense.expense_date >= start,
                Expense.expense_date <= end,
                Expense.settled_date.is_(None),
                db.or_(Expense.is_settled.is_(False), Expense.is_settled.is_(None)),
            ),
            Expense,
        ).scalar() or 0
    )
    settled_total = total - unsettled_total

    search_q = (request.args.get('search') or '').strip()
    expenses_q = (
        apply_agency_filter(
            Expense.query.options(
                joinedload(Expense.settle_customer),
                joinedload(Expense.settle_vendor),
            ).filter(Expense.expense_date >= start, Expense.expense_date <= end),
            Expense,
        )
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
    )
    if search_q:
        like = f'%{search_q}%'
        expenses_q = expenses_q.filter(
            db.or_(
                Expense.name.ilike(like),
                Expense.description.ilike(like),
                Expense.settle_note.ilike(like),
            )
        )
    expenses, expenses_pagination = paginate(expenses_q, request.args.get('page', 1), PER_PAGE)
    customers = apply_agency_filter(Customer.query, Customer).order_by(Customer.name.asc()).all()
    vendors = apply_agency_filter(Vendor.query, Vendor).order_by(Vendor.name.asc()).all()

    from app.charts_data import expenses_listing_series

    return render_template(
        'expenses/index.html',
        expenses=expenses,
        expenses_pagination=expenses_pagination,
        customers=customers,
        vendors=vendors,
        total=total,
        unsettled_total=unsettled_total,
        settled_total=settled_total,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        today=today.isoformat(),
        chart_series=expenses_listing_series(expenses),
        search=search_q,
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
    search = (request.args.get('search') or request.form.get('search') or '').strip()
    if not search and request.referrer:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(request.referrer).query)
        search = (qs.get('search') or [''])[0]
    if search:
        args['search'] = search
    return args
