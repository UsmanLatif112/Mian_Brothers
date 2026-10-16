from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.inventory import inventory_bp
from app.models import (
    db, FuelType, FuelPrice, Inventory, StockEntry, OtherItem, ItemPurchaseLog, ItemPriceLog,
    Sale, Machine, MeterReading, CreditSale, DailyFuelStock, Vendor,
    ShopCategory, ShopCategoryOption,
)
from app.tenancy import apply_agency_filter, require_agency_access, stamp_agency
from app.utils import paginate, parse_form_date, datetime_from_date, fuel_rate_for, business_today
from app.vendors.service import (
    link_purchase_to_vendor,
    resolve_vendor,
    apply_purchase_vendor_payment,
    purchase_log_total,
    delete_purchase_log_cascade,
    apply_purchase_stock_delta,
    sync_auto_payment_for_log,
    recalculate_vendor_balance,
    find_auto_payments_for_log,
    purchase_log_payment_status,
    normalize_purchase_payment_status,
)
from app.services.stock_batches import (
    create_batch_from_purchase_log,
    list_batches_for_fuel,
    list_batches_for_item,
)
from app.inventory.categories import (
    list_active_categories, category_payload, unique_category_key, slugify_category,
    category_has_linked_activity, option_has_linked_activity, ensure_shop_categories,
)
from datetime import datetime


PER_PAGE = 15


def _inventory_vendor_and_payment():
    """Require vendor; default payment status is unpaid (posts to vendor account)."""
    vendor_id = (request.form.get('vendor_id') or '').strip() or None
    vendor_name = (request.form.get('vendor') or '').strip() or None
    payment_status = (request.form.get('payment_status') or 'unpaid').strip().lower()
    if payment_status not in ('unpaid', 'paid'):
        payment_status = 'unpaid'

    vendor = resolve_vendor(vendor_id=vendor_id, vendor_name=vendor_name)
    if not vendor:
        return None, payment_status, 'Vendor is required. Search or add a vendor for this purchase.'
    return vendor, payment_status, None


def _find_or_create_shop_item(category, name, company, item_type):
    query = apply_agency_filter(
        OtherItem.query.filter_by(category=category, name=name), OtherItem
    )
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
    query = apply_agency_filter(OtherItem.query.filter_by(category=category), OtherItem)
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
        db.session.add(stamp_agency(ItemPriceLog(
            other_item_id=item.id,
            sale_price=sale_val,
            cost_price=cost_val if cost_val is not None else item.cost_price,
            effective_date=effective_date or business_today(),
            updated_by=current_user.id,
        )))
        updated += 1
    return updated


