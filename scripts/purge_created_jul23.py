"""
Delete ALL transactional rows created on 2026-07-23 (and Jul-20 rows without created_at).
KEEP every MeterReading.

Usage:
  python scripts/purge_created_jul23.py --production           # dry-run on PROD
  python scripts/purge_created_jul23.py --production --commit  # apply on PROD
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--production', action='store_true', help='Target production DB')
    return parser.parse_args()


def _bootstrap_env(production: bool):
    from dotenv import load_dotenv

    load_dotenv(ROOT / '.env', override=False)

    if production:
        # Set BEFORE importing app.config (URI is built at import time)
        os.environ['DB_USER'] = os.environ.get('PROD_DB_USER', 'mygymlahore_mianbrother112')
        os.environ['DB_PASSWORD'] = os.environ.get('PROD_DB_PASSWORD', 'Mianbrothers@0336')
        os.environ['DB_HOST'] = os.environ.get('PROD_DB_HOST', os.environ.get('DB_HOST', '148.163.100.132'))
        os.environ['DB_PORT'] = os.environ.get('PROD_DB_PORT', os.environ.get('DB_PORT', '3306'))
        os.environ['DB_NAME'] = os.environ.get('PROD_DB_NAME', 'mygymlahore_mianbrothers')
        print('*** TARGET: PRODUCTION DATABASE ***')


DAY = date(2026, 7, 23)
BACKDATE = date(2026, 7, 20)


def main():
    args = _parse_args()
    _bootstrap_env(args.production)

    from app import create_app
    from app.models import (
        db,
        MeterReading,
        CreditSale,
        Expense,
        CashTaken,
        DailyCashCount,
        DailyTillBalance,
        DailyFuelStock,
        Payment,
        VendorPayment,
        Sale,
        StockEntry,
        ItemPurchaseLog,
        Inventory,
        OtherItem,
        Vendor,
        Customer,
        FuelPrice,
        ItemPriceLog,
    )
    from app.services.entries import delete_credit_sale, delete_payment, delete_expense
    from app.vendors.service import recalculate_vendor_balance
    from app.customers.service import recalculate_customer_balance

    def on_day(col, day=DAY):
        start = datetime.combine(day, time.min)
        end = datetime.combine(day, time.max)
        return db.and_(col >= start, col <= end)

    def on_day_or_backdate(col):
        return db.or_(on_day(col, DAY), on_day(col, BACKDATE))

    def reverse_purchase_stock(log: ItemPurchaseLog):
        cat = (log.category or '').lower()
        liters = float(log.liters or 0)
        qty = int(round(float(log.quantity or 0)))

        if cat == 'fuel' and log.fuel_type_id:
            inv = Inventory.query.filter_by(fuel_type_id=log.fuel_type_id).first()
            if inv:
                inv.current_stock_liters = max(float(inv.current_stock_liters or 0) - liters, 0.0)
            return

        item = None
        if getattr(log, 'other_item_id', None):
            item = OtherItem.query.get(log.other_item_id)
        if item is None and log.item_name:
            q = OtherItem.query.filter(OtherItem.name == log.item_name)
            if cat == 'ft_mobile':
                q = q.filter_by(category='ft_mobile')
            item = q.first()
        if not item:
            return
        if cat == 'ft_mobile' or (item.category or '') == 'ft_mobile':
            item.liters = max(float(item.liters or 0) - liters, 0.0)
        else:
            item.quantity = max(int(item.quantity or 0) - qty, 0)

    app = create_app()
    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        db_name = uri.rsplit('/', 1)[-1].split('?')[0] if uri else '?'
        print(f'Database: {db_name}')
        if args.production and 'mianbrothers_test' in db_name:
            print('ERROR: refused — still connected to TEST DB')
            sys.exit(1)
        if args.production and db_name != 'mygymlahore_mianbrothers':
            print(f'ERROR: refused — unexpected DB name {db_name!r}')
            sys.exit(1)

        print(f'Delete everything created on {DAY} (+ Jul-20 business dates without created_at)')
        print('KEEP: all MeterReading rows')
        print()

        meters_total = MeterReading.query.count()
        meters_day = MeterReading.query.filter_by(reading_date=DAY).count()
        meters_back = MeterReading.query.filter_by(reading_date=BACKDATE).count()
        print(f'MeterReading total KEEP: {meters_total}')
        print(f'  (reading_date {DAY}: {meters_day}, reading_date {BACKDATE}: {meters_back})')

        credit = CreditSale.query.filter(on_day(CreditSale.created_at, DAY)).order_by(CreditSale.id).all()
        expenses = Expense.query.filter(on_day(Expense.created_at, DAY)).order_by(Expense.id).all()
        cash_taken = CashTaken.query.filter(on_day(CashTaken.created_at, DAY)).order_by(CashTaken.id).all()
        payments = Payment.query.filter(on_day_or_backdate(Payment.payment_date)).order_by(Payment.id).all()
        vendor_pays = VendorPayment.query.filter(
            on_day_or_backdate(VendorPayment.payment_date)
        ).order_by(VendorPayment.id).all()
        legacy_sales = Sale.query.filter(on_day_or_backdate(Sale.sale_date)).order_by(Sale.id).all()
        purchase_logs = ItemPurchaseLog.query.filter(
            on_day_or_backdate(ItemPurchaseLog.entry_date)
        ).order_by(ItemPurchaseLog.id).all()
        stock_entries = StockEntry.query.filter(
            on_day_or_backdate(StockEntry.entry_date)
        ).order_by(StockEntry.id).all()
        cash_counts = DailyCashCount.query.filter(
            DailyCashCount.count_date.in_([DAY, BACKDATE])
        ).all()
        till = DailyTillBalance.query.filter(
            DailyTillBalance.balance_date.in_([DAY, BACKDATE])
        ).all()
        fuel_stock = DailyFuelStock.query.filter(
            DailyFuelStock.stock_date.in_([DAY, BACKDATE])
        ).all()
        fuel_prices = FuelPrice.query.filter(on_day(FuelPrice.created_at, DAY)).all()
        item_prices = ItemPriceLog.query.filter(on_day(ItemPriceLog.created_at, DAY)).all()

        print('--- WILL DELETE ---')
        print(f'CreditSale (created_at {DAY}): {len(credit)}  {dict(Counter((c.entry_type or "sale") for c in credit))}')
        print(f'  sale_dates: {dict(Counter(str(c.sale_date) for c in credit))}')
        print(f'Expense (created_at {DAY}): {len(expenses)}')
        print(f'CashTaken (created_at {DAY}): {len(cash_taken)}')
        print(f'Payment (date {BACKDATE} or {DAY}): {len(payments)}')
        print(f'VendorPayment (date {BACKDATE} or {DAY}): {len(vendor_pays)}')
        print(f'Sale legacy (date {BACKDATE} or {DAY}): {len(legacy_sales)}')
        print(f'ItemPurchaseLog (date {BACKDATE} or {DAY}): {len(purchase_logs)}')
        print(f'StockEntry (date {BACKDATE} or {DAY}): {len(stock_entries)}')
        print(f'DailyCashCount: {len(cash_counts)}')
        print(f'DailyTillBalance: {len(till)}')
        print(f'DailyFuelStock: {len(fuel_stock)}')
        print(f'FuelPrice (created_at {DAY}): {len(fuel_prices)}')
        print(f'ItemPriceLog (created_at {DAY}): {len(item_prices)}')
        print()

        touched_customers = set()
        touched_vendors = set()

        for cs in list(credit):
            sid, cid, et, sd = cs.id, cs.customer_id, cs.entry_type, cs.sale_date
            delete_credit_sale(cs)
            if cid:
                touched_customers.add(cid)
            print(f'  - CreditSale #{sid} type={et} sale_date={sd}')

        for p in list(payments):
            pid, cid = p.id, p.customer_id
            delete_payment(p)
            if cid:
                touched_customers.add(cid)
            print(f'  - Payment #{pid}')

        for e in list(expenses):
            eid = e.id
            delete_expense(e)
            print(f'  - Expense #{eid}')

        for row in list(cash_taken):
            rid = row.id
            db.session.delete(row)
            print(f'  - CashTaken #{rid}')

        for vp in list(vendor_pays):
            vpid, vid = vp.id, vp.vendor_id
            db.session.delete(vp)
            if vid:
                touched_vendors.add(vid)
            print(f'  - VendorPayment #{vpid}')

        for log in list(purchase_logs):
            lid = log.id
            if log.vendor_id:
                touched_vendors.add(log.vendor_id)
            reverse_purchase_stock(log)
            db.session.delete(log)
            print(f'  - ItemPurchaseLog #{lid}')

        for se in list(stock_entries):
            sid = se.id
            db.session.delete(se)
            print(f'  - StockEntry #{sid}')

        for s in list(legacy_sales):
            if s.customer_id:
                touched_customers.add(s.customer_id)
            sid = s.id
            db.session.delete(s)
            print(f'  - Sale #{sid}')

        for row in list(cash_counts):
            db.session.delete(row)
            print('  - DailyCashCount')
        for row in list(till):
            db.session.delete(row)
            print('  - DailyTillBalance')
        for row in list(fuel_stock):
            rid = row.id
            db.session.delete(row)
            print(f'  - DailyFuelStock #{rid}')

        for row in list(fuel_prices):
            rid = row.id
            db.session.delete(row)
            print(f'  - FuelPrice #{rid}')
        for row in list(item_prices):
            rid = row.id
            db.session.delete(row)
            print(f'  - ItemPriceLog #{rid}')

        db.session.flush()

        for cid in touched_customers:
            c = Customer.query.get(cid)
            if c:
                recalculate_customer_balance(c)
                print(f'  ~ Customer #{cid} balance recalculated')
        for vid in touched_vendors:
            v = Vendor.query.get(vid)
            if v:
                recalculate_vendor_balance(v)
                print(f'  ~ Vendor #{vid} balance recalculated')

        left_credit = CreditSale.query.filter(on_day(CreditSale.created_at, DAY)).count()
        left_meters = MeterReading.query.count()
        print(f'\nCreditSale created on {DAY} left: {left_credit}')
        print(f'MeterReading kept: {left_meters}')

        if args.commit:
            db.session.commit()
            print('\nCOMMITTED on PRODUCTION.' if args.production else '\nCOMMITTED.')
        else:
            db.session.rollback()
            print('\nDRY-RUN (rolled back). Re-run with --production --commit to apply.')


if __name__ == '__main__':
    main()
