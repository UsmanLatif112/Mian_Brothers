"""Daily inventory statement per fuel tank or shop/FT item."""
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func

from app.models import (
    db, FuelType, Inventory, OtherItem, ItemPurchaseLog, StockEntry,
    MeterReading, CreditSale,
)
from app.tenancy import apply_agency_filter
from app.vendors.service import is_opening_stock_purchase, is_opening_vendor_name


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def product_choices():
    """Dropdown options: fuels first, then shop items."""
    choices = []
    fuels = (
        apply_agency_filter(FuelType.query, FuelType, include_shared=True)
        .order_by(FuelType.name.asc())
        .all()
    )
    for ft in fuels:
        choices.append({
            'key': f'fuel:{ft.id}',
            'label': ft.name,
            'kind': 'fuel',
            'unit': 'L',
            'id': ft.id,
        })
    items = (
        apply_agency_filter(OtherItem.query, OtherItem)
        .order_by(OtherItem.category.asc(), OtherItem.name.asc())
        .all()
    )
    for item in items:
        unit = 'L' if item.tracks_liters() else 'pcs'
        choices.append({
            'key': f'item:{item.id}',
            'label': item.display_name(),
            'kind': 'item',
            'unit': unit,
            'id': item.id,
        })
    return choices


def resolve_product(product_key):
    if not product_key or ':' not in product_key:
        return None
    kind, raw_id = product_key.split(':', 1)
    try:
        pid = int(raw_id)
    except (TypeError, ValueError):
        return None
    if kind == 'fuel':
        ft = apply_agency_filter(
            FuelType.query.filter_by(id=pid), FuelType, include_shared=True
        ).first()
        if not ft:
            return None
        return {'key': product_key, 'kind': 'fuel', 'id': ft.id, 'name': ft.name, 'unit': 'L'}
    if kind == 'item':
        item = apply_agency_filter(OtherItem.query.filter_by(id=pid), OtherItem).first()
        if not item:
            return None
        return {
            'key': product_key,
            'kind': 'item',
            'id': item.id,
            'name': item.display_name(),
            'unit': 'L' if item.tracks_liters() else 'pcs',
            'item': item,
        }
    return None


def _live_stock(product):
    if product['kind'] == 'fuel':
        inv = apply_agency_filter(
            Inventory.query.filter_by(fuel_type_id=product['id']), Inventory
        ).first()
        return float(inv.current_stock_liters or 0) if inv else 0.0
    item = product.get('item') or OtherItem.query.get(product['id'])
    if not item:
        return 0.0
    if item.tracks_liters():
        return float(item.liters or 0)
    return float(item.quantity or 0)


def _fuel_movements(fuel_type_id, start, end):
    """Purchases / opening-stock / meter sales / credit sales by day for a fuel tank."""
    purchases_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0})
    opening_in_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0})
    sales_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0})
    credit_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0, 'credit': 0.0})

    # Purchase logs (preferred) + stock entries fallback
    logs = apply_agency_filter(
        ItemPurchaseLog.query.filter(
            ItemPurchaseLog.fuel_type_id == fuel_type_id,
            ItemPurchaseLog.category == 'fuel',
            func.date(ItemPurchaseLog.entry_date) >= start,
            func.date(ItemPurchaseLog.entry_date) <= end,
        ),
        ItemPurchaseLog,
    ).all()
    log_days = set()
    for log in logs:
        day = _as_date(log.entry_date)
        qty = float(log.liters or 0)
        amt = qty * float(log.cost_price or 0)
        bucket = opening_in_by_day if is_opening_stock_purchase(log) else purchases_by_day
        bucket[day]['qty'] += qty
        bucket[day]['amount'] += amt
        log_days.add(day)

    entries = apply_agency_filter(
        StockEntry.query.filter(
            StockEntry.fuel_type_id == fuel_type_id,
            func.date(StockEntry.entry_date) >= start,
            func.date(StockEntry.entry_date) <= end,
        ),
        StockEntry,
    ).all()
    for se in entries:
        day = _as_date(se.entry_date)
        if day in log_days:
            continue  # already counted via purchase log
        qty = float(se.liters_added or 0)
        amt = qty * float(se.cost_per_liter or 0)
        # StockEntry.supplier may be the opening vendor name
        if is_opening_vendor_name(getattr(se, 'supplier', None)):
            opening_in_by_day[day]['qty'] += qty
            opening_in_by_day[day]['amount'] += amt
        else:
            purchases_by_day[day]['qty'] += qty
            purchases_by_day[day]['amount'] += amt

    meters = apply_agency_filter(
        MeterReading.query.filter(
            MeterReading.fuel_type_id == fuel_type_id,
            MeterReading.reading_date >= start,
            MeterReading.reading_date <= end,
            MeterReading.liters_sold.isnot(None),
        ),
        MeterReading,
    ).all()
    for m in meters:
        day = _as_date(m.reading_date)
        qty = float(m.liters_sold or 0)
        rate = float(m.sale_rate or 0)
        sales_by_day[day]['qty'] += qty
        sales_by_day[day]['amount'] += qty * rate

    credits = apply_agency_filter(
        CreditSale.query.filter(
            CreditSale.fuel_type_id == fuel_type_id,
            CreditSale.other_item_id.is_(None),
            CreditSale.entry_type == 'sale',
            CreditSale.sale_date >= start,
            CreditSale.sale_date <= end,
        ),
        CreditSale,
    ).all()
    for cs in credits:
        day = _as_date(cs.sale_date)
        qty = float(cs.liters or 0)
        amt = float(cs.amount or 0)
        credit = max(amt - float(cs.amount_paid or 0), 0.0)
        credit_by_day[day]['qty'] += qty
        credit_by_day[day]['amount'] += amt
        credit_by_day[day]['credit'] += credit

    return purchases_by_day, opening_in_by_day, sales_by_day, credit_by_day