@inventory_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip().lower()
        cost_price = request.form.get('cost_price')
        sale_price = request.form.get('sale_price')

        cat_row = apply_agency_filter(
            ShopCategory.query.filter_by(key=category, is_active=True), ShopCategory
        ).first()
        if not cat_row and category not in ('fuel', 'other', 'ft_mobile'):
            flash('Please select a valid item category.', 'danger')
            return redirect(url_for('inventory.index'))

        try:
            cost_val = float(cost_price)
            sale_val = float(sale_price)
            if cost_val <= 0 or sale_val <= 0:
                raise ValueError('Cost and sale prices must be greater than zero.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid price values: {e}', 'danger')
            return redirect(url_for('inventory.index'))

        entry_day = parse_form_date(request.form.get('entry_date'))
        entry_dt = datetime_from_date(entry_day)

        vendor, payment_status, vendor_error = _inventory_vendor_and_payment()
        if vendor_error:
            flash(vendor_error, 'danger')
            return redirect(url_for('inventory.index'))

        if category == 'fuel':
            fuel_type_id = request.form.get('fuel_type_id')
            liters_added = request.form.get('liters')
            fuel_other_name = (request.form.get('fuel_other_name') or '').strip()

            if not fuel_type_id or not liters_added:
                flash('Fuel type and liters are required.', 'danger')
                return redirect(url_for('inventory.index'))

            try:
                liters_val = float(liters_added)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except ValueError as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('inventory.index'))

            if fuel_type_id == 'other':
                if not fuel_other_name:
                    flash('Please enter the fuel name.', 'danger')
                    return redirect(url_for('inventory.index'))
                fuel_type = apply_agency_filter(
                    FuelType.query.filter(
                        db.func.lower(FuelType.name) == fuel_other_name.lower()
                    ),
                    FuelType,
                ).first()
                if not fuel_type:
                    fuel_type = FuelType(name=fuel_other_name, unit='Liter')
                    stamp_agency(fuel_type)
                    db.session.add(fuel_type)
                    db.session.flush()
            else:
                fuel_type = FuelType.query.get(fuel_type_id)
                if not fuel_type:
                    flash('Fuel type not found.', 'danger')
                    return redirect(url_for('inventory.index'))
                require_agency_access(fuel_type)

            stock_entry = StockEntry(
                fuel_type_id=fuel_type.id,
                liters_added=liters_val,
                cost_per_liter=cost_val,
                supplier=vendor.name,
                entry_date=entry_dt,
                added_by=current_user.id
            )
            stamp_agency(stock_entry)
            db.session.add(stock_entry)

            inventory = Inventory.query.filter_by(fuel_type_id=fuel_type.id).first()
            if not inventory:
                inventory = Inventory(fuel_type_id=fuel_type.id, current_stock_liters=0.0)
                stamp_agency(inventory)
                db.session.add(inventory)
            inventory.current_stock_liters = float(inventory.current_stock_liters) + liters_val
            new_stock = float(inventory.current_stock_liters)

            # Sale price applies to the whole live stock for this fuel type
            current_rate = fuel_rate_for(fuel_type.id, FuelPrice) or 0.0
            if abs(float(current_rate) - float(sale_val)) > 0.0001 or current_rate <= 0:
                db.session.add(stamp_agency(FuelPrice(
                    fuel_type_id=fuel_type.id,
                    price_per_liter=sale_val,
                    effective_date=entry_day,
                    effective_at=entry_dt,
                    updated_by=current_user.id
                )))

            # Every inventory add = one purchase log row (batch cost kept separately)
            batch_total = liters_val * cost_val
            purchase_log = ItemPurchaseLog(
                category='fuel',
                item_name=fuel_type.name,
                vendor=vendor.name,
                cost_price=cost_val,
                sale_price=sale_val,
                liters=liters_val,
                fuel_type_id=fuel_type.id,
                payment_status=payment_status,
                entry_date=entry_dt,
                added_by=current_user.id
            )
            stamp_agency(purchase_log)
            db.session.add(purchase_log)
            link_purchase_to_vendor(vendor.name, purchase_log, stock_entry, vendor=vendor)
            apply_purchase_vendor_payment(
                vendor, purchase_log, payment_status=payment_status, payment_date=entry_dt
            )
            create_batch_from_purchase_log(purchase_log)
            db.session.commit()
            batch_total = purchase_log_total(purchase_log)
            if payment_status == 'paid':
                pay_note = (
                    f'Paid PKR {batch_total:,.2f} settled on vendor account '
                    f'(payable now PKR {float(vendor.current_balance_payable):,.2f}).'
                )
            else:
                pay_note = (
                    f'Unpaid PKR {batch_total:,.2f} added to vendor payable '
                    f'(payable now PKR {float(vendor.current_balance_payable):,.2f}).'
                )
            flash(
                f"Purchase logged: {liters_val:,.2f}L of {fuel_type.name} @ PKR {cost_val:,.2f}/L "
                f"(batch cost PKR {batch_total:,.2f}). Live stock now {new_stock:,.2f}L. "
                f"Sale price for all stock: PKR {sale_val:,.2f}/L. Vendor: {vendor.name}. {pay_note}",
                'success'
            )
            return redirect(url_for('inventory.index'))

        # ---------- FT Mobile Oil (liquid stock — liters only, no quantity) ----------
        if category == 'ft_mobile':
            company = (request.form.get('company') or '').strip() or None
            liters_raw = request.form.get('liters')
            if not company:
                flash('Company name is required for FT Mobile Oil.', 'danger')
                return redirect(url_for('inventory.index'))
            try:
                liters_val = float(liters_raw)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('inventory.index'))

            item_name = company
            shop_item = _find_or_create_shop_item('ft_mobile', item_name, company, None)
            if shop_item:
                shop_item.liters = float(shop_item.liters or 0) + liters_val
                shop_item.vendor = vendor.name
                shop_item.cost_price = cost_val
                shop_item.stock_unit = 'liters'
            else:
                shop_item = OtherItem(
                    category='ft_mobile',
                    name=item_name,
                    company=company,
                    item_type=None,
                    vendor=vendor.name,
                    cost_price=cost_val,
                    sale_price=sale_val,
                    liters=liters_val,
                    quantity=0,
                    stock_unit='liters',
                )
                stamp_agency(shop_item)
                db.session.add(shop_item)

            db.session.flush()
            price_updates = _apply_product_sale_price(
                'ft_mobile',
                sale_val,
                company=company,
                name=item_name,
                cost_val=cost_val,
                effective_date=entry_day,
            )
            shop_item.sale_price = sale_val
            shop_item.quantity = 0
            shop_item.stock_unit = 'liters'

            purchase_log = ItemPurchaseLog(
                category='ft_mobile',
                item_name=item_name,
                company=company,
                item_type=None,
                vendor=vendor.name,
                cost_price=cost_val,
                sale_price=sale_val,
                quantity=None,
                liters=liters_val,
                payment_status=payment_status,
                entry_date=entry_dt,
                added_by=current_user.id,
            )
            stamp_agency(purchase_log)
            db.session.add(purchase_log)
            link_purchase_to_vendor(vendor.name, purchase_log, vendor=vendor)
            apply_purchase_vendor_payment(
                vendor, purchase_log, payment_status=payment_status, payment_date=entry_dt
            )
            create_batch_from_purchase_log(purchase_log, other_item_id=shop_item.id)
            db.session.commit()
            batch_total = purchase_log_total(purchase_log)
            if payment_status == 'paid':
                pay_note = (
                    f'Paid PKR {batch_total:,.2f} settled on vendor account '
                    f'(payable now PKR {float(vendor.current_balance_payable):,.2f}).'
                )
            else:
                pay_note = (
                    f'Unpaid PKR {batch_total:,.2f} added to vendor payable '
                    f'(payable now PKR {float(vendor.current_balance_payable):,.2f}).'
                )
            msg = (
                f"Purchase logged: {liters_val:,.2f}L FT Mobile Oil ({company}) "
                f"@ PKR {cost_val:,.2f}/L (batch cost PKR {batch_total:,.2f}). "
                f"Live stock now {float(shop_item.liters):,.2f}L. "
                f"Sale price for all stock: PKR {sale_val:,.2f}/L. Vendor: {vendor.name}. {pay_note}"
            )
            flash(msg, 'success')
            return redirect(url_for('inventory.index'))

        company = (request.form.get('company') or '').strip() or None
        item_type = (request.form.get('item_type') or '').strip() or None
        quantity_raw = request.form.get('quantity')
        liters_raw = request.form.get('liters')
        # Other: pieces (qty) or liters — chosen on the form
        stock_unit = (request.form.get('stock_unit') or '').strip().lower()
        if stock_unit not in ('qty', 'liters'):
            stock_unit = 'qty'

        item_name = (request.form.get('item_name') or '').strip()
        if not item_name:
            if company and item_type:
                item_name = f"{company} {item_type}"
            elif company:
                item_name = company
            else:
                flash('Item name is required.', 'danger')
                return redirect(url_for('inventory.index'))

        # Other + Liters (and any leftover custom liter categories)
        if category == 'other' and stock_unit == 'liters':
            try:
                liters_val = float(liters_raw)
                if liters_val <= 0:
                    raise ValueError('Liters must be greater than zero.')
            except (TypeError, ValueError) as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('inventory.index'))

            shop_item = _find_or_create_shop_item(category, item_name, company, item_type)
            if shop_item:
                shop_item.liters = float(shop_item.liters or 0) + liters_val
                shop_item.vendor = vendor.name
                shop_item.cost_price = cost_val
                shop_item.stock_unit = 'liters'
            else:
                shop_item = OtherItem(
                    category=category,
                    name=item_name,
                    company=company,
                    item_type=item_type,
                    vendor=vendor.name,
                    cost_price=cost_val,
                    sale_price=sale_val,
                    liters=liters_val,
                    quantity=0,
                    stock_unit='liters',
                )
                stamp_agency(shop_item)
                db.session.add(shop_item)
            db.session.flush()
            _apply_product_sale_price(
                category, sale_val, company=company, item_type=item_type,
                name=item_name, cost_val=cost_val, effective_date=entry_day,
            )
            shop_item.sale_price = sale_val
            shop_item.quantity = 0
            shop_item.stock_unit = 'liters'
            purchase_log = ItemPurchaseLog(
                category=category,
                item_name=item_name,
                company=company,
                item_type=item_type,
                vendor=vendor.name,
                cost_price=cost_val,
                sale_price=sale_val,
                quantity=None,
                liters=liters_val,
                payment_status=payment_status,
                entry_date=entry_dt,
                added_by=current_user.id,
            )
            stamp_agency(purchase_log)
            db.session.add(purchase_log)
            link_purchase_to_vendor(vendor.name, purchase_log, vendor=vendor)
            apply_purchase_vendor_payment(
                vendor, purchase_log, payment_status=payment_status, payment_date=entry_dt
            )
            create_batch_from_purchase_log(purchase_log, other_item_id=shop_item.id)
            db.session.commit()
            flash(
                f"Purchase logged: {liters_val:,.2f}L {item_name} "
                f"(stock now {float(shop_item.liters):,.2f}L).",
                'success',
            )
            return redirect(url_for('inventory.index'))

        try:
            qty_val = int(quantity_raw)
            if qty_val <= 0:
                raise ValueError('Quantity must be greater than zero.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid quantity: {e}', 'danger')
            return redirect(url_for('inventory.index'))

        liters_val = None
        shop_item = _find_or_create_shop_item(category, item_name, company, item_type)
        if shop_item:
            shop_item.quantity = int(shop_item.quantity or 0) + qty_val
            shop_item.vendor = vendor.name
            shop_item.cost_price = cost_val
            shop_item.stock_unit = 'qty'
        else:
            shop_item = OtherItem(
                category=category,
                name=item_name,
                company=company,
                item_type=item_type,
                vendor=vendor.name,
                cost_price=cost_val,
                sale_price=sale_val,
                liters=None,
                quantity=qty_val,
                stock_unit='qty',
            )
            stamp_agency(shop_item)
            db.session.add(shop_item)

        db.session.flush()
        price_updates = _apply_product_sale_price(
            category,
            sale_val,
            company=company,
            item_type=item_type,
            name=item_name if category == 'other' else None,
            cost_val=cost_val,
            effective_date=entry_day,
        )
        shop_item.sale_price = sale_val
        shop_item.stock_unit = 'qty'

        purchase_log = ItemPurchaseLog(
            category=category,
            item_name=item_name,
            company=company,
            item_type=item_type,
            vendor=vendor.name,
            cost_price=cost_val,
            sale_price=sale_val,
            quantity=qty_val,
            liters=liters_val,
            payment_status=payment_status,
            entry_date=entry_dt,
            added_by=current_user.id
        )
        stamp_agency(purchase_log)
        db.session.add(purchase_log)
        link_purchase_to_vendor(vendor.name, purchase_log, vendor=vendor)
        apply_purchase_vendor_payment(
            vendor, purchase_log, payment_status=payment_status, payment_date=entry_dt
        )
        create_batch_from_purchase_log(purchase_log, other_item_id=shop_item.id)
        db.session.commit()
        batch_total = purchase_log_total(purchase_log)
        if payment_status == 'paid':
            pay_note = (
                f'Paid PKR {batch_total:,.2f} settled on vendor account '
                f'(payable now PKR {float(vendor.current_balance_payable):,.2f}).'
            )
        else:
            pay_note = (
                f'Unpaid PKR {batch_total:,.2f} added to vendor payable '
                f'(payable now PKR {float(vendor.current_balance_payable):,.2f}).'
            )
        msg = (
            f"Purchase logged: {qty_val} × {item_name} @ PKR {cost_val:,.2f} "
            f"(batch cost PKR {batch_total:,.2f}). Stock now {shop_item.quantity}. "
            f"Sale price for product: PKR {sale_val:,.2f}. Vendor: {vendor.name}. {pay_note}"
        )
        flash(msg, 'success')
        return redirect(url_for('inventory.index'))

    fuel_types = apply_agency_filter(FuelType.query, FuelType, include_shared=True).order_by(FuelType.name.asc()).all()
    live_stock = {}
    fuel_sale_rates = {}
    fuel_last_costs = {}
    for ft in fuel_types:
        live_stock[ft.id] = Inventory.query.filter_by(fuel_type_id=ft.id).first()
        fuel_sale_rates[ft.id] = fuel_rate_for(ft.id, FuelPrice) or 0.0
        last_buy = (
            apply_agency_filter(
                ItemPurchaseLog.query.filter_by(category='fuel', fuel_type_id=ft.id),
                ItemPurchaseLog,
            )
            .order_by(ItemPurchaseLog.entry_date.desc(), ItemPurchaseLog.id.desc())
            .first()
        )
        fuel_last_costs[ft.id] = float(last_buy.cost_price) if last_buy else None

    search_q = (request.args.get('search') or '').strip()
    stock_page = request.args.get('stock_page', 1)
    items_q = apply_agency_filter(OtherItem.query, OtherItem).order_by(
        OtherItem.category.asc(), OtherItem.name.asc()
    )
    if search_q:
        like = f'%{search_q}%'
        items_q = items_q.filter(
            db.or_(
                OtherItem.name.ilike(like),
                OtherItem.category.ilike(like),
                OtherItem.vendor.ilike(like),
                OtherItem.company.ilike(like),
            )
        )
    shop_items, shop_pagination = paginate(items_q, stock_page, PER_PAGE)

    vendors = apply_agency_filter(Vendor.query, Vendor).order_by(Vendor.name.asc()).all()
    existing_fuel_names = {ft.name.lower() for ft in fuel_types}
    needs_default_fuels = 'petrol' not in existing_fuel_names or 'diesel' not in existing_fuel_names
    ensure_shop_categories()
    shop_categories = [category_payload(c) for c in list_active_categories()]

    from app.charts_data import inventory_listing_series

    return render_template(
        'inventory/index.html',
        fuel_types=fuel_types,
        live_stock=live_stock,
        fuel_sale_rates=fuel_sale_rates,
        fuel_last_costs=fuel_last_costs,
        shop_items=shop_items,
        shop_pagination=shop_pagination,
        vendors=vendors,
        today=business_today().isoformat(),
        chart_series=inventory_listing_series(),
        search=search_q,
        needs_default_fuels=needs_default_fuels,
        shop_categories=shop_categories,
    )


