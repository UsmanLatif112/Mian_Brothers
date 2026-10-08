"""Lightweight series builders for listing (line) and ledger (pie) charts."""

from collections import defaultdict
from datetime import datetime, timedelta


def _as_date(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if hasattr(val, 'year') and hasattr(val, 'month') and hasattr(val, 'day') and not hasattr(val, 'hour'):
        return val
    if isinstance(val, str):
        try:
            return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def daily_labels(days=14):
    today = datetime.utcnow().date()
    start = today - timedelta(days=days - 1)
    labels = []
    dates = []
    cursor = start
    while cursor <= today:
        labels.append(cursor.strftime('%b %d'))
        dates.append(cursor)
        cursor += timedelta(days=1)
    return labels, dates, start, today


def fill_daily(dates, bucket):
    return [round(float(bucket.get(d, 0) or 0), 2) for d in dates]


def customers_listing_series():
    """Line: daily credit sales vs payments (last 14 days)."""
    from app.models import CreditSale, Payment

    labels, dates, start, end = daily_labels(14)
    sales = defaultdict(float)
    pays = defaultdict(float)

    for e in CreditSale.query.filter(
        CreditSale.sale_date >= start,
        CreditSale.sale_date <= end,
        CreditSale.entry_type == 'sale',
    ).all():
        d = _as_date(e.sale_date)
        if d:
            sales[d] += float(e.amount or 0)

    for p in Payment.query.filter(
        Payment.payment_date >= datetime.combine(start, datetime.min.time()),
        Payment.payment_date <= datetime.combine(end, datetime.max.time()),
    ).all():
        d = _as_date(p.payment_date)
        if d:
            pays[d] += float(p.amount_paid or 0)

    return {
        'labels': labels,
        'datasets': [
            {'label': 'Credit sales', 'data': fill_daily(dates, sales), 'borderColor': '#e11d48'},
            {'label': 'Payments in', 'data': fill_daily(dates, pays), 'borderColor': '#008060'},
        ],
    }


def vendors_listing_series():
    """Line: daily purchases vs vendor payments (last 14 days)."""
    from app.models import ItemPurchaseLog, VendorPayment
    from app.vendors.service import purchase_log_total

    labels, dates, start, end = daily_labels(14)
    buys = defaultdict(float)
    pays = defaultdict(float)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    for log in ItemPurchaseLog.query.filter(
        ItemPurchaseLog.entry_date >= start_dt,
        ItemPurchaseLog.entry_date <= end_dt,
    ).all():
        d = _as_date(log.entry_date)
        if d:
            buys[d] += float(purchase_log_total(log) or 0)

    for p in VendorPayment.query.filter(
        VendorPayment.payment_date >= start_dt,
        VendorPayment.payment_date <= end_dt,
    ).all():
        d = _as_date(p.payment_date)
        if d:
            pays[d] += float(p.amount_paid or 0)

    return {
        'labels': labels,
        'datasets': [
            {'label': 'Purchases', 'data': fill_daily(dates, buys), 'borderColor': '#f59e0b'},
            {'label': 'Payments out', 'data': fill_daily(dates, pays), 'borderColor': '#0891b2'},
        ],
    }


def sales_listing_series(stats):
    """Line from period stats entries — daily sale amounts."""
    sales = defaultdict(float)
    for e in stats.get('entries') or []:
        et = (getattr(e, 'entry_type', None) or 'sale').lower()
        if et != 'sale':
            continue
        d = _as_date(getattr(e, 'sale_date', None))
        if d:
            sales[d] += float(getattr(e, 'amount', 0) or 0)

    if not sales:
        labels, dates, _, _ = daily_labels(7)
        return {
            'labels': labels,
            'datasets': [{'label': 'Sales', 'data': fill_daily(dates, {}), 'borderColor': '#008060'}],
        }

    dates = sorted(sales.keys())
    labels = [d.strftime('%b %d') for d in dates]
    return {
        'labels': labels,
        'datasets': [{'label': 'Sales', 'data': fill_daily(dates, sales), 'borderColor': '#008060'}],
    }


def expenses_listing_series(expenses):
    bucket = defaultdict(float)
    for e in expenses or []:
        d = _as_date(getattr(e, 'expense_date', None))
        if d:
            bucket[d] += float(getattr(e, 'amount', 0) or 0)
    if not bucket:
        labels, dates, _, _ = daily_labels(7)
        return {
            'labels': labels,
            'datasets': [{'label': 'Expenses', 'data': fill_daily(dates, {}), 'borderColor': '#e11d48'}],
        }
    dates = sorted(bucket.keys())
    return {
        'labels': [d.strftime('%b %d') for d in dates],
        'datasets': [{'label': 'Expenses', 'data': fill_daily(dates, bucket), 'borderColor': '#e11d48'}],
    }


def journal_listing_series(rows):
    ins = defaultdict(float)
    outs = defaultdict(float)
    for row in rows or []:
        d = _as_date(getattr(row, 'sale_date', None))
        if not d:
            continue
        amt = float(getattr(row, 'amount', 0) or 0)
        if getattr(row, 'cash_direction', '') == 'in':
            ins[d] += amt
        else:
            outs[d] += amt
    dates = sorted(set(ins) | set(outs))
    if not dates:
        labels, dates, _, _ = daily_labels(7)
        return {
            'labels': labels,
            'datasets': [
                {'label': 'Cash in', 'data': fill_daily(dates, {}), 'borderColor': '#008060'},
                {'label': 'Cash out', 'data': fill_daily(dates, {}), 'borderColor': '#e11d48'},
            ],
        }
    return {
        'labels': [d.strftime('%b %d') for d in dates],
        'datasets': [
            {'label': 'Cash in', 'data': fill_daily(dates, ins), 'borderColor': '#008060'},
            {'label': 'Cash out', 'data': fill_daily(dates, outs), 'borderColor': '#e11d48'},
        ],
    }


def inventory_listing_series():
    """Line: daily purchase liters/qty last 14 days."""
    from app.models import ItemPurchaseLog

    labels, dates, start, end = daily_labels(14)
    fuel = defaultdict(float)
    shop = defaultdict(float)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())
    for log in ItemPurchaseLog.query.filter(
        ItemPurchaseLog.entry_date >= start_dt,
        ItemPurchaseLog.entry_date <= end_dt,
    ).all():
        d = _as_date(log.entry_date)
        if not d:
            continue
        if log.category in ('fuel', 'ft_mobile'):
            fuel[d] += float(log.liters or 0)
        else:
            shop[d] += float(log.quantity or 0)
    return {
        'labels': labels,
        'datasets': [
            {'label': 'Fuel / FT L', 'data': fill_daily(dates, fuel), 'borderColor': '#0891b2'},
            {'label': 'Shop qty', 'data': fill_daily(dates, shop), 'borderColor': '#f59e0b'},
        ],
    }


