"""Purchasing — all purchase logs with filter, search, edit, delete, receipt."""
from datetime import datetime

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required
from sqlalchemy import or_

from app.purchasing import purchasing_bp
from app.models import db, ItemPurchaseLog, Vendor
from app.tenancy import apply_agency_filter, require_agency_access
from app.utils import (
    paginate, parse_period, PERIOD_CHOICES, parse_form_date, datetime_from_date,
    business_today,
)
from app.vendors.service import (
    resolve_vendor,
    purchase_log_total,
    delete_purchase_log_cascade,
    apply_purchase_stock_delta,
    sync_auto_payment_for_log,
    recalculate_vendor_balance,
    purchase_log_payment_status,
    normalize_purchase_payment_status,
    counts_toward_purchase_kpis,
)
from app.inventory.categories import list_active_categories, ensure_shop_categories

PER_PAGE = 20

STATUS_CHOICES = (
    ('all', 'All statuses'),
    ('paid', 'Paid'),
    ('unpaid', 'Unpaid'),
)


def _category_choices():
    ensure_shop_categories()
    choices = [('all', 'All categories'), ('fuel', 'Fuel')]
    for cat in list_active_categories():
        key = (cat.key or '').strip()
        if not key or key == 'fuel':
            continue
        label = (cat.name or key).strip()
        choices.append((key, label))
    known = {c[0] for c in choices}
    for key, label in (
        ('mobile', 'Mobile Oil'),
        ('ft_mobile', 'FT Mobile Oil'),
        ('filter', 'Filter'),
        ('other', 'Other'),
    ):
        if key not in known:
            choices.append((key, label))
    return choices


@purchasing_bp.route('/', methods=['GET'])
@login_required
def index():
    if not request.args:
        return redirect(url_for('purchasing.index', period='all'))

    period, start, end = parse_period(request.args, default_period='all')
    category = (request.args.get('category') or 'all').strip().lower()
    status = (request.args.get('status') or 'all').strip().lower()
    if status not in dict(STATUS_CHOICES):
        status = 'all'
    search_q = (request.args.get('search') or '').strip()
    page = request.args.get('page', 1)

    logs_q = apply_agency_filter(ItemPurchaseLog.query, ItemPurchaseLog).filter(
        ItemPurchaseLog.entry_date >= datetime.combine(start, datetime.min.time()),
        ItemPurchaseLog.entry_date <= datetime.combine(end, datetime.max.time().replace(microsecond=0)),
        # Expense→vendor settles live on vendor ledger, not Purchasing stock buys
        ItemPurchaseLog.category != 'expense_settle',
        # Opening / previous stock (no vendor) stays out of purchasing activity
        ItemPurchaseLog.vendor_id.isnot(None),
    )

    if category and category != 'all':
        logs_q = logs_q.filter(ItemPurchaseLog.category == category)

    if search_q:
        like = f'%{search_q}%'
        logs_q = logs_q.filter(
            or_(
                ItemPurchaseLog.item_name.ilike(like),
                ItemPurchaseLog.vendor.ilike(like),
                ItemPurchaseLog.category.ilike(like),
                ItemPurchaseLog.company.ilike(like),
                ItemPurchaseLog.item_type.ilike(like),
            )
        )

    logs_q = logs_q.order_by(ItemPurchaseLog.entry_date.desc(), ItemPurchaseLog.id.desc())
    all_for_stats = logs_q.all()

    # Status filter after load (payment status is derived)
    status_map = {log.id: purchase_log_payment_status(log) for log in all_for_stats}
    if status in ('paid', 'unpaid'):
        filtered = [log for log in all_for_stats if status_map.get(log.id) == status]
    else:
        filtered = all_for_stats

    kpi_logs = [log for log in filtered if counts_toward_purchase_kpis(log)]
    purchase_total = sum(purchase_log_total(log) for log in kpi_logs)
    paid_total = sum(
        purchase_log_total(log) for log in kpi_logs if status_map.get(log.id) == 'paid'
    )
    unpaid_total = purchase_total - paid_total

    purchase_logs, hist_pagination = paginate(filtered, page, PER_PAGE)
    page_status = {log.id: status_map.get(log.id, 'unpaid') for log in purchase_logs}

    vendors = apply_agency_filter(Vendor.query, Vendor).order_by(Vendor.name.asc()).all()
    category_choices = _category_choices()
    if category not in dict(category_choices):
        category = 'all'

    return render_template(
        'purchasing/index.html',
        purchase_logs=purchase_logs,
        hist_pagination=hist_pagination,
        purchase_log_status=page_status,
        purchase_total=purchase_total,
        paid_total=paid_total,
        unpaid_total=unpaid_total,
        vendors=vendors,
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        category=category,
        category_choices=category_choices,
        status=status,
        status_choices=STATUS_CHOICES,
        search=search_q,
        today=business_today().isoformat(),
    )


