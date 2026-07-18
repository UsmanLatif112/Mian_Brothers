from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.inventory import inventory_bp
from app.models import (
    db, FuelType, FuelPrice, Inventory, StockEntry, OtherItem, ItemPurchaseLog, ItemPriceLog,
    Sale, Machine, CreditSale, DailyFuelStock,
)
from app.utils import paginate, parse_form_date, datetime_from_date, fuel_rate_for
from app.vendors.service import link_purchase_to_vendor
from datetime import datetime


PER_PAGE = 15


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


@inventory_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip().lower()
        vendor = (request.form.get('vendor') or '').strip() or None
        cost_price = request.form.get('cost_price')
        sale_price = request.form.get('sale_price')

        if category not in ('fuel', 'mobile', 'filter', 'other', 'ft_mobile'):
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
                    return redirect(url_for('inventory.index'))

            stock_entry = StockEntry(
                fuel_type_id=fuel_type.id,
                liters_added=liters_val,
                cost_per_liter=cost_val,
                supplier=vendor,
                entry_date=entry_dt,
                added_by=current_user.id
            )
            db.session.add(stock_entry)

            inventory = Inventory.query.filter_by(fuel_type_id=fuel_type.id).first()
            if not inventory:
                inventory = Inventory(fuel_type_id=fuel_type.id, current_stock_liters=0.0)
                db.session.add(inventory)
            inventory.current_stock_liters = float(inventory.current_stock_liters) + liters_val
            new_stock = float(inventory.current_stock_liters)

            # Sale price applies to the whole live stock for this fuel type
            current_rate = fuel_rate_for(fuel_type.id, FuelPrice) or 0.0
            if abs(float(current_rate) - float(sale_val)) > 0.0001 or current_rate <= 0:
                db.session.add(FuelPrice(
                    fuel_type_id=fuel_type.id,
                    price_per_liter=sale_val,
                    effective_date=entry_day,
                    updated_by=current_user.id
                ))

            # Every inventory add = one purchase log row (batch cost kept separately)
            batch_total = liters_val * cost_val
            purchase_log = ItemPurchaseLog(
                category='fuel',
                item_name=fuel_type.name,
                vendor=vendor,
                cost_price=cost_val,
                sale_price=sale_val,
                liters=liters_val,
                fuel_type_id=fuel_type.id,
                entry_date=entry_dt,
                added_by=current_user.id
            )
            db.session.add(purchase_log)
            link_purchase_to_vendor(vendor, purchase_log, stock_entry)
            db.session.commit()
            flash(
                f"Purchase logged: {liters_val:,.2f}L of {fuel_type.name} @ PKR {cost_val:,.2f}/L "
                f"(batch cost PKR {batch_total:,.2f}). Live stock now {new_stock:,.2f}L. "
                f"Sale price for all stock: PKR {sale_val:,.2f}/L.",
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

            purchase_log = ItemPurchaseLog(
                category='ft_mobile',
                item_name=item_name,
                company=company,
                item_type=None,
                vendor=vendor,
                cost_price=cost_val,
                sale_price=sale_val,
                quantity=None,
                liters=liters_val,
                entry_date=entry_dt,
                added_by=current_user.id,
            )
            db.session.add(purchase_log)
            link_purchase_to_vendor(vendor, purchase_log)
            db.session.commit()
            msg = (
                f"Purchase logged: {liters_val:,.2f}L FT Mobile Oil ({company}) "
                f"@ PKR {cost_val:,.2f}/L (batch cost PKR {liters_val * cost_val:,.2f}). "
                f"Live stock now {float(shop_item.liters):,.2f}L. "
                f"Sale price for all stock: PKR {sale_val:,.2f}/L."
            )
            flash(msg, 'success')
            return redirect(url_for('inventory.index'))

        company = (request.form.get('company') or '').strip() or None
        item_type = (request.form.get('item_type') or '').strip() or None
        quantity_raw = request.form.get('quantity')
        liters_raw = request.form.get('liters')

        if category == 'mobile':
            if not company or not item_type:
                flash('Mobile company name and type are required.', 'danger')
                return redirect(url_for('inventory.index'))
            item_name = f"{company} {item_type}"
        elif category == 'filter':
            if not company or not item_type:
                flash('Filter type and company name are required.', 'danger')
                return redirect(url_for('inventory.index'))
            item_name = f"{company} {item_type}"
        else:
            item_name = (request.form.get('item_name') or '').strip()
            if not item_name:
                flash('Item name is required for other items.', 'danger')
                return redirect(url_for('inventory.index'))

        try:
            qty_val = int(quantity_raw)
            if qty_val <= 0:
                raise ValueError('Quantity must be greater than zero.')
        except (TypeError, ValueError) as e:
            flash(f'Invalid quantity: {e}', 'danger')
            return redirect(url_for('inventory.index'))

        liters_val = None
        if category == 'mobile' and liters_raw:
            try:
                liters_val = float(liters_raw)
                if liters_val < 0:
                    raise ValueError('Liters cannot be negative.')
            except ValueError as e:
                flash(f'Invalid liters value: {e}', 'danger')
                return redirect(url_for('inventory.index'))

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
            effective_date=entry_day,
        )
        shop_item.sale_price = sale_val

        purchase_log = ItemPurchaseLog(
            category=category,
            item_name=item_name,
            company=company,
            item_type=item_type,
            vendor=vendor,
            cost_price=cost_val,
            sale_price=sale_val,
            quantity=qty_val,
            liters=liters_val,
            entry_date=entry_dt,
            added_by=current_user.id
        )
        db.session.add(purchase_log)
        link_purchase_to_vendor(vendor, purchase_log)
        db.session.commit()
        batch_total = qty_val * cost_val
        msg = (
            f"Purchase logged: {qty_val} × {item_name} @ PKR {cost_val:,.2f} "
            f"(batch cost PKR {batch_total:,.2f}). Stock now {shop_item.quantity}. "
            f"Sale price for product: PKR {sale_val:,.2f}."
        )
        flash(msg, 'success')
        return redirect(url_for('inventory.index'))

    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()
    live_stock = {}
    fuel_sale_rates = {}
    fuel_last_costs = {}
    for ft in fuel_types:
        live_stock[ft.id] = Inventory.query.filter_by(fuel_type_id=ft.id).first()
        fuel_sale_rates[ft.id] = fuel_rate_for(ft.id, FuelPrice) or 0.0
        last_buy = (
            ItemPurchaseLog.query
            .filter_by(category='fuel', fuel_type_id=ft.id)
            .order_by(ItemPurchaseLog.entry_date.desc(), ItemPurchaseLog.id.desc())
            .first()
        )
        fuel_last_costs[ft.id] = float(last_buy.cost_price) if last_buy else None

    stock_page = request.args.get('stock_page', 1)
    shop_items, shop_pagination = paginate(
        OtherItem.query.order_by(OtherItem.category.asc(), OtherItem.name.asc()),
        stock_page,
        PER_PAGE,
    )

    return render_template(
        'inventory/index.html',
        fuel_types=fuel_types,
        live_stock=live_stock,
        fuel_sale_rates=fuel_sale_rates,
        fuel_last_costs=fuel_last_costs,
        shop_items=shop_items,
        shop_pagination=shop_pagination,
        today=datetime.utcnow().date().isoformat(),
    )


