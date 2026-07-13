from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.inventory import inventory_bp
from app.models import db, FuelType, FuelPrice, Inventory, StockEntry, OtherItem, ItemPurchaseLog
from datetime import datetime


def _find_or_create_shop_item(category, name, company, item_type):
    """Match existing shop stock by category + name (+ company/type when present)."""
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


@inventory_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip().lower()
        vendor = (request.form.get('vendor') or '').strip() or None
        cost_price = request.form.get('cost_price')
        sale_price = request.form.get('sale_price')

        if category not in ('fuel', 'mobile', 'filter', 'other'):
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

        # ---- Fuel ----
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

            entry = StockEntry(
                fuel_type_id=fuel_type.id,
                liters_added=liters_val,
                cost_per_liter=cost_val,
                supplier=vendor,
                entry_date=datetime.utcnow(),
                added_by=current_user.id
            )
            db.session.add(entry)

            inventory = Inventory.query.filter_by(fuel_type_id=fuel_type.id).first()
            if not inventory:
                inventory = Inventory(fuel_type_id=fuel_type.id, current_stock_liters=0.0)
                db.session.add(inventory)
            inventory.current_stock_liters = float(inventory.current_stock_liters) + liters_val

            # Update active sale price for this fuel
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
            flash(f"Added {liters_val:.2f}L of {fuel_type.name} from {vendor or 'vendor'}.", 'success')
            return redirect(url_for('inventory.index'))

        # ---- Mobile / Filter / Other ----
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
            shop_item.sale_price = sale_val
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
        flash(f"Added {qty_val} × {item_name} to inventory.", 'success')
        return redirect(url_for('inventory.index'))

    # GET
    fuel_types = FuelType.query.all()
    live_stock = {}
    for ft in fuel_types:
        live_stock[ft.id] = Inventory.query.filter_by(fuel_type_id=ft.id).first()

    purchase_logs = ItemPurchaseLog.query.order_by(ItemPurchaseLog.entry_date.desc()).limit(100).all()
    # Fallback: include legacy tanker entries not yet mirrored
    if not purchase_logs:
        deliveries = StockEntry.query.order_by(StockEntry.entry_date.desc()).all()
    else:
        deliveries = []

    shop_items = OtherItem.query.order_by(OtherItem.category.asc(), OtherItem.name.asc()).all()

    return render_template(
        'inventory/index.html',
        fuel_types=fuel_types,
        live_stock=live_stock,
        purchase_logs=purchase_logs,
        deliveries=deliveries,
        shop_items=shop_items
    )
