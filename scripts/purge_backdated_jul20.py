"""
Purge Jul 20 business-date rows that were entered on Jul 23 (backdated).
Keep MeterReading for Jul 20.

Usage:
  python scripts/purge_backdated_jul20.py
  python scripts/purge_backdated_jul20.py --commit
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / '.env', override=True)

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
)
from app.services.entries import delete_credit_sale, delete_payment, delete_expense
from app.vendors.service import recalculate_vendor_balance
from app.customers.service import recalculate_customer_balance


SALE_DAY = date(2026, 7, 20)
CREATED_DAY = date(2026, 7, 23)


def created_on(col, day: date):
    start = datetime.combine(day, time.min)
    end = datetime.combine(day, time.max)
    return db.and_(col >= start, col <= end)


def datetime_on(col, day: date):
    start = datetime.combine(day, time.min)
    end = datetime.combine(day, time.max)
    return db.and_(col >= start, col <= end)


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


def collect():
    """Gather rows: business date Jul 20, entered Jul 23 when created_at exists.
    Must run inside an app context.
    """
    credit = CreditSale.query.filter(
        CreditSale.sale_date == SALE_DAY,
        created_on(CreditSale.created_at, CREATED_DAY),
    ).order_by(CreditSale.id).all()

    expenses = Expense.query.filter(
        Expense.expense_date == SALE_DAY,
        created_on(Expense.created_at, CREATED_DAY),
    ).order_by(Expense.id).all()

    cash_taken = CashTaken.query.filter(
        CashTaken.taken_date == SALE_DAY,
        created_on(CashTaken.created_at, CREATED_DAY),
    ).order_by(CashTaken.id).all()

    # No created_at: business datetime on SALE_DAY only.
    payments = Payment.query.filter(datetime_on(Payment.payment_date, SALE_DAY)).order_by(Payment.id).all()
    vendor_pays = VendorPayment.query.filter(
        datetime_on(VendorPayment.payment_date, SALE_DAY)
    ).order_by(VendorPayment.id).all()
    legacy_sales = Sale.query.filter(datetime_on(Sale.sale_date, SALE_DAY)).order_by(Sale.id).all()
    purchase_logs = ItemPurchaseLog.query.filter(
        datetime_on(ItemPurchaseLog.entry_date, SALE_DAY)
    ).order_by(ItemPurchaseLog.id).all()
    stock_entries = StockEntry.query.filter(
        datetime_on(StockEntry.entry_date, SALE_DAY)
    ).order_by(StockEntry.id).all()

    cash_counts = DailyCashCount.query.filter_by(count_date=SALE_DAY).all()
    till = DailyTillBalance.query.filter_by(balance_date=SALE_DAY).all()
    fuel_stock = DailyFuelStock.query.filter_by(stock_date=SALE_DAY).all()
    meters = MeterReading.query.filter_by(reading_date=SALE_DAY).count()

    credit_real = CreditSale.query.filter(
        CreditSale.sale_date == SALE_DAY,
        created_on(CreditSale.created_at, SALE_DAY),
    ).count()
    credit_other = CreditSale.query.filter(
        CreditSale.sale_date == SALE_DAY,
        db.not_(created_on(CreditSale.created_at, CREATED_DAY)),
    ).count()

    return {
        'meters_keep': meters,
        'credit': credit,
        'credit_real_keep': credit_real,
        'credit_other_keep': credit_other,
        'expenses': expenses,
        'cash_taken': cash_taken,
        'payments': payments,
        'vendor_pays': vendor_pays,
        'legacy_sales': legacy_sales,
        'purchase_logs': purchase_logs,
        'stock_entries': stock_entries,
        'cash_counts': cash_counts,
        'till': till,
        'fuel_stock': fuel_stock,
    }


def print_preview(data, db_name: str):
    print(f'Database: {db_name}')
    print(f'Target business date: {SALE_DAY}')
    print(f'Entered / created on:  {CREATED_DAY}')
    print(f'KEEP MeterReading on {SALE_DAY}: {data["meters_keep"]}')
    print()
    print('--- DELETE (sale/expense date 20 + created_at on 23) ---')
    types = dict(Counter((c.entry_type or 'sale') for c in data['credit']))
    print(f'CreditSale: {len(data["credit"])}  {types}')
    print(f'Expense:    {len(data["expenses"])}')
    print(f'CashTaken:  {len(data["cash_taken"])}')
    print()
    print(f'KEEP CreditSale sale_date=20 created on 20: {data["credit_real_keep"]}')
    print(f'KEEP CreditSale sale_date=20 created other day (not 23): {data["credit_other_keep"]}')
    print()
    print('--- ALSO DELETE (business date 20, no created_at column) ---')
    print(f'Payment:          {len(data["payments"])}')
    print(f'VendorPayment:    {len(data["vendor_pays"])}')
    print(f'Sale (legacy):    {len(data["legacy_sales"])}')
    print(f'ItemPurchaseLog:  {len(data["purchase_logs"])}')
    print(f'StockEntry:       {len(data["stock_entries"])}')
    print(f'DailyCashCount:   {len(data["cash_counts"])}')
    print(f'DailyTillBalance: {len(data["till"])}')
    print(f'DailyFuelStock:   {len(data["fuel_stock"])}')


def purge(app, commit: bool):
    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        db_name = uri.rsplit('/', 1)[-1].split('?')[0] if uri else '?'
        data = collect()
        print_preview(data, db_name)
        print()

        touched_customers = set()
        touched_vendors = set()

        for cs in list(data['credit']):
            cid = cs.customer_id
            et = cs.entry_type
            sid = cs.id
            delete_credit_sale(cs)
            if cid:
                touched_customers.add(cid)
            print(f'  deleted CreditSale #{sid} ({et})')

        for p in list(data['payments']):
            cid = p.customer_id
            pid = p.id
            delete_payment(p)
            if cid:
                touched_customers.add(cid)
            print(f'  deleted Payment #{pid}')

        for e in list(data['expenses']):
            eid = e.id
            delete_expense(e)
            print(f'  deleted Expense #{eid}')

        for row in list(data['cash_taken']):
            rid = row.id
            db.session.delete(row)
            print(f'  deleted CashTaken #{rid}')

        for vp in list(data['vendor_pays']):
            vid = vp.vendor_id
            vpid = vp.id
            db.session.delete(vp)
            if vid:
                touched_vendors.add(vid)
            print(f'  deleted VendorPayment #{vpid}')

        # Reverse stock from purchase logs, then delete logs + stock entries
        for log in list(data['purchase_logs']):
            lid = log.id
            if log.vendor_id:
                touched_vendors.add(log.vendor_id)
            reverse_purchase_stock(log)
            db.session.delete(log)
            print(f'  deleted ItemPurchaseLog #{lid}')

        for se in list(data['stock_entries']):
            # liters already reversed via purchase logs when paired; delete orphan row
            sid = se.id
            db.session.delete(se)
            print(f'  deleted StockEntry #{sid}')

        for s in list(data['legacy_sales']):
            if s.customer_id:
                touched_customers.add(s.customer_id)
            sid = s.id
            db.session.delete(s)
            print(f'  deleted Sale #{sid}')

        for row in list(data['cash_counts']):
            db.session.delete(row)
            print('  deleted DailyCashCount')
        for row in list(data['till']):
            db.session.delete(row)
            print('  deleted DailyTillBalance')
        for row in list(data['fuel_stock']):
            rid = row.id
            db.session.delete(row)
            print(f'  deleted DailyFuelStock #{rid}')

        db.session.flush()

        for cid in touched_customers:
            c = Customer.query.get(cid)
            if c:
                recalculate_customer_balance(c)
                print(f'  recalculated Customer #{cid}')

        for vid in touched_vendors:
            v = Vendor.query.get(vid)
            if v:
                recalculate_vendor_balance(v)
                print(f'  recalculated Vendor #{vid}')

        meters = MeterReading.query.filter_by(reading_date=SALE_DAY).count()
        leftover_backdated = CreditSale.query.filter(
            CreditSale.sale_date == SALE_DAY,
            created_on(CreditSale.created_at, CREATED_DAY),
        ).count()
        print(f'\nMeterReading kept for {SALE_DAY}: {meters}')
        print(f'Backdated CreditSale remaining: {leftover_backdated}')

        if commit:
            db.session.commit()
            print('\nCOMMITTED.')
        else:
            db.session.rollback()
            print('\nDRY-RUN (rolled back). Run with --commit to apply.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    app = create_app()
    purge(app, commit=args.commit)


if __name__ == '__main__':
    main()