def customer_ledger_pie(ledger_entries):
    """Pie: sales paid / unpaid, payments, advance, loan."""
    buckets = defaultdict(float)
    for e in ledger_entries or []:
        kind = (e.get('entry_kind') or e.get('pay_type') or '').lower()
        etype = (e.get('type') or '').lower()
        debit = float(e.get('debit') or 0)
        credit = float(e.get('credit') or 0)
        amount = float(e.get('amount') or 0)
        paid = float(e.get('amount_paid') or 0)

        if kind == 'advance' or (etype == 'payment' and kind == 'advance'):
            buckets['Advance'] += credit or amount or debit
        elif kind == 'loan':
            buckets['Loan'] += debit or amount
        elif kind == 'opening':
            buckets['Opening'] += debit or amount
        elif etype == 'payment' or kind == 'payment':
            buckets['Paid'] += credit or amount or paid
        else:
            unpaid = max(debit, amount - paid, 0)
            if paid > 0:
                buckets['Paid'] += paid
            if unpaid > 0:
                buckets['Unpaid'] += unpaid
            elif debit > 0 and paid <= 0:
                buckets['Unpaid'] += debit
    pairs = sorted(((k, v) for k, v in buckets.items() if v > 0), key=lambda x: -x[1])
    return {
        'labels': [p[0] for p in pairs],
        'values': [round(p[1], 2) for p in pairs],
    }


def vendor_ledger_pie(total_purchased, total_paid, balance):
    """Pie: purchased paid / unpaid (still payable)."""
    purchased = max(float(total_purchased or 0), 0)
    paid = max(float(total_paid or 0), 0)
    due = max(float(balance or 0), 0)
    labels, values = [], []
    if paid:
        labels.append('Paid')
        values.append(round(paid, 2))
    if due:
        labels.append('Unpaid')
        values.append(round(due, 2))
    if not labels and purchased:
        labels = ['Purchased']
        values = [round(purchased, 2)]
    return {'labels': labels, 'values': values}