@inventory_bp.route('/purchase-log/<int:log_id>/edit', methods=['POST'])
@login_required
def edit_purchase_log(log_id):
    """Legacy URL — purchasing page owns edit now."""
    return redirect(url_for('purchasing.index'))


@inventory_bp.route('/purchase-log/<int:log_id>/delete', methods=['POST'])
@login_required
def delete_purchase_log(log_id):
    """Legacy URL — purchasing page owns delete now."""
    return redirect(url_for('purchasing.index'))


@inventory_bp.route('/item/<int:item_id>/edit', methods=['POST'])
@login_required
def edit_item(item_id):
    item = OtherItem.query.get_or_404(item_id)
    require_agency_access(item)
    name = (request.form.get('name') or '').strip()
    vendor = (request.form.get('vendor') or '').strip() or None
    company = (request.form.get('company') or '').strip() or None
    item_type = (request.form.get('item_type') or '').strip() or None

    if not name:
        flash('Item name is required.', 'danger')
        return redirect(url_for('inventory.index'))

    try:
        cost_val = float(request.form.get('cost_price') or 0)
        sale_val = float(request.form.get('sale_price') or 0)
        qty_val = int(request.form.get('quantity') or 0)
        if cost_val < 0 or sale_val < 0 or qty_val < 0:
            raise ValueError('Values cannot be negative.')
    except (TypeError, ValueError) as e:
        flash(f'Invalid values: {e}', 'danger')
        return redirect(url_for('inventory.index'))

    liters_val = None
    liters_raw = request.form.get('liters')
    if liters_raw not in (None, ''):
        try:
            liters_val = float(liters_raw)
            if liters_val < 0:
                raise ValueError('Liters cannot be negative.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid liters: {e}', 'danger')
            return redirect(url_for('inventory.index'))

    prev_sale = float(item.sale_price or 0)
    item.name = name
    item.vendor = vendor
    item.company = company
    item.item_type = item_type
    item.cost_price = cost_val
    item.sale_price = sale_val
    if item.tracks_liters():
        item.quantity = 0
        item.stock_unit = 'liters'
        if liters_val is None:
            flash('Liters are required for liter-stock items.', 'danger')
            return redirect(url_for('inventory.index'))
        item.liters = liters_val
    else:
        item.quantity = qty_val
        item.stock_unit = 'qty'
        if liters_val is not None:
            item.liters = liters_val

    if prev_sale != sale_val:
        db.session.add(stamp_agency(ItemPriceLog(
            other_item_id=item.id,
            sale_price=sale_val,
            cost_price=cost_val,
            effective_date=business_today(),
            updated_by=current_user.id,
        )))

    db.session.commit()
    flash(f'Updated {item.name}.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/item/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_item(item_id):
    item = OtherItem.query.get_or_404(item_id)
    require_agency_access(item)
    name = item.name
    # Detach sales history; keep ledger rows but clear item FK
    CreditSale.query.filter_by(other_item_id=item.id).update(
        {CreditSale.other_item_id: None}, synchronize_session=False
    )
    ItemPriceLog.query.filter_by(other_item_id=item.id).delete(synchronize_session=False)

    purchase_logs = ItemPurchaseLog.query.filter(
        ItemPurchaseLog.category == (item.category or 'other'),
        ItemPurchaseLog.item_name == item.name,
        ItemPurchaseLog.company == item.company,
        ItemPurchaseLog.item_type == item.item_type,
    ).all()
    vendor_ids = set()
    for log in purchase_logs:
        # Stock lives on the item row we're deleting — skip reverse_purchase_stock
        for pay in find_auto_payments_for_log(log):
            db.session.delete(pay)
        if log.vendor_id:
            vendor_ids.add(log.vendor_id)
        db.session.delete(log)

    db.session.delete(item)
    db.session.flush()

    for vid in vendor_ids:
        vendor = Vendor.query.get(vid)
        if vendor:
            recalculate_vendor_balance(vendor)

    db.session.commit()
    flash(f'Deleted “{name}” with purchase logs and vendor payments. Balances recalculated.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/fuel/<int:fuel_type_id>/edit', methods=['POST'])
@login_required
def edit_fuel(fuel_type_id):
    fuel = FuelType.query.get_or_404(fuel_type_id)
    require_agency_access(fuel)
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Fuel name is required.', 'danger')
        return redirect(url_for('inventory.index'))

    conflict = apply_agency_filter(
        FuelType.query.filter(
            db.func.lower(FuelType.name) == name.lower(),
            FuelType.id != fuel.id,
        ),
        FuelType,
    ).first()
    if conflict:
        flash(f'Fuel type “{name}” already exists.', 'danger')
        return redirect(url_for('inventory.index'))

    try:
        stock_val = float(request.form.get('stock_liters') or 0)
        threshold_val = float(request.form.get('reorder_threshold') or 0)
        if stock_val < 0 or threshold_val < 0:
            raise ValueError('Values cannot be negative.')
    except (TypeError, ValueError) as e:
        flash(f'Invalid values: {e}', 'danger')
        return redirect(url_for('inventory.index'))

    fuel.name = name
    inventory = Inventory.query.filter_by(fuel_type_id=fuel.id).first()
    if not inventory:
        inventory = Inventory(fuel_type_id=fuel.id, current_stock_liters=0, reorder_threshold=0)
        stamp_agency(inventory)
        db.session.add(inventory)
    inventory.current_stock_liters = stock_val
    inventory.reorder_threshold = threshold_val
    db.session.commit()
    flash(f'Updated {fuel.name} stock.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/fuel/seed-defaults', methods=['POST'])
@login_required
def seed_default_fuels():
    """Create default Petrol and Diesel fuel categories from the UI."""
    created = []
    for name in ('Petrol', 'Diesel'):
        fuel = apply_agency_filter(
            FuelType.query.filter(db.func.lower(FuelType.name) == name.lower()),
            FuelType,
        ).first()
        if fuel:
            continue
        fuel = FuelType(name=name, unit='Liter')
        stamp_agency(fuel)
        db.session.add(fuel)
        db.session.flush()
        db.session.add(stamp_agency(Inventory(
            fuel_type_id=fuel.id,
            current_stock_liters=0,
            reorder_threshold=0,
        )))
        created.append(name)

    db.session.commit()
    if created:
        flash(f'Created fuel categories: {", ".join(created)}.', 'success')
    else:
        flash('Petrol and Diesel already exist.', 'info')
    next_page = (request.form.get('next') or '').strip()
    if next_page == 'categories':
        return redirect(url_for('inventory.categories'))
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/fuel/<int:fuel_type_id>/delete', methods=['POST'])
@login_required
def delete_fuel(fuel_type_id):
    """Delete fuel type and cascade related stock / meter / machine / purchase rows."""
    fuel = FuelType.query.get_or_404(fuel_type_id)
    require_agency_access(fuel)
    name = fuel.name

    # Detach sale history (keep customer ledger rows, clear fuel FK)
    CreditSale.query.filter_by(fuel_type_id=fuel.id).update(
        {CreditSale.fuel_type_id: None}, synchronize_session=False
    )
    Sale.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)

    # Machines → their meter readings first
    machine_ids = [m.id for m in Machine.query.filter_by(fuel_type_id=fuel.id).all()]
    if machine_ids:
        MeterReading.query.filter(MeterReading.machine_id.in_(machine_ids)).delete(
            synchronize_session=False
        )
    MeterReading.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)
    Machine.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)

    # Remove fuel purchase batches + auto vendor payments, then rebuild payables
    fuel_purchases = ItemPurchaseLog.query.filter_by(fuel_type_id=fuel.id).all()
    vendor_ids = {log.vendor_id for log in fuel_purchases if log.vendor_id}
    for log in fuel_purchases:
        for pay in find_auto_payments_for_log(log):
            db.session.delete(pay)
        db.session.delete(log)
    db.session.flush()

    DailyFuelStock.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)
    StockEntry.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)
    FuelPrice.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)
    Inventory.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)

    db.session.delete(fuel)
    db.session.flush()

    for vid in vendor_ids:
        vendor = Vendor.query.get(vid)
        if vendor:
            recalculate_vendor_balance(vendor)

    db.session.commit()
    flash(f'Deleted fuel “{name}” with stock, meters, machines, and purchase batches. Vendor balances recalculated.', 'success')
    next_page = (request.form.get('next') or '').strip()
    if next_page == 'categories':
        return redirect(url_for('inventory.categories'))
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/categories')
@login_required
def categories():
    """Dedicated page to manage inventory categories and subcategories."""
    ensure_shop_categories()
    cat_rows = list_active_categories()
    cats = []
    for row in cat_rows:
        payload = category_payload(row)
        payload['linked'] = category_has_linked_activity(row)
        for opt in (payload.get('company_options') or []):
            opt_row = next((o for o in row.options if o.id == opt['id']), None)
            opt['linked'] = bool(opt_row and option_has_linked_activity(opt_row))
        for opt in (payload.get('type_options') or []):
            opt_row = next((o for o in row.options if o.id == opt['id']), None)
            opt['linked'] = bool(opt_row and option_has_linked_activity(opt_row))
        cats.append(payload)
    fuel_types = apply_agency_filter(FuelType.query, FuelType, include_shared=True).order_by(FuelType.name.asc()).all()
    existing_fuel_names = {(ft.name or '').strip().lower() for ft in fuel_types}
    needs_default_fuels = 'petrol' not in existing_fuel_names or 'diesel' not in existing_fuel_names
    company_count = sum(len(c.get('company_options') or []) for c in cats)
    type_count = sum(len(c.get('type_options') or []) for c in cats)
    return render_template(
        'inventory/categories.html',
        categories=cats,
        fuel_types=fuel_types,
        needs_default_fuels=needs_default_fuels,
        stats={
            'categories': len(cats),
            'fuels': len(fuel_types),
            'companies': company_count,
            'types': type_count,
        },
    )


