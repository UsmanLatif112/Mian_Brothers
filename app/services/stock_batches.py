"""FIFO stock batches for fuel tanks and shop/FT items."""
from datetime import datetime

from app.models import db, StockBatch, StockBatchConsumption, ItemPurchaseLog, OtherItem, Inventory
from app.tenancy import stamp_agency, apply_agency_filter


SOURCE_METER = 'meter_reading'
SOURCE_CREDIT = 'credit_sale'


class BatchStockError(ValueError):
    """Raised when FIFO cannot cover requested qty."""


def _as_dt(value):
    if value is None:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


def _qty_from_log(log):
    liters = float(log.liters or 0) if log.liters is not None else 0.0
    qty = int(log.quantity or 0) if log.quantity is not None else 0
    if (log.category or '') in ('fuel', 'ft_mobile') or (liters > 0 and qty <= 0):
        return liters
    return float(qty)


def create_batch_from_purchase_log(log, other_item_id=None):
    """Create a FIFO batch for a purchase log. Idempotent if one already exists."""
    if not log or not getattr(log, 'id', None):
        return None
    if (log.category or '') == 'expense_settle':
        return None
    existing = StockBatch.query.filter_by(purchase_log_id=log.id).first()
    if existing:
        return existing

    qty = _qty_from_log(log)
    if qty <= 0:
        return None

    fuel_type_id = log.fuel_type_id if (log.category or '') == 'fuel' else None
    item_id = other_item_id
    if item_id is None and fuel_type_id is None:
        item = _resolve_shop_item(log)
        item_id = item.id if item else None
    if fuel_type_id is None and item_id is None:
        return None

    batch = stamp_agency(StockBatch(
        fuel_type_id=fuel_type_id,
        other_item_id=item_id,
        purchase_log_id=log.id,
        qty_received=qty,
        qty_remaining=qty,
        unit_cost=float(log.cost_price or 0),
        received_at=_as_dt(log.entry_date),
    ))
    db.session.add(batch)
    db.session.flush()
    return batch


def _resolve_shop_item(log):
    from app.vendors.service import _shop_item_for_log
    return _shop_item_for_log(log)


def adjust_batch_for_purchase_log(log, old_qty, new_qty, unit_cost=None):
    """Resize the linked batch when purchase qty/cost changes."""
    batch = StockBatch.query.filter_by(purchase_log_id=log.id).first() if log and log.id else None
    if batch is None:
        return create_batch_from_purchase_log(log)

    old_qty = float(old_qty or 0)
    new_qty = float(new_qty or 0)
    delta = new_qty - old_qty
    consumed = float(batch.qty_received or 0) - float(batch.qty_remaining or 0)
    batch.qty_received = new_qty
    batch.qty_remaining = max(0.0, new_qty - consumed)
    if unit_cost is not None:
        batch.unit_cost = float(unit_cost)
    if delta != 0:
        # Prefer adjusting remaining by delta when not over-consumed
        batch.qty_remaining = max(0.0, float(batch.qty_remaining or 0) + delta)
        if float(batch.qty_remaining) > float(batch.qty_received):
            batch.qty_remaining = float(batch.qty_received)
    db.session.flush()
    return batch


def delete_batch_for_purchase_log(log):
    """Remove batch for a deleted purchase (restores remaining into void — stock reversed separately)."""
    if not log or not log.id:
        return
    batch = StockBatch.query.filter_by(purchase_log_id=log.id).first()
    if not batch:
        return
    for row in list(batch.consumptions or []):
        db.session.delete(row)
    db.session.delete(batch)
    db.session.flush()


def reverse_consumptions(source_type, source_id):
    """Put consumed qty back onto batches for a sale/meter row. Returns qty restored."""
    rows = StockBatchConsumption.query.filter_by(
        source_type=source_type, source_id=int(source_id),
    ).all()
    restored = 0.0
    for row in rows:
        take = float(row.qty_consumed or 0)
        restored += take
        batch = StockBatch.query.get(row.stock_batch_id)
        if batch:
            batch.qty_remaining = float(batch.qty_remaining or 0) + take
            if float(batch.qty_remaining) > float(batch.qty_received or 0):
                batch.qty_remaining = float(batch.qty_received or 0)
        db.session.delete(row)
    db.session.flush()
    return restored


def _open_batches(fuel_type_id=None, other_item_id=None):
    q = StockBatch.query.filter(StockBatch.qty_remaining > 0)
    if fuel_type_id:
        q = q.filter_by(fuel_type_id=fuel_type_id)
    elif other_item_id:
        q = q.filter_by(other_item_id=other_item_id)
    else:
        return []
    q = apply_agency_filter(q, StockBatch)
    return q.order_by(StockBatch.received_at.asc(), StockBatch.id.asc()).all()


def consume_fifo(*, qty, source_type, source_id, fuel_type_id=None, other_item_id=None, allow_short=False):
    """
    Consume qty from oldest batches. Returns total COGS.
    Raises BatchStockError if not enough remaining (unless allow_short).
    """
    need = float(qty or 0)
    if need <= 0:
        return 0.0

    reverse_consumptions(source_type, source_id)

    batches = _open_batches(fuel_type_id=fuel_type_id, other_item_id=other_item_id)
    available = sum(float(b.qty_remaining or 0) for b in batches)
    if available + 1e-9 < need and not allow_short:
        label = 'fuel' if fuel_type_id else 'item'
        raise BatchStockError(
            f'Insufficient FIFO batch stock for {label}. '
            f'Need {need:.3f}, batch remaining {available:.3f}.'
        )

    left = need
    cogs = 0.0
    for batch in batches:
        if left <= 1e-12:
            break
        rem = float(batch.qty_remaining or 0)
        if rem <= 0:
            continue
        take = min(rem, left)
        unit = float(batch.unit_cost or 0)
        amount = round(take * unit, 2)
        row = stamp_agency(StockBatchConsumption(
            stock_batch_id=batch.id,
            qty_consumed=take,
            unit_cost=unit,
            cogs_amount=amount,
            source_type=source_type,
            source_id=int(source_id),
        ))
        db.session.add(row)
        batch.qty_remaining = rem - take
        cogs += amount
        left -= take

    if left > 1e-6 and allow_short:
        # No batch left — COGS only from what we had
        pass
    elif left > 1e-6:
        raise BatchStockError(f'FIFO short by {left:.3f}.')

    db.session.flush()
    return round(cogs, 2)