@inventory_bp.route('/item/<int:item_id>/edit', methods=['POST'])
@login_required
def edit_item(item_id):
    item = OtherItem.query.get_or_404(item_id)
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
    if item.category == 'ft_mobile':
        item.quantity = 0
        if liters_val is None:
            flash('Liters are required for FT Mobile Oil.', 'danger')
            return redirect(url_for('inventory.index'))
        item.liters = liters_val
    else:
        item.quantity = qty_val
        if liters_val is not None:
            item.liters = liters_val

    if prev_sale != sale_val:
        db.session.add(ItemPriceLog(
            other_item_id=item.id,
            sale_price=sale_val,
            cost_price=cost_val,
            effective_date=datetime.utcnow().date(),
            updated_by=current_user.id,
        ))

    db.session.commit()
    flash(f'Updated {item.name}.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/item/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_item(item_id):
    item = OtherItem.query.get_or_404(item_id)
    name = item.name
    CreditSale.query.filter_by(other_item_id=item.id).update(
        {CreditSale.other_item_id: None}, synchronize_session=False
    )
    ItemPriceLog.query.filter_by(other_item_id=item.id).delete(synchronize_session=False)
    db.session.delete(item)
    db.session.commit()
    flash(f'Deleted “{name}” from inventory.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/fuel/<int:fuel_type_id>/edit', methods=['POST'])
@login_required
def edit_fuel(fuel_type_id):
    fuel = FuelType.query.get_or_404(fuel_type_id)
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Fuel name is required.', 'danger')
        return redirect(url_for('inventory.index'))

    conflict = FuelType.query.filter(
        db.func.lower(FuelType.name) == name.lower(),
        FuelType.id != fuel.id,
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
        db.session.add(inventory)
    inventory.current_stock_liters = stock_val
    inventory.reorder_threshold = threshold_val
    db.session.commit()
    flash(f'Updated {fuel.name} stock.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/fuel/<int:fuel_type_id>/delete', methods=['POST'])
@login_required
def delete_fuel(fuel_type_id):
    fuel = FuelType.query.get_or_404(fuel_type_id)
    name = fuel.name

    sale_count = Sale.query.filter_by(fuel_type_id=fuel.id).count()
    credit_count = CreditSale.query.filter_by(fuel_type_id=fuel.id).count()
    if sale_count or credit_count:
        flash(
            f'Cannot delete “{name}”: it is used in sale records.',
            'danger',
        )
        return redirect(url_for('inventory.index'))

    machine_count = Machine.query.filter_by(fuel_type_id=fuel.id).count()
    if machine_count:
        flash(
            f'Cannot delete “{name}”: {machine_count} machine(s) are linked to it.',
            'danger',
        )
        return redirect(url_for('inventory.index'))

    ItemPurchaseLog.query.filter_by(fuel_type_id=fuel.id).update(
        {ItemPurchaseLog.fuel_type_id: None}, synchronize_session=False
    )
    DailyFuelStock.query.filter_by(fuel_type_id=fuel.id).delete(synchronize_session=False)
    db.session.delete(fuel)
    db.session.commit()
    flash(f'Deleted fuel type “{name}”.', 'success')
    return redirect(url_for('inventory.index'))


@inventory_bp.route('/api/fuels', methods=['GET'])
@login_required
def api_fuels():
    """Fuel types from inventory — used by Add Inventory dropdown."""
    fuels = []
    for ft in FuelType.query.order_by(FuelType.name.asc()).all():
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

    fuel = FuelType.query.filter(db.func.lower(FuelType.name) == name.lower()).first()
    created = False
    if not fuel:
        fuel = FuelType(name=name, unit='Liter')
        db.session.add(fuel)
        db.session.flush()
        created = True

    inv = Inventory.query.filter_by(fuel_type_id=fuel.id).first()
    if not inv:
        inv = Inventory(fuel_type_id=fuel.id, current_stock_liters=0, reorder_threshold=0)
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
