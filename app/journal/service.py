"""Cash journal helpers — IN/OUT cash movements only (no openings / unpaid purchases)."""

from types import SimpleNamespace

# User rules:
#   IN  — customer settle/payment, customer advance, expense settle
#   OUT — paid inventory purchase, vendor payment, customer loan, expenses, cash taken
# Excluded — opening balance, unpaid purchases (payable via vendor ledger), sales lines
JOURNAL_DIRECTION = {
    'payment': 'in',
    'advance': 'in',
    'overpay': 'in',
    'settle': 'in',
    'loan': 'out',
    'expense': 'out',
    'purchase': 'out',
    'vendor_pay': 'out',
    'cash_taken': 'out',
}

JOURNAL_TYPE_LABELS = {
    'payment': 'Customer settle',
    'advance': 'Customer advance',
    'overpay': 'Overpayment',
    'settle': 'Expense settle',
    'loan': 'Customer loan',
    'expense': 'Expense',
    'purchase': 'Inventory purchase',
    'vendor_pay': 'Vendor payment',
    'cash_taken': 'Cash taken',
}

TYPE_FILTER_CHOICES = (
    ('all', 'All types'),
    ('payment', 'Customer settle'),
    ('advance', 'Customer advance'),
    ('loan', 'Customer loan'),
    ('purchase', 'Inventory purchase'),
    ('vendor_pay', 'Vendor payment'),
    ('expense', 'Expense'),
    ('settle', 'Expense settle'),
    ('cash_taken', 'Cash taken'),
)

DIRECTION_FILTER_CHOICES = (
    ('all', 'All'),
    ('in', 'Cash In'),
    ('out', 'Cash Out'),
)


def _row_cash_amount(row):
    """Signed cash amount for the till (positive = in, negative = out)."""
    et = (getattr(row, 'entry_type', None) or '').lower()
    direction = JOURNAL_DIRECTION.get(et)
    if not direction:
        return 0.0
    if et in ('payment', 'advance', 'overpay', 'settle'):
        amt = float(getattr(row, 'amount_paid', 0) or getattr(row, 'amount', 0) or 0)
    elif et == 'purchase':
        # Paid inventory only (unpaid never reaches the journal)
        amt = float(getattr(row, 'amount_paid', 0) or getattr(row, 'amount', 0) or 0)
    else:
        amt = float(getattr(row, 'amount', 0) or getattr(row, 'amount_paid', 0) or 0)
    return amt if direction == 'in' else -amt


def build_cash_flow_rows(stats, direction='all', entry_type='all'):
    """
    Build journal rows from period stats.
    Skips openings, previous balance, sales, and unpaid purchases.
    """
    from app.utils import build_period_cash_entries

    direction = (direction or 'all').lower()
    entry_type = (entry_type or 'all').lower()
    raw = build_period_cash_entries(stats)

    rows = []
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
        if entry_type != 'all' and et != entry_type:
            continue

        signed = _row_cash_amount(row)
        abs_amt = abs(signed)
        if cash_dir == 'in':
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

    return {
        'rows': rows,
        'total_in': total_in,
        'total_out': total_out,
        'net': total_in - total_out,
        'by_type': by_type,
        'count': len(rows),
    }
