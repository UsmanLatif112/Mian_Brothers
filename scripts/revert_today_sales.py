"""
ONE-TIME: remove today's SALES activity only.

KEEP:
  - inventory / opening stock
  - customers + opening credit balances
  - vendors + opening payable balances
  - fuel types / prices
  - machines

REMOVE for the target day (default: today UTC):
  - credit/other/FT sales (entry_type != opening)
  - customer payments recorded that day
  - legacy Sale rows that day
  - daily cash count / cash taken / till balance / daily fuel stock

KEEP for that day:
  - meter readings (fuel meter sales) — rates refreshed separately

Then rebuild customer + vendor balances.

Then rebuild customer + vendor balances.

Usage:
  PYTHONPATH=. python scripts/revert_today_sales.py
  PYTHONPATH=. python scripts/revert_today_sales.py --date=2026-10-02
"""
from datetime import datetime, date, time

from app import create_app
from app.models import (
    db,
    MeterReading,
    CreditSale,
    Payment,
    Sale,
    DailyCashCount,
    CashTaken,
    DailyTillBalance,
    DailyFuelStock,
    Customer,
    Vendor,
)
from app.customers.service import recalculate_customer_balance
from app.vendors.service import recalculate_vendor_balance


def _parse_day(argv):
    for arg in argv:
        if arg.startswith('--date='):
            return datetime.strptime(arg.split('=', 1)[1], '%Y-%m-%d').date()
    return datetime.utcnow().date()


def _delete(query, label):
    rows = query.all()
    n = len(rows)
    for row in rows:
        db.session.delete(row)
    print(f'  removed {label}: {n}')
    return n


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    day = _parse_day(argv)
    start_dt = datetime.combine(day, time.min)
    end_dt = datetime.combine(day, time.max)

    app = create_app()
    with app.app_context():
        print(f'Reverting SALES data for {day.isoformat()} (keeping meters + inventory + openings)...')

        # Track customers touched by deleted sales/payments for balance rebuild
        customer_ids = set()
        for cid in (
            db.session.query(CreditSale.customer_id)
            .filter(
                CreditSale.sale_date == day,
                CreditSale.entry_type != 'opening',
                CreditSale.customer_id.isnot(None),
            )
            .distinct()
        ):
            if cid[0]:
                customer_ids.add(cid[0])
        for cid in (
            db.session.query(Payment.customer_id)
            .filter(
                Payment.payment_date >= start_dt,
                Payment.payment_date <= end_dt,
            )
            .distinct()
        ):
            if cid[0]:
                customer_ids.add(cid[0])

        meters_kept = MeterReading.query.filter(MeterReading.reading_date == day).count()
        print(f'  kept meter readings: {meters_kept}')

        _delete(
            CreditSale.query.filter(
                CreditSale.sale_date == day,
                CreditSale.entry_type != 'opening',
            ),
            'credit/other/FT sales (non-opening)',
        )
        _delete(
            Payment.query.filter(
                Payment.payment_date >= start_dt,
                Payment.payment_date <= end_dt,
            ),
            'customer payments',
        )
        _delete(
            Sale.query.filter(
                Sale.sale_date >= start_dt,
                Sale.sale_date <= end_dt,
            ),
            'legacy sales',
        )
        _delete(
            DailyCashCount.query.filter(DailyCashCount.count_date == day),
            'daily cash counts',
        )
        _delete(
            CashTaken.query.filter(CashTaken.taken_date == day),
            'cash taken',
        )
        _delete(
            DailyTillBalance.query.filter(DailyTillBalance.balance_date == day),
            'daily till balances',
        )
        _delete(
            DailyFuelStock.query.filter(DailyFuelStock.stock_date == day),
            'daily fuel stocks',
        )

        db.session.flush()

        for cid in sorted(customer_ids):
            customer = Customer.query.get(cid)
            if customer:
                before = float(customer.current_balance_due or 0)
                recalculate_customer_balance(customer)
                after = float(customer.current_balance_due or 0)
                print(f'  customer #{cid} {customer.name}: due {before:.2f} → {after:.2f}')

        for vendor in Vendor.query.all():
            before = float(vendor.current_balance_payable or 0)
            recalculate_vendor_balance(vendor)
            after = float(vendor.current_balance_payable or 0)
            if abs(before - after) >= 0.005:
                print(f'  vendor #{vendor.id} {vendor.name}: payable {before:.2f} → {after:.2f}')

        # Confirm openings kept
        openings = CreditSale.query.filter_by(entry_type='opening').count()
        print(f'  kept opening customer credits: {openings}')
        print(f'  kept customers: {Customer.query.count()}, vendors: {Vendor.query.count()}')

        db.session.commit()
        print(f'Done. Sales activity for {day.isoformat()} removed; inventory + openings kept.')


if __name__ == '__main__':
    main()
