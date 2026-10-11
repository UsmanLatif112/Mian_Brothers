"""Seed default fuels + shop categories after wipe_keep_admin_only."""
from __future__ import annotations

import os

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
        cursorclass=pymysql.cursors.DictCursor,
    )
    aid = 1
    cats = [
        ('fuel', 'Fuel', 'fuel', 10),
        ('mobile', 'Mobile (Oil)', 'qty', 20),
        ('ft_mobile', 'FT Mobile Oil', 'liters', 30),
        ('filter', 'Filter', 'qty', 40),
        ('other', 'Other Item', 'qty', 50),
    ]
    with conn.cursor() as cur:
        cur.execute('SELECT id FROM agencies ORDER BY id ASC LIMIT 1')
        row = cur.fetchone()
        if row:
            aid = row['id']

        for name in ('Petrol', 'Diesel'):
            cur.execute(
                'SELECT id FROM fuel_types WHERE LOWER(name)=LOWER(%s) AND agency_id=%s',
                (name, aid),
            )
            ft = cur.fetchone()
            if not ft:
                cur.execute(
                    'INSERT INTO fuel_types (name, unit, agency_id) VALUES (%s, %s, %s)',
                    (name, 'Liter', aid),
                )
                ft_id = cur.lastrowid
                print('fuel', name, ft_id)
            else:
                ft_id = ft['id']
                print('fuel exists', name, ft_id)
            cur.execute(
                'SELECT id FROM inventory WHERE fuel_type_id=%s AND agency_id=%s',
                (ft_id, aid),
            )
            if not cur.fetchone():
                cur.execute(
                    'INSERT INTO inventory '
                    '(fuel_type_id, agency_id, current_stock_liters, reorder_threshold) '
                    'VALUES (%s, %s, 0, 0)',
                    (ft_id, aid),
                )
                print('inventory', ft_id)

        for key, name, mode, sort in cats:
            cur.execute(
                'SELECT id FROM shop_categories WHERE `key`=%s AND agency_id=%s',
                (key, aid),
            )
            if not cur.fetchone():
                cur.execute(
                    'INSERT INTO shop_categories '
                    '(`key`, name, unit_mode, is_system, is_active, sort_order, agency_id) '
                    'VALUES (%s, %s, %s, 1, 1, %s, %s)',
                    (key, name, mode, sort, aid),
                )
                print('category', key)
            else:
                print('category exists', key)

        conn.commit()
        cur.execute('SELECT COUNT(*) AS c FROM fuel_types')
        print('fuels', cur.fetchone()['c'])
        cur.execute('SELECT COUNT(*) AS c FROM shop_categories')
        print('categories', cur.fetchone()['c'])
        cur.execute('SELECT id, name, role, agency_id FROM users')
        print('users', cur.fetchall())
    conn.close()
    print('done')


if __name__ == '__main__':
    main()
