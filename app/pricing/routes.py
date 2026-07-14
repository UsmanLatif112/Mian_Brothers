from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.pricing import pricing_bp
from app.models import db, FuelType, FuelPrice, OtherItem, ItemPriceLog
from app.decorators import role_required
from app.utils import parse_form_date
from datetime import datetime


@pricing_bp.route('/', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def index():
    if request.method == 'POST':
        item_key = (request.form.get('item_key') or '').strip()
        new_price = request.form.get('price')
        effective = parse_form_date(request.form.get('effective_date'))

        if not item_key or not new_price:
            flash('Item and price are required.', 'danger')
            return redirect(url_for('pricing.index'))

        try:
            price_val = float(new_price)
            if price_val <= 0:
                raise ValueError('Price must be greater than zero.')
        except ValueError as e:
            flash(f'Invalid price value: {e}', 'danger')
            return redirect(url_for('pricing.index'))

        if item_key.startswith('fuel:'):
            fuel_type_id = item_key.split(':', 1)[1]
            fuel_type = FuelType.query.get(fuel_type_id)
            if not fuel_type:
                flash('Selected fuel type does not exist.', 'danger')
                return redirect(url_for('pricing.index'))

            db.session.add(FuelPrice(
                fuel_type_id=fuel_type.id,
                price_per_liter=price_val,
                effective_date=effective,
                updated_by=current_user.id
            ))
            db.session.commit()
            flash(f"Updated price for {fuel_type.name} to PKR {price_val:,.2f}/{fuel_type.unit} successfully.", 'success')
            return redirect(url_for('pricing.index'))

        if item_key.startswith('item:'):
            item_id = item_key.split(':', 1)[1]
            shop_item = OtherItem.query.get(item_id)
            if not shop_item:
                flash('Selected inventory item does not exist.', 'danger')
                return redirect(url_for('pricing.index'))

            # Same product = same category + company + type (each has its own price).
            query = OtherItem.query.filter_by(category=shop_item.category)
            if shop_item.company:
                query = query.filter_by(company=shop_item.company)
            else:
                query = query.filter(OtherItem.company.is_(None))
            if shop_item.item_type:
                query = query.filter_by(item_type=shop_item.item_type)
            else:
                query = query.filter(OtherItem.item_type.is_(None))
            if shop_item.category == 'other':
                query = query.filter_by(name=shop_item.name)

            updated = 0
            for item in query.all():
                item.sale_price = price_val
                db.session.add(ItemPriceLog(
                    other_item_id=item.id,
                    sale_price=price_val,
                    cost_price=item.cost_price,
                    updated_by=current_user.id
                ))
                updated += 1

            db.session.commit()
            label = shop_item.display_name()
            flash(
                f"Updated sale price for {label} to PKR {price_val:,.2f} "
                f"({updated} stock row{'s' if updated != 1 else ''}).",
                'success'
            )
            return redirect(url_for('pricing.index'))

        flash('Invalid item selection.', 'danger')
        return redirect(url_for('pricing.index'))

    # GET
    fuel_types = FuelType.query.order_by(FuelType.name.asc()).all()
    shop_items = OtherItem.query.order_by(OtherItem.category.asc(), OtherItem.name.asc()).all()

    current_fuel_prices = {}
    for ft in fuel_types:
        latest = FuelPrice.query.filter_by(fuel_type_id=ft.id).order_by(FuelPrice.created_at.desc()).first()
        current_fuel_prices[ft.id] = float(latest.price_per_liter) if latest else 0.0

    # Unified price history (fuel + shop items)
    price_history = []
    for record in FuelPrice.query.order_by(FuelPrice.created_at.desc()).limit(100).all():
        price_history.append({
            'category': 'fuel',
            'item_name': record.fuel_type.name,
            'price': float(record.price_per_liter),
            'unit': record.fuel_type.unit,
            'updated_by': record.updater.name if record.updater else '-',
            'logged_at': record.created_at,
            'effective_date': record.effective_date,
        })

    for record in ItemPriceLog.query.order_by(ItemPriceLog.created_at.desc()).limit(100).all():
        price_history.append({
            'category': record.item.category if record.item else 'other',
            'item_name': record.item.name if record.item else 'Unknown',
            'price': float(record.sale_price),
            'unit': 'unit',
            'updated_by': record.updater.name if record.updater else '-',
            'logged_at': record.created_at,
            'effective_date': record.created_at.date() if record.created_at else None,
        })

    price_history.sort(key=lambda r: r['logged_at'] or datetime.min, reverse=True)

    return render_template(
        'pricing/index.html',
        fuel_types=fuel_types,
        shop_items=shop_items,
        current_fuel_prices=current_fuel_prices,
        price_history=price_history[:100],
        today=datetime.utcnow().date().isoformat(),
    )