@inventory_bp.route('/categories/create', methods=['POST'])
@login_required
def categories_create():
    flash(
        'Only Fuel, FT Mobile Oil, and Other are used. '
        'Put mobiles, filters, and similar under Other (Pieces or Liters).',
        'warning',
    )
    return redirect(url_for('inventory.categories'))


@inventory_bp.route('/categories/<int:category_id>/delete', methods=['POST'])
@login_required
def categories_delete(category_id):
    cat = ShopCategory.query.get_or_404(category_id)
    require_agency_access(cat)
    if cat.is_system:
        flash('System categories cannot be deleted.', 'danger')
        return redirect(url_for('inventory.categories'))
    if category_has_linked_activity(cat):
        flash(
            f'Cannot delete “{cat.name}”. It already has inventory stock or purchase logs. '
            f'Clear linked items first.',
            'danger',
        )
        return redirect(url_for('inventory.categories'))
    name = cat.name
    db.session.delete(cat)
    db.session.commit()
    flash(f'Deleted category “{name}”.', 'success')
    return redirect(url_for('inventory.categories'))


@inventory_bp.route('/categories/<int:category_id>/options', methods=['POST'])
@login_required
def categories_add_option(category_id):
    cat = ShopCategory.query.get_or_404(category_id)
    require_agency_access(cat)
    kind = (request.form.get('kind') or '').strip().lower()
    name = (request.form.get('name') or '').strip()
    if kind not in ('company', 'type'):
        flash('Invalid option kind.', 'danger')
        return redirect(url_for('inventory.categories'))
    if not name:
        flash('Name is required.', 'danger')
        return redirect(url_for('inventory.categories'))

    existing = ShopCategoryOption.query.filter_by(
        category_id=cat.id, kind=kind, name=name
    ).first()
    if existing:
        flash(f'“{name}” already exists under {cat.name}.', 'info')
        return redirect(url_for('inventory.categories'))

    db.session.add(ShopCategoryOption(category_id=cat.id, kind=kind, name=name))
    db.session.commit()
    label = 'Company' if kind == 'company' else 'Type'
    flash(f'{label} “{name}” added to {cat.name}.', 'success')
    return redirect(url_for('inventory.categories'))