def _item_movements(item, start, end):
    """Purchases / opening-stock / sales / credit by day for a shop or FT item."""
    purchases_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0})
    opening_in_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0})
    sales_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0})
    credit_by_day = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0, 'credit': 0.0})
    tracks_l = item.tracks_liters()

    logs = apply_agency_filter(
        ItemPurchaseLog.query.filter(
            ItemPurchaseLog.category == (item.category or 'other'),
            ItemPurchaseLog.item_name == item.name,
            func.date(ItemPurchaseLog.entry_date) >= start,
            func.date(ItemPurchaseLog.entry_date) <= end,
        ),
        ItemPurchaseLog,
    )
    if item.company is not None:
        logs = logs.filter(ItemPurchaseLog.company == item.company)
    if item.item_type is not None:
        logs = logs.filter(ItemPurchaseLog.item_type == item.item_type)
    for log in logs.all():
        day = _as_date(log.entry_date)
        if tracks_l:
            qty = float(log.liters or 0)
        else:
            qty = float(log.quantity or 0)
        amt = qty * float(log.cost_price or 0)
        bucket = opening_in_by_day if is_opening_stock_purchase(log) else purchases_by_day
        bucket[day]['qty'] += qty
        bucket[day]['amount'] += amt

    sales = apply_agency_filter(
        CreditSale.query.filter(
            CreditSale.other_item_id == item.id,
            CreditSale.entry_type == 'sale',
            CreditSale.sale_date >= start,
            CreditSale.sale_date <= end,
        ),
        CreditSale,
    ).all()
    for cs in sales:
        day = _as_date(cs.sale_date)
        qty = float(cs.liters or 0)
        amt = float(cs.amount or 0)
        paid = float(cs.amount_paid or 0)
        credit = max(amt - paid, 0.0)
        sales_by_day[day]['qty'] += qty
        sales_by_day[day]['amount'] += amt
        credit_by_day[day]['qty'] += qty if credit > 0 else 0.0
        credit_by_day[day]['amount'] += amt
        credit_by_day[day]['credit'] += credit

    return purchases_by_day, opening_in_by_day, sales_by_day, credit_by_day


def _movements_through(product, start, end):
    if product['kind'] == 'fuel':
        return _fuel_movements(product['id'], start, end)
    item = product.get('item') or OtherItem.query.get(product['id'])
    return _item_movements(item, start, end)


def _qty_after(product, from_day):
    """Net stock change from from_day (inclusive) through far future → used to back into opening."""
    far = from_day + timedelta(days=36500)
    purchases, opening_in, sales, _ = _movements_through(product, from_day, far)
    stock_in = (
        sum(v['qty'] for v in purchases.values())
        + sum(v['qty'] for v in opening_in.values())
    )
    sold = sum(v['qty'] for v in sales.values())
    return stock_in - sold


def build_stock_log(product, start, end):
    """
    Daily rows for one product in [start, end].

    \"Open for previous\" stock is treated as opening (not purchase).
    """
    if not product:
        return [], {}

    live = _live_stock(product)
    # Opening at start = live − (stock in − sales) from start onward
    net_from_start = _qty_after(product, start)
    opening = live - net_from_start

    purchases, opening_in, sales, credits = _movements_through(product, start, end)

    rows = []
    running = opening
    totals = {
        'purchase_qty': 0.0,
        'sale_qty': 0.0,
        'purchase_amount': 0.0,
        'sale_amount': 0.0,
        'credit_amount': 0.0,
        'opening': opening,
        'closing': opening,
        'opening_stock_in': 0.0,
    }

    # Walk every day in range so stock-for-date is continuous
    day = start
    while day <= end:
        p = purchases.get(day, {'qty': 0.0, 'amount': 0.0})
        o = opening_in.get(day, {'qty': 0.0, 'amount': 0.0})
        s = sales.get(day, {'qty': 0.0, 'amount': 0.0})
        c = credits.get(day, {'qty': 0.0, 'amount': 0.0, 'credit': 0.0})
        has_move = (
            abs(p['qty']) > 1e-9 or abs(o['qty']) > 1e-9 or abs(s['qty']) > 1e-9
            or abs(c['credit']) > 1e-9 or abs(c['amount']) > 1e-9
        )
        # Opening-vendor stock folds into opening for the day (not purchase)
        day_opening = running + o['qty']
        day_closing = day_opening + p['qty'] - s['qty']
        running = day_closing

        if has_move:
            sale_amount = s['amount']
            rows.append(SimpleNamespace(
                name=product['name'],
                date=day,
                stock_opening=day_opening,
                stock=day_closing,
                purchase_qty=p['qty'],
                sale_qty=s['qty'],
                purchase_amount=p['amount'],
                sale_amount=sale_amount,
                credit_amount=c['credit'],
                credit_sale_amount=c['amount'],
                opening_stock_in=o['qty'],
                unit=product['unit'],
            ))
            totals['purchase_qty'] += p['qty']
            totals['sale_qty'] += s['qty']
            totals['purchase_amount'] += p['amount']
            totals['sale_amount'] += sale_amount
            totals['credit_amount'] += c['credit']
            totals['opening_stock_in'] += o['qty']

        day += timedelta(days=1)

    totals['closing'] = running
    # Newest first for reading recent activity
    rows.reverse()
    return rows, totals

