from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.purchasing import purchasing_bp
from app.models import (
    db, FuelType, FuelPrice, Inventory, StockEntry, OtherItem, ItemPurchaseLog, ItemPriceLog,
    Vendor, VendorPayment,
)
from app.utils import paginate, parse_period, PERIOD_CHOICES
from datetime import datetime, time
from sqlalchemy import func


PER_PAGE = 15

CATEGORY_CHOICES = (
    ('all', 'All Categories'),
    ('fuel', 'Fuel'),
    ('mobile', 'Mobile'),
    ('ft_mobile', 'FT Mobile Oil'),
    ('filter', 'Filter'),
    ('other', 'Other'),
)


def _purchase_amount(log):
    """Total purchase spend for one log row (cost × liters or quantity)."""
    cost = float(log.cost_price or 0)
    if log.category in ('fuel', 'ft_mobile'):
        return cost * float(log.liters or 0)
    return cost * float(log.quantity or 0)


def _period_bounds(start_date, end_date):
    return (
        datetime.combine(start_date, time.min),
        datetime.combine(end_date, time.max),
    )


def _compute_purchase_stats(start_date, end_date, category='all'):
    """Purchase totals for the selected period (and optional category)."""
    stats = {
        'petrol_purchase': 0.0,
        'petrol_liters': 0.0,
        'diesel_purchase': 0.0,
        'diesel_liters': 0.0,
        'mobile_purchase': 0.0,
        'mobile_qty': 0,
        'filter_purchase': 0.0,
        'filter_qty': 0,
        'other_purchase': 0.0,
        'other_qty': 0,
        'ft_mobile_purchase': 0.0,
        'ft_mobile_liters': 0.0,
    }

    start_dt, end_dt = _period_bounds(start_date, end_date)
    query = ItemPurchaseLog.query.filter(
        ItemPurchaseLog.entry_date >= start_dt,
        ItemPurchaseLog.entry_date <= end_dt,
    )
    if category and category != 'all':
        query = query.filter(ItemPurchaseLog.category == category)

    for log in query.all():
        amount = _purchase_amount(log)
        if log.category == 'fuel':
            name = (log.item_name or '').lower()
            if log.fuel_type and log.fuel_type.name:
                name = log.fuel_type.name.lower()
            liters = float(log.liters or 0)
            if 'petrol' in name or 'gasoline' in name:
                stats['petrol_purchase'] += amount
                stats['petrol_liters'] += liters
            elif 'diesel' in name:
                stats['diesel_purchase'] += amount
                stats['diesel_liters'] += liters
        elif log.category == 'mobile':
            stats['mobile_purchase'] += amount
            stats['mobile_qty'] += int(log.quantity or 0)
        elif log.category == 'ft_mobile':
            stats['ft_mobile_purchase'] += amount
            stats['ft_mobile_liters'] += float(log.liters or 0)
        elif log.category == 'filter':
            stats['filter_purchase'] += amount
            stats['filter_qty'] += int(log.quantity or 0)
        elif log.category == 'other':
            stats['other_purchase'] += amount
            stats['other_qty'] += int(log.quantity or 0)

    if (
        category in ('all', 'fuel')
        and not ItemPurchaseLog.query.filter_by(category='fuel').first()
    ):
        for entry in StockEntry.query.filter(
            StockEntry.entry_date >= start_dt,
            StockEntry.entry_date <= end_dt,
        ).all():
            ft = FuelType.query.get(entry.fuel_type_id)
            if not ft:
                continue
            amount = float(entry.cost_per_liter or 0) * float(entry.liters_added or 0)
            liters = float(entry.liters_added or 0)
            name = (ft.name or '').lower()
            if 'petrol' in name or 'gasoline' in name:
                stats['petrol_purchase'] += amount
                stats['petrol_liters'] += liters
            elif 'diesel' in name:
                stats['diesel_purchase'] += amount
                stats['diesel_liters'] += liters

    stats['total_purchase'] = (
        stats['petrol_purchase']
        + stats['diesel_purchase']
        + stats['mobile_purchase']
        + stats['ft_mobile_purchase']
        + stats['filter_purchase']
        + stats['other_purchase']
    )
    return stats


def _find_or_create_shop_item(category, name, company, item_type):
    query = OtherItem.query.filter_by(category=category, name=name)
    if company:
        query = query.filter_by(company=company)
    else:
        query = query.filter(OtherItem.company.is_(None))
    if item_type:
        query = query.filter_by(item_type=item_type)
    else:
        query = query.filter(OtherItem.item_type.is_(None))
    return query.first()


def _apply_product_sale_price(category, sale_val, company=None, item_type=None, name=None, cost_val=None, effective_date=None):
    query = OtherItem.query.filter_by(category=category)
    if company:
        query = query.filter_by(company=company)
    else:
        query = query.filter(OtherItem.company.is_(None))
    if item_type:
        query = query.filter_by(item_type=item_type)
    else:
        query = query.filter(OtherItem.item_type.is_(None))
    if category == 'other' and name:
        query = query.filter_by(name=name)

    updated = 0
    for item in query.all():
        prev = float(item.sale_price or 0)
        if prev == float(sale_val):
            continue
        item.sale_price = sale_val
        db.session.add(ItemPriceLog(
            other_item_id=item.id,
            sale_price=sale_val,
            cost_price=cost_val if cost_val is not None else item.cost_price,
            effective_date=effective_date or datetime.utcnow().date(),
            updated_by=current_user.id,
        ))
        updated += 1
    return updated