@inventory_bp.route('/categories/options/<int:option_id>/delete', methods=['POST'])
@login_required
def categories_delete_option(option_id):
    opt = ShopCategoryOption.query.get_or_404(option_id)
    cat = opt.category or ShopCategory.query.get(opt.category_id)
    require_agency_access(cat)
    if option_has_linked_activity(opt):
        flash(
            f'Cannot remove “{opt.name}”. It is already used on inventory stock or purchase logs.',
            'danger',
        )
        return redirect(url_for('inventory.categories'))
    name = opt.name
    db.session.delete(opt)
    db.session.commit()
    flash(f'Removed “{name}”.', 'success')
    return redirect(url_for('inventory.categories'))


@inventory_bp.route('/categories/fuel', methods=['POST'])
@login_required
def categories_add_fuel():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Fuel type name is required.', 'danger')
        return redirect(url_for('inventory.categories'))

    fuel = apply_agency_filter(
        FuelType.query.filter(db.func.lower(FuelType.name) == name.lower()),
        FuelType,
    ).first()
    if fuel:
        flash(f'Fuel type “{fuel.name}” already exists.', 'info')
        return redirect(url_for('inventory.categories'))

    fuel = FuelType(name=name, unit='Liter')
    stamp_agency(fuel)
    db.session.add(fuel)
    db.session.flush()
    db.session.add(stamp_agency(Inventory(
        fuel_type_id=fuel.id,
        current_stock_liters=0,
        reorder_threshold=0,
    )))
    db.session.commit()
    flash(f'Fuel type “{name}” added. It will show in Inventory and Sales.', 'success')
    return redirect(url_for('inventory.categories'))