def list_batches_for_fuel(fuel_type_id, include_empty=False):
    q = apply_agency_filter(
        StockBatch.query.filter_by(fuel_type_id=fuel_type_id), StockBatch
    )
    if not include_empty:
        q = q.filter(StockBatch.qty_remaining > 0)
    return q.order_by(StockBatch.received_at.asc(), StockBatch.id.asc()).all()


def list_batches_for_item(other_item_id, include_empty=False):
    q = apply_agency_filter(
        StockBatch.query.filter_by(other_item_id=other_item_id), StockBatch
    )
    if not include_empty:
        q = q.filter(StockBatch.qty_remaining > 0)
    return q.order_by(StockBatch.received_at.asc(), StockBatch.id.asc()).all()


def repair_fuel_credit_double_stock():
    """
    One-time: fuel CreditSales wrongly deducted tank + FIFO (meter already owns stock).
    Restore liters to Inventory, reverse credit_sale consumptions, clear cogs_total.
    Idempotent — skips rows with no FIFO layers and no cogs_total.
    Returns (sale_count, {fuel_type_id: liters_restored}).
    """
    from app.models import CreditSale

    sales = CreditSale.query.filter(
        CreditSale.entry_type == 'sale',
        CreditSale.fuel_type_id.isnot(None),
        CreditSale.other_item_id.is_(None),
    ).order_by(CreditSale.id.asc()).all()

    restored_by_fuel = {}
    count = 0
    for cs in sales:
        cons = StockBatchConsumption.query.filter_by(
            source_type=SOURCE_CREDIT, source_id=cs.id,
        ).all()
        if not cons and cs.cogs_total is None:
            continue

        qty = float(cs.liters or 0)
        reverse_consumptions(SOURCE_CREDIT, cs.id)
        # Live tank was always reduced by full sale liters when the bug ran
        restore_qty = qty if qty > 0 else sum(float(c.qty_consumed or 0) for c in cons)

        inv = None
        if cs.agency_id is not None:
            inv = Inventory.query.filter_by(
                fuel_type_id=cs.fuel_type_id, agency_id=cs.agency_id,
            ).first()
        if inv is None:
            inv = Inventory.query.filter_by(fuel_type_id=cs.fuel_type_id).first()

        if inv and restore_qty > 0:
            inv.current_stock_liters = float(inv.current_stock_liters or 0) + restore_qty
            fid = cs.fuel_type_id
            restored_by_fuel[fid] = restored_by_fuel.get(fid, 0.0) + restore_qty

        cs.cogs_total = None
        count += 1

    db.session.commit()
    return count, restored_by_fuel


def backfill_batches_from_purchases():
    """
    One-time: create batches from purchase logs, then trim remaining
    so sum(remaining) matches live Inventory / OtherItem stock (FIFO oldest kept).
    """
    from app.vendors.service import is_expense_settle_purchase

    created = 0
    logs = ItemPurchaseLog.query.order_by(ItemPurchaseLog.entry_date.asc(), ItemPurchaseLog.id.asc()).all()
    for log in logs:
        if is_expense_settle_purchase(log):
            continue
        if StockBatch.query.filter_by(purchase_log_id=log.id).first():
            continue
        item = None
        if (log.category or '') != 'fuel':
            item = _resolve_shop_item(log)
        batch = create_batch_from_purchase_log(log, other_item_id=item.id if item else None)
        if batch:
            created += 1

    # Trim fuel tanks to live stock
    for inv in Inventory.query.all():
        _trim_batches_to_stock(
            fuel_type_id=inv.fuel_type_id,
            target=float(inv.current_stock_liters or 0),
        )

    for item in OtherItem.query.all():
        if item.tracks_liters():
            target = float(item.liters or 0)
        else:
            target = float(item.quantity or 0)
        _trim_batches_to_stock(other_item_id=item.id, target=target)

    db.session.flush()
    return created


def _trim_batches_to_stock(fuel_type_id=None, other_item_id=None, target=0.0):
    """Keep oldest remaining totaling `target`; zero out newer excess."""
    q = StockBatch.query
    if fuel_type_id:
        q = q.filter_by(fuel_type_id=fuel_type_id)
    else:
        q = q.filter_by(other_item_id=other_item_id)
    batches = q.order_by(StockBatch.received_at.asc(), StockBatch.id.asc()).all()
    # Reset remaining to received, then keep only `target` from the end (newest unused)
    # Actually: sold stock came from oldest first, so remaining should be the *newest* units.
    total_received = sum(float(b.qty_received or 0) for b in batches)
    sold = max(0.0, total_received - float(target or 0))
    left_to_mark_sold = sold
    for batch in batches:
        recv = float(batch.qty_received or 0)
        if left_to_mark_sold <= 0:
            batch.qty_remaining = recv
            continue
        if left_to_mark_sold >= recv:
            batch.qty_remaining = 0
            left_to_mark_sold -= recv
        else:
            batch.qty_remaining = recv - left_to_mark_sold
            left_to_mark_sold = 0
