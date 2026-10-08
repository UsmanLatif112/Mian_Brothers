from types import SimpleNamespace

from flask import render_template, request
from flask_login import login_required

from app.journal import journal_bp
from app.journal.service import (
    DIRECTION_FILTER_CHOICES,
    TYPE_FILTER_CHOICES,
    build_cash_flow_rows,
)
from app.models import (
    CreditSale, Expense, Payment, DailyCashCount, Customer,
    MeterReading, FuelType, FuelPrice,
)
from app.utils import PERIOD_CHOICES, compute_period_stats, paginate, parse_period

PER_PAGE = 25


@journal_bp.route('/', methods=['GET'])
@login_required
def index():
    # Default to today so the till view is actionable on open.
    args = request.args.to_dict(flat=True)
    if 'period' not in request.args:
        args['period'] = 'today'
    period, start, end = parse_period(args)
    direction = (request.args.get('direction') or 'all').strip().lower()
    if direction not in dict(DIRECTION_FILTER_CHOICES):
        direction = 'all'
    entry_type = (request.args.get('type') or 'all').strip().lower()
    if entry_type not in dict(TYPE_FILTER_CHOICES):
        entry_type = 'all'

    models_ns = SimpleNamespace(
        MeterReading=MeterReading,
        FuelType=FuelType,
        FuelPrice=FuelPrice,
        CreditSale=CreditSale,
        Expense=Expense,
        Payment=Payment,
        DailyCashCount=DailyCashCount,
        Customer=Customer,
    )
    stats = compute_period_stats(
        start, end, models_ns,
        include_opening_credit=False,
    )
    flow = build_cash_flow_rows(stats, direction=direction, entry_type=entry_type)
    search_q = (request.args.get('search') or '').strip().lower()
    rows = flow['rows']
    if search_q:
        def _journal_match(row):
            blob = ' '.join([
                str(getattr(row, 'type_label', '') or ''),
                str(getattr(row, 'party', '') or ''),
                str(getattr(row, 'item_name', '') or ''),
                str(getattr(row, 'entry_type', '') or ''),
                str(getattr(row, 'cash_direction', '') or ''),
            ]).lower()
            return search_q in blob
        rows = [r for r in rows if _journal_match(r)]

    page = request.args.get('page', 1)
    page_rows, pagination = paginate(rows, page, PER_PAGE)

    from app.charts_data import journal_listing_series

    return render_template(
        'journal/index.html',
        rows=page_rows,
        pagination=pagination,
        total_in=flow['total_in'],
        total_out=flow['total_out'],
        net=flow['net'],
        by_type=flow['by_type'],
        row_count=len(rows),
        period=period,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        period_choices=PERIOD_CHOICES,
        direction=direction,
        entry_type=entry_type,
        direction_choices=DIRECTION_FILTER_CHOICES,
        type_choices=TYPE_FILTER_CHOICES,
        chart_series=journal_listing_series(rows),
        search=search_q,
    )