@purchasing_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip().lower()
        vendor = (request.form.get('vendor') or '').strip() or None
        cost_price = request.form.get('cost_price')
        sale_price = request.form.get('sale_price')

        if category not in ('fuel', 'mobile', 'filter', 'other', 'ft_mobile'):
            flash('Please select a valid item category.', 'danger')
            return redirect(url_for('purchasing.index'))

        try:
            cost_val = float(cost_price)
            sale_val = float(sale_price)
            if cost_val <= 0 or sale_val <= 0:
                raise ValueError('Cost and sale prices must be greater than zero.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid price values: {e}', 'danger')
            return redirect(url_for('purchasing.index'))

        if category == 'fuel':
            fuel_type_id = request.form.get('fuel_type_id')
            liters_added = request.form.get('liters')
            fuel_other_name = (request.form.get('fuel_other_name') or '').strip()

            if not fuel_type_id or not liters_added:
                flash('Fuel type and liters are required.', 'danger')
                return redirect(url_for('purchasing.index'))

            try:
                liters_val = float(liters_added)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except ValueError as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('purchasing.index'))

            if fuel_type_id == 'other':
                if not fuel_other_name:
                    flash('Please enter the fuel name.', 'danger')
                    return redirect(url_for('purchasing.index'))
                fuel_type = FuelType.query.filter(
                    db.func.lower(FuelType.name) == fuel_other_name.lower()
                ).first()
                if not fuel_type:
                    fuel_type = FuelType(name=fuel_other_name, unit='Liter')
                    db.session.add(fuel_type)
                    db.session.flush()
            else:
                fuel_type = FuelType.query.get(fuel_type_id)
                if not fuel_type:
                    flash('Fuel type not found.', 'danger')
                    return redirect(url_for('purchasing.index'))

            db.session.add(StockEntry(
                fuel_type_id=fuel_type.id,
                liters_added=liters_val,
                cost_per_liter=cost_val,
                supplier=vendor,
                entry_date=datetime.utcnow(),
                added_by=current_user.id
            ))

            inventory = Inventory.query.filter_by(fuel_type_id=fuel_type.id).first()
            if not inventory:
                inventory = Inventory(fuel_type_id=fuel_type.id, current_stock_liters=0.0)
                db.session.add(inventory)
            inventory.current_stock_liters = float(inventory.current_stock_liters) + liters_val

            db.session.add(FuelPrice(
                fuel_type_id=fuel_type.id,
                price_per_liter=sale_val,
                updated_by=current_user.id
            ))

            db.session.add(ItemPurchaseLog(
                category='fuel',
                item_name=fuel_type.name,
                vendor=vendor,
                cost_price=cost_val,
                sale_price=sale_val,
                liters=liters_val,
                fuel_type_id=fuel_type.id,
                entry_date=datetime.utcnow(),
                added_by=current_user.id
            ))
            db.session.commit()
            flash(
                f"Purchased {liters_val:.2f}L of {fuel_type.name}. "
                f"Sale price updated to PKR {sale_val:,.2f}/L.",
                'success'
            )
            return redirect(url_for('purchasing.index'))

        if category == 'ft_mobile':
            company = (request.form.get('company') or '').strip() or None
            liters_raw = request.form.get('liters')
            if not company:
                flash('Company name is required for FT Mobile Oil.', 'danger')
                return redirect(url_for('purchasing.index'))
            try:
                liters_val = float(liters_raw)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('purchasing.index'))

            item_name = company
            shop_item = _find_or_create_shop_item('ft_mobile', item_name, company, None)
            if shop_item:
                shop_item.liters = float(shop_item.liters or 0) + liters_val
                shop_item.vendor = vendor
                shop_item.cost_price = cost_val
            else:
                shop_item = OtherItem(
                    category='ft_mobile',
                    name=item_name,
                    company=company,
                    item_type=None,
                    vendor=vendor,
                    cost_price=cost_val,
                    sale_price=sale_val,
                    liters=liters_val,
                    quantity=0,
                )
                db.session.add(shop_item)

            db.session.flush()
            _apply_product_sale_price(
                'ft_mobile', sale_val, company=company, name=item_name, cost_val=cost_val,
                effective_date=datetime.utcnow().date(),
            )
            shop_item.sale_price = sale_val
            shop_item.quantity = 0

            db.session.add(ItemPurchaseLog(
                category='ft_mobile',
                item_name=item_name,
                company=company,
                vendor=vendor,
                cost_price=cost_val,
                sale_price=sale_val,
                liters=liters_val,
                entry_date=datetime.utcnow(),
                added_by=current_user.id,
            ))
            db.session.commit()
            flash(f'Purchased {liters_val:.2f}L of FT Mobile Oil ({company}).', 'success')
            return redirect(url_for('purchasing.index'))

        company = (request.form.get('company') or '').strip() or None
        item_type = (request.form.get('item_type') or '').strip() or None
        quantity_raw = request.form.get('quantity')
        liters_raw = request.form.get('liters')

        if category == 'mobile':
            if not company or not item_type:
                flash('Mobile company name and type are required.', 'danger')
                return redirect(url_for('purchasing.index'))
            item_name = f"{company} {item_type}"
        elif category == 'filter':
            if not company or not item_type:
                flash('Filter type and company name are required.', 'danger')
                return redirect(url_for('purchasing.index'))
            item_name = f"{company} {item_type}"
        else:
            item_name = (request.form.get('item_name') or '').strip()
            if not item_name:
                flash('Item name is required for other items.', 'danger')
                return redirect(url_for('purchasing.index'))

        try:
            qty_val = int(quantity_raw)
            if qty_val <= 0:
                raise ValueError('Quantity must be greater than zero.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid quantity: {e}', 'danger')
            return redirect(url_for('purchasing.index'))

        liters_val = None
        if category == 'mobile' and liters_raw:
            try:
                liters_val = float(liters_raw)
                if liters_val < 0:
                    raise ValueError('Liters cannot be negative.')
            except ValueError as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('purchasing.index'))

        shop_item = _find_or_create_shop_item(category, item_name, company, item_type)
        if shop_item:
            shop_item.quantity = int(shop_item.quantity) + qty_val
            shop_item.vendor = vendor
            shop_item.cost_price = cost_val
            if liters_val is not None:
                shop_item.liters = liters_val
        else:
            shop_item = OtherItem(
                category=category,
                name=item_name,
                company=company,
                item_type=item_type,
                vendor=vendor,
                cost_price=cost_val,
                sale_price=sale_val,
                liters=liters_val,
                quantity=qty_val
            )
            db.session.add(shop_item)

        db.session.flush()
        price_updates = _apply_product_sale_price(
            category,
            sale_val,
            company=company,
            item_type=item_type,
            name=item_name if category == 'other' else None,
            cost_val=cost_val,
            effective_date=datetime.utcnow().date(),
        )
        shop_item.sale_price = sale_val

        db.session.add(ItemPurchaseLog(
            category=category,
            item_name=item_name,
            company=company,
            item_type=item_type,
            vendor=vendor,
            cost_price=cost_val,
            sale_price=sale_val,
            quantity=qty_val,
            liters=liters_val,
            entry_date=datetime.utcnow(),
            added_by=current_user.id
        ))
        db.session.commit()
        msg = f"Purchased {qty_val} × {item_name}."
        if price_updates:
            msg += f" Sale price updated to PKR {sale_val:,.2f} for this product."
        else:
            msg += f" Sale price set to PKR {sale_val:,.2f}."
        flash(msg, 'success')
        return redirect(url_for('purchasing.index'))

    period, start_date, end_date = parse_period(request.args)
    category = (request.args.get('category') or 'all').strip().lower()
    if category not in dict(CATEGORY_CHOICES):
        category = 'all'

    start_dt, end_dt = _period_bounds(start_date, end_date)
    hist_page = request.args.get('page', 1)

    log_query = ItemPurchaseLog.query.filter(
        ItemPurchaseLog.entry_date >= start_dt,
        ItemPurchaseLog.entry_date <= end_dt,
    )
    if category != 'all':
        log_query = log_query.filter(ItemPurchaseLog.category == category)

    purchase_logs, hist_pagination = paginate(
        log_query.order_by(ItemPurchaseLog.entry_date.desc()),
        hist_page,
        PER_PAGE,
    )

    deliveries = []
    if hist_pagination['total'] == 0 and category in ('all', 'fuel'):
        if not ItemPurchaseLog.query.filter_by(category='fuel').first():
            deliveries, hist_pagination = paginate(
                StockEntry.query.filter(
                    StockEntry.entry_date >= start_dt,
                    StockEntry.entry_date <= end_dt,
                ).order_by(StockEntry.entry_date.desc()).all(),
                hist_page,
                PER_PAGE,
            )

    purchase_stats = _compute_purchase_stats(start_date, end_date, category)

    payable_total = float(
        Vendor.query.with_entities(
            func.coalesce(func.sum(Vendor.current_balance_payable), 0)
        ).scalar() or 0
    )
    paid_total = float(
        VendorPayment.query.with_entities(
            func.coalesce(func.sum(VendorPayment.amount_paid), 0)
        ).scalar() or 0
    )

    return render_template(
        'purchasing/index.html',
        purchase_logs=purchase_logs,
        deliveries=deliveries,
        hist_pagination=hist_pagination,
        purchase_stats=purchase_stats,
        purchase_amount=_purchase_amount,
        purchase_total=purchase_stats.get('total_purchase', 0),
        payable_total=payable_total,
        paid_total=paid_total,
        period=period,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        period_choices=PERIOD_CHOICES,
        category=category,
        category_choices=CATEGORY_CHOICES,
    )