@purchasing_bp.route('/log/<int:log_id>/edit', methods=['POST'])
@login_required
def edit_purchase_log(log_id):
    """Edit a purchase batch — updates stock, vendor payment, and payable."""
    log = ItemPurchaseLog.query.get_or_404(log_id)
    require_agency_access(log)
    try:
        cost_val = float(request.form.get('cost_price') or 0)
        sale_val = float(request.form.get('sale_price') or log.sale_price or 0)
        if cost_val < 0 or sale_val < 0:
            raise ValueError('Prices cannot be negative.')
    except (TypeError, ValueError) as e:
        flash(f'Invalid values: {e}', 'danger')
        return redirect(url_for('purchasing.index'))

    old_liters = float(log.liters or 0) if log.liters is not None else 0.0
    old_qty = int(log.quantity or 0) if log.quantity is not None else 0
    new_liters = old_liters
    new_qty = old_qty

    liters_raw = request.form.get('liters')
    qty_raw = request.form.get('quantity')
    if liters_raw not in (None, ''):
        try:
            new_liters = float(liters_raw)
            if new_liters < 0:
                raise ValueError('Liters cannot be negative.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid liters: {e}', 'danger')
            return redirect(url_for('purchasing.index'))
    if qty_raw not in (None, ''):
        try:
            new_qty = int(qty_raw)
            if new_qty < 0:
                raise ValueError('Quantity cannot be negative.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid quantity: {e}', 'danger')
            return redirect(url_for('purchasing.index'))

    vendor_id = (request.form.get('vendor_id') or '').strip() or None
    vendor_name = (request.form.get('vendor') or log.vendor or '').strip() or None
    payment_status = normalize_purchase_payment_status(request.form.get('payment_status'))
    entry_day = parse_form_date(request.form.get('entry_date'))
    apply_purchase_stock_delta(log, old_liters, old_qty, new_liters, new_qty)

    log.cost_price = cost_val
    log.sale_price = sale_val
    log.entry_date = datetime_from_date(entry_day)
    log.liters = new_liters if (log.category in ('fuel', 'ft_mobile') or new_liters > 0) else log.liters
    if log.category not in ('fuel', 'ft_mobile'):
        log.quantity = new_qty

    vendor = resolve_vendor(vendor_id=vendor_id, vendor_name=vendor_name)
    if vendor:
        log.vendor = vendor.name
        log.vendor_id = vendor.id
    elif vendor_name:
        log.vendor = vendor_name

    sync_auto_payment_for_log(log, payment_status=payment_status, payment_date=log.entry_date)
    db.session.flush()
    if log.vendor_id:
        vendor = Vendor.query.get(log.vendor_id)
        if vendor:
            recalculate_vendor_balance(vendor)
    db.session.commit()
    flash(f'Purchase log #{log.id} updated. Stock and vendor ledger recalculated.', 'success')
    return redirect(url_for('purchasing.index'))


@purchasing_bp.route('/log/<int:log_id>/delete', methods=['POST'])
@login_required
def delete_purchase_log(log_id):
    log = ItemPurchaseLog.query.get_or_404(log_id)
    require_agency_access(log)
    label = f'{log.item_name} (#{log.id})'
    delete_purchase_log_cascade(log)
    db.session.commit()
    flash(f'Deleted purchase {label}. Stock and vendor ledger updated.', 'success')
    return redirect(url_for('purchasing.index'))
