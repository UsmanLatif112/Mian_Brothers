"""Cash journal helpers — IN/OUT cash movements."""

from types import SimpleNamespace

# Journal cash rules (business):
#   OUT — inventory purchase (paid/unpaid), vendor payment, vendor advance,
#         customer loan, expense, cash taken
#   IN  — sale (paid/unpaid), customer settle/payment, customer advance, expense settle
# Excluded — openings
JOURNAL_DIRECTION = {
    'diesel_cash': 'in',
    'petrol_cash': 'in',
    'diesel_credit': 'credit',
    'petrol_credit': 'credit',
    'other_credit': 'credit',
    'other_cash': 'in',
    'payment': 'in',
    'advance': 'in',
    'overpay': 'in',
    'loan': 'out',
    'expense': 'out',
    'purchase': 'out',
    'vendor_pay': 'out',
    'vendor_advance': 'out',
}

JOURNAL_TYPE_LABELS = {
    'diesel_cash': 'Total diesel cash sale',
    'petrol_cash': 'Total petrol cash sale',
    'diesel_credit': 'Diesel credit sale',
    'petrol_credit': 'Petrol credit sale',
    'other_credit': 'Other credit sale',
    'other_cash': 'Other cash sale',
    'payment': 'Payment received',
    'advance': 'Payment received',
    'overpay': 'Payment received',
    'loan': 'Payment to customer',
    'expense': 'Expense',
    'purchase': 'Inventory purchase',
    'vendor_pay': 'Payment to vendor',
    'vendor_advance': 'Vendor advance',
}

TYPE_FILTER_CHOICES = (
    ('all', 'All types'),
    ('diesel_cash', 'Diesel cash sale'),
    ('petrol_cash', 'Petrol cash sale'),
    ('diesel_credit', 'Diesel credit sales'),
    ('petrol_credit', 'Petrol credit sales'),
    ('other_credit', 'Other credit sale'),
    ('other_cash', 'Other cash sales'),
    ('expense', 'Expense'),
    ('purchase', 'Inventory purchase'),
    ('loan', 'Payment to customer'),
    ('payment', 'Payment received'),
    ('vendor_pay', 'Payment to vendor'),
    ('vendor_advance', 'Vendor advance'),
)

FUEL_CASH_TOTALS = ('diesel_cash', 'petrol_cash')

DIRECTION_FILTER_CHOICES = (
    ('all', 'All flows'),
    ('in', 'Cash In'),
    ('out', 'Cash Out'),
)


def _row_cash_amount(row):
    """Signed cash amount for the till (positive = in, negative = out)."""
    et = (getattr(row, 'entry_type', None) or '').lower()
    direction = JOURNAL_DIRECTION.get(et)
    if not direction:
        return 0.0
    if et in ('diesel_credit', 'petrol_credit', 'other_credit'):
        return 0.0
    if et in ('payment', 'advance', 'overpay', 'diesel_cash', 'petrol_cash', 'other_cash'):
        amt = float(getattr(row, 'amount_paid', 0) or getattr(row, 'amount', 0) or 0)
    elif et == 'purchase':
        amt = float(getattr(row, 'amount', 0) or getattr(row, 'amount_paid', 0) or 0)
    else:
        amt = float(getattr(row, 'amount', 0) or getattr(row, 'amount_paid', 0) or 0)
    return amt if direction == 'in' else -amt


def build_cash_flow_rows(stats, direction='all', entry_type='all'):
    """
    Build journal rows from period stats.
    Sales (paid/unpaid) are IN. Purchases (paid/unpaid) are OUT.
    """
    from app.utils import build_period_cash_entries

    direction = (direction or 'all').lower()
    entry_type = (entry_type or 'all').lower()
    raw = build_period_cash_entries(stats)

    rows = []
    chart_rows = []
    total_in = 0.0
    total_out = 0.0
    by_type = {k: 0.0 for k in JOURNAL_DIRECTION}

    for row in raw:
        et = (getattr(row, 'entry_type', None) or '').lower()
        if et not in JOURNAL_DIRECTION:
            continue
        cash_dir = JOURNAL_DIRECTION[et]
        if direction in ('in', 'out') and cash_dir != direction:
            continue
        if entry_type == 'payment' and et not in ('payment', 'advance', 'overpay'):
            continue
        if entry_type == 'vendor_pay' and et not in ('vendor_pay', 'vendor_advance'):
            continue
        if entry_type not in ('all', 'payment', 'vendor_pay') and et != entry_type:
            continue

        if et in FUEL_CASH_TOTALS:
            chart_rows.append(SimpleNamespace(
                sale_date=getattr(row, 'sale_date', None),
                cash_direction='in',
                amount=float(getattr(row, 'amount', 0) or 0),
            ))
            continue

        signed = _row_cash_amount(row)
        abs_amt = abs(signed)
        display_amt = float(getattr(row, 'amount', 0) or 0)
        if abs_amt <= 0 and display_amt <= 0:
            continue
        if abs_amt <= 0:
            if direction in ('in', 'out'):
                continue
            cash_dir = 'credit'
            abs_amt = display_amt
        elif cash_dir == 'in':
            total_in += abs_amt
        else:
            total_out += abs_amt
        by_type[et] = by_type.get(et, 0.0) + abs_amt

        party = None
        if getattr(row, 'customer', None):
            party = row.customer.name
        elif getattr(row, 'vendor_name', None):
            party = row.vendor_name
        elif getattr(row, 'vendor', None):
            party = row.vendor.name

        rows.append(SimpleNamespace(
            id=getattr(row, 'id', None),
            sale_date=getattr(row, 'sale_date', None),
            entry_type=et,
            type_label=JOURNAL_TYPE_LABELS.get(et, et),
            cash_direction=cash_dir,
            party=party or '—',
            item_name=getattr(row, 'item_name', '') or '—',
            amount=abs_amt,
            payment_status=getattr(row, 'payment_status', '') or '',
        ))
        if cash_dir in ('in', 'out'):
            chart_rows.append(SimpleNamespace(
                sale_date=getattr(row, 'sale_date', None),
                cash_direction=cash_dir,
                amount=abs_amt,
            ))

    summary = []
    total_specs = (
        ('diesel_cash', 'diesel_cash', 'diesel_cash_liters', 'Total diesel cash sale'),
        ('petrol_cash', 'petrol_cash', 'petrol_cash_liters', 'Total petrol cash sale'),
    )
    period_end = stats.get('end_date')
    for key, amount_key, liter_key, label in total_specs:
        if entry_type not in ('all', key):
            continue
        if direction == 'out':
            continue
        amount = float(stats.get(amount_key) or 0)
        liters = float(stats.get(liter_key) or 0)
        if amount <= 0.01:
            continue
        summary.append(SimpleNamespace(
            id=None,
            sale_date=period_end,
            entry_type=key,
            type_label=label,
            cash_direction='in',
            party='—',
            item_name=f'{label} · {liters:,.2f} L',
            amount=amount,
            payment_status='paid',
        ))
        total_in += amount
        by_type[key] = by_type.get(key, 0.0) + amount
    rows = summary + rows

    return {
        'rows': rows,
        'total_in': total_in,
        'total_out': total_out,
        'net': total_in - total_out,
        'by_type': by_type,
        'count': len(rows),
        'chart_rows': chart_rows,
    }
