"""Opening / previous stock (no vendor)."""
from datetime import datetime

from app.models import (
    db, User, FuelType, FuelPrice, Inventory, StockEntry,
    OtherItem, ItemPurchaseLog, ItemPriceLog,
)


def _admin_user_id():
    user = User.query.filter_by(role='admin').order_by(User.id.asc()).first()
    if user is None:
        user = User.query.order_by(User.id.asc()).first()
    return user.id if user else None


def _fuel_by_name(name):
    return (
        FuelType.query
        .filter(db.func.lower(FuelType.name) == name.lower())
        .order_by(FuelType.id.asc())
        .first()
    )


def _set_fuel_opening(name, liters, cost_per_liter, sale_per_liter, user_id, as_of, write_history=True):
    fuel = _fuel_by_name(name)
    if fuel is None:
        fuel = FuelType(name=name, unit='Liter')
        db.session.add(fuel)
        db.session.flush()

    inv = Inventory.query.filter_by(fuel_type_id=fuel.id).first()
    if inv is None:
        inv = Inventory(fuel_type_id=fuel.id, current_stock_liters=0, reorder_threshold=0)
        db.session.add(inv)
        write_history = True
    inv.current_stock_liters = liters

    existing_price = FuelPrice.query.filter_by(
        fuel_type_id=fuel.id,
        effective_date=as_of.date(),
    ).first()
    if existing_price:
        existing_price.price_per_liter = sale_per_liter
        existing_price.updated_by = user_id
    else:
        db.session.add(FuelPrice(
            fuel_type_id=fuel.id,
            price_per_liter=sale_per_liter,
            effective_date=as_of.date(),
            updated_by=user_id,
            created_at=as_of,
        ))

    if write_history:
        db.session.add(StockEntry(
            fuel_type_id=fuel.id,
            liters_added=liters,
            cost_per_liter=cost_per_liter,
            supplier=None,
            vendor_id=None,
            entry_date=as_of,
            added_by=user_id,
        ))
        db.session.add(ItemPurchaseLog(
            category='fuel',
            item_name=fuel.name,
            vendor=None,
            vendor_id=None,
            cost_price=cost_per_liter,
            sale_price=sale_per_liter,
            liters=liters,
            fuel_type_id=fuel.id,
            entry_date=as_of,
            added_by=user_id,
        ))
    return fuel


def _upsert_shop_item(
    *,
    category,
    name,
    company,
    item_type,
    quantity,
    liters,
    cost_price,
    sale_price,
    user_id,
    as_of,
    write_history=True,
):
    query = OtherItem.query.filter_by(category=category, name=name)
    if company:
        query = query.filter_by(company=company)
    else:
        query = query.filter(OtherItem.company.is_(None))
    if item_type:
        query = query.filter_by(item_type=item_type)
    else:
        query = query.filter(OtherItem.item_type.is_(None))
    item = query.first()
    created = item is None

    if item is None:
        item = OtherItem(
            category=category,
            name=name,
            company=company,
            item_type=item_type,
            vendor=None,
            cost_price=cost_price,
            sale_price=sale_price,
            liters=liters,
            quantity=quantity if category != 'ft_mobile' else 0,
        )
        db.session.add(item)
        db.session.flush()
    else:
        item.cost_price = cost_price
        item.sale_price = sale_price
        item.vendor = None
        if category == 'ft_mobile':
            item.liters = liters
            item.quantity = 0
        else:
            item.quantity = quantity
            if liters is not None:
                item.liters = liters

    if write_history or created:
        db.session.add(ItemPriceLog(
            other_item_id=item.id,
            sale_price=sale_price,
            cost_price=cost_price,
            effective_date=as_of.date(),
            updated_by=user_id,
            created_at=as_of,
        ))
        db.session.add(ItemPurchaseLog(
            category=category,
            item_name=name,
            company=company,
            item_type=item_type,
            vendor=None,
            vendor_id=None,
            cost_price=cost_price,
            sale_price=sale_price,
            quantity=None if category == 'ft_mobile' else quantity,
            liters=liters,
            entry_date=as_of,
            added_by=user_id,
        ))
    return item


def seed_opening_stock(force=False):
    """
    One-time insert of previous station stock (no vendor / no payable).

    force=True overwrites fuel liters + shop qty/prices.
    """
    user_id = _admin_user_id()
    if not user_id:
        print('Opening stock seed skipped: no users in database.')
        return False

    as_of = datetime.utcnow()
    # First run (empty shop) also writes purchase/stock history rows
    write_history = OtherItem.query.count() == 0

    _set_fuel_opening('Diesel', 1200.0, 393.0, 404.30, user_id, as_of, write_history=write_history)
    _set_fuel_opening('Petrol', 818.0, 381.0, 390.50, user_id, as_of, write_history=write_history)

    shop_kwargs = dict(user_id=user_id, as_of=as_of, write_history=write_history)

    # FT Mobile Oil — 95 1/4 L = 95.25 L
    _upsert_shop_item(
        category='ft_mobile',
        name='FT Mobile Oil',
        company='FT Mobile Oil',
        item_type=None,
        quantity=0,
        liters=95.25,
        cost_price=450.0,
        sale_price=600.0,
        **shop_kwargs,
    )

    _upsert_shop_item(
        category='mobile',
        name='Caltex Havoline',
        company='Caltex',
        item_type='Havoline',
        quantity=11,
        liters=0.75,
        cost_price=741.0,
        sale_price=850.0,
        **shop_kwargs,
    )

    _upsert_shop_item(
        category='mobile',
        name='Caltex Delo 20-50',
        company='Caltex',
        item_type='Delo 20-50',
        quantity=6,
        liters=4.0,
        cost_price=4600.0,
        sale_price=5000.0,
        **shop_kwargs,
    )

    _upsert_shop_item(
        category='filter',
        name='Filter 480',
        company='Guard',
        item_type='Oil Filter',
        quantity=5,
        liters=None,
        cost_price=550.0,
        sale_price=950.0,
        **shop_kwargs,
    )
    _upsert_shop_item(
        category='filter',
        name='Filter 385',
        company='Guard',
        item_type='Oil Filter',
        quantity=4,
        liters=None,
        cost_price=540.0,
        sale_price=600.0,
        **shop_kwargs,
    )

    _upsert_shop_item(
        category='other',
        name='Greece',
        company=None,
        item_type=None,
        quantity=21,
        liters=None,
        cost_price=170.0,
        sale_price=220.0,
        **shop_kwargs,
    )

    db.session.commit()
    print('Opening stock written to database (no vendor).')
    print('  Diesel 1200 L @ cost 393 / sale 404.30')
    print('  Petrol 818 L @ cost 381 / sale 390.50')
    print('  FT Mobile Oil 95.25 L @ cost 450 / sale 600')
    print('  Caltex Havoline x11 @ 741 / 850 (0.75 L)')
    print('  Caltex Delo 20-50 x6 @ 4600 / 5000 (4 L)')
    print('  Filter 480 x5 @ 550 / 950')
    print('  Filter 385 x4 @ 540 / 600')
    print('  Greece x21 @ 170 / 220')
    return True


def ensure_opening_stock_seed():
    """Deprecated auto-hook — opening stock is one-time via scripts/seed_opening_stock.py."""
    return False
