"""Add payment_status to item_purchase_logs and backfill from linked payments."""
from __future__ import annotations

import os
import sys

import pymysql
from dotenv import load_dotenv

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
load_dotenv(os.path.join(_BASE, '.env'))


def main():
    conn = pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', '3306')),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        database=os.environ['DB_NAME'],
        charset='utf8mb4',
        connect_timeout=20,
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM item_purchase_logs LIKE 'payment_status'")
            if cur.fetchone():
                print('payment_status already exists')
            else:
                cur.execute(
                    "ALTER TABLE item_purchase_logs "
                    "ADD COLUMN payment_status VARCHAR(20) NOT NULL DEFAULT 'unpaid'"
                )
                print('added payment_status column')

            cur.execute(
                """
                UPDATE item_purchase_logs l
                JOIN vendor_payments p ON p.purchase_log_id = l.id
                SET l.payment_status = 'paid'
                WHERE IFNULL(l.payment_status, 'unpaid') <> 'paid'
                """
            )
            print(f'backfilled from purchase_log_id: {cur.rowcount}')

            # Legacy auto-pay notes without FK
            cur.execute(
                """
                UPDATE item_purchase_logs l
                JOIN vendor_payments p
                  ON p.vendor_id = l.vendor_id
                 AND p.note LIKE CONCAT('Paid with inventory purchase: %%', l.item_name, '%%')
                SET l.payment_status = 'paid'
                WHERE IFNULL(l.payment_status, 'unpaid') <> 'paid'
                  AND l.vendor_id IS NOT NULL
                """
            )
            print(f'backfilled from note match: {cur.rowcount}')

            conn.commit()
            print('Done.')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('FAILED:', e, file=sys.stderr)
        sys.exit(1)