@inventory_bp.route('/api/fuels', methods=['GET'])
@login_required
def api_fuels():
    """Fuel types from inventory — used by Add Inventory dropdown."""
    fuels = []
    for ft in apply_agency_filter(FuelType.query, FuelType, include_shared=True).order_by(FuelType.name.asc()).all():
        inv = Inventory.query.filter_by(fuel_type_id=ft.id).first()
        stock = float(inv.current_stock_liters) if inv else 0.0
        rate = fuel_rate_for(ft.id, FuelPrice) or 0.0
        fuels.append({
            'id': ft.id,
            'name': ft.name,
            'rate': rate,
            'stock': stock,
            'text': f'{ft.name} · {stock:,.2f} L in stock',
        })
    return jsonify({'ok': True, 'fuels': fuels})


@inventory_bp.route('/api/quick/fuel', methods=['POST'])
@login_required
def quick_fuel():
    """Create a fuel type if missing; return existing if name already in inventory."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Fuel name is required'}), 400

    fuel = apply_agency_filter(
        FuelType.query.filter(db.func.lower(FuelType.name) == name.lower()),
        FuelType,
    ).first()
    created = False
    if not fuel:
        fuel = FuelType(name=name, unit='Liter')
        stamp_agency(fuel)
        db.session.add(fuel)
        db.session.flush()
        created = True
    else:
        require_agency_access(fuel)

    inv = Inventory.query.filter_by(fuel_type_id=fuel.id).first()
    if not inv:
        inv = Inventory(fuel_type_id=fuel.id, current_stock_liters=0, reorder_threshold=0)
        stamp_agency(inv)
        db.session.add(inv)

    db.session.commit()
    stock = float(inv.current_stock_liters or 0)
    rate = fuel_rate_for(fuel.id, FuelPrice) or 0.0
    return jsonify({
        'ok': True,
        'id': fuel.id,
        'value': fuel.id,
        'name': fuel.name,
        'text': f'{fuel.name} · {stock:,.2f} L in stock',
        'rate': rate,
        'stock': stock,
        'created': created,
    })


@inventory_bp.route('/api/categories', methods=['GET'])
@login_required
def api_categories():
    ensure_shop_categories()
    return jsonify({
        'ok': True,
        'categories': [category_payload(c) for c in list_active_categories()],
    })


@inventory_bp.route('/api/categories', methods=['POST'])
@login_required
def api_create_category():
    return jsonify({
        'ok': False,
        'error': 'Only Fuel, FT Mobile Oil, and Other are available. Use Other with Pieces or Liters.',
    }), 400


@inventory_bp.route('/api/categories/<int:category_id>/options', methods=['POST'])
@login_required
def api_add_category_option(category_id):
    cat = ShopCategory.query.get_or_404(category_id)
    require_agency_access(cat)
    data = request.get_json(silent=True) or {}
    kind = (data.get('kind') or '').strip().lower()
    name = (data.get('name') or '').strip()
    if kind not in ('company', 'type'):
        return jsonify({'ok': False, 'error': 'Kind must be company or type.'}), 400
    if not name:
        return jsonify({'ok': False, 'error': 'Option name is required.'}), 400

    existing = ShopCategoryOption.query.filter_by(
        category_id=cat.id, kind=kind, name=name
    ).first()
    if existing:
        return jsonify({'ok': True, 'option': {'id': existing.id, 'kind': kind, 'name': name}, 'created': False})

    opt = ShopCategoryOption(category_id=cat.id, kind=kind, name=name)
    db.session.add(opt)
    db.session.commit()
    return jsonify({'ok': True, 'option': {'id': opt.id, 'kind': kind, 'name': name}, 'created': True})


@inventory_bp.route('/api/categories/options/<int:option_id>/delete', methods=['POST'])
@login_required
def api_delete_category_option(option_id):
    opt = ShopCategoryOption.query.get_or_404(option_id)
    cat = opt.category or ShopCategory.query.get(opt.category_id)
    require_agency_access(cat)
    if option_has_linked_activity(opt):
        return jsonify({
            'ok': False,
            'error': f'Cannot remove “{opt.name}”. It is already used on inventory stock or purchase logs.',
        }), 400
    db.session.delete(opt)
    db.session.commit()
    return jsonify({'ok': True})


@inventory_bp.route('/api/categories/<int:category_id>/delete', methods=['POST'])
@login_required
def api_delete_category(category_id):
    cat = ShopCategory.query.get_or_404(category_id)
    require_agency_access(cat)
    if cat.is_system:
        return jsonify({'ok': False, 'error': 'System categories cannot be deleted.'}), 400
    if category_has_linked_activity(cat):
        return jsonify({
            'ok': False,
            'error': f'Cannot delete “{cat.name}”. It already has inventory stock or purchase logs.',
        }), 400
    db.session.delete(cat)
    db.session.commit()
    return jsonify({'ok': True})


@inventory_bp.route('/batches')
@login_required
def batches():
    """FIFO stock layers still on hand (oldest sold first)."""
    fuel_types = apply_agency_filter(FuelType.query, FuelType, include_shared=True).order_by(FuelType.name.asc()).all()
    fuel_sections = []
    for ft in fuel_types:
        rows = list_batches_for_fuel(ft.id, include_empty=False)
        if not rows:
            continue
        rem = sum(float(b.qty_remaining or 0) for b in rows)
        value = sum(float(b.qty_remaining or 0) * float(b.unit_cost or 0) for b in rows)
        fuel_sections.append({
            'label': ft.name,
            'remaining': rem,
            'value': value,
            'batches': rows,
            'unit': 'L',
        })

    shop_items = apply_agency_filter(OtherItem.query, OtherItem).order_by(
        OtherItem.category.asc(), OtherItem.name.asc()
    ).all()
    shop_sections = []
    for item in shop_items:
        rows = list_batches_for_item(item.id, include_empty=False)
        if not rows:
            continue
        rem = sum(float(b.qty_remaining or 0) for b in rows)
        value = sum(float(b.qty_remaining or 0) * float(b.unit_cost or 0) for b in rows)
        shop_sections.append({
            'label': item.name,
            'remaining': rem,
            'value': value,
            'batches': rows,
            'unit': 'L' if item.tracks_liters() else 'pcs',
        })

    return render_template(
        'inventory/batches.html',
        fuel_sections=fuel_sections,
        shop_sections=shop_sections,
    )
