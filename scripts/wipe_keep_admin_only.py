"""
Delete ALL application data. Keep only the super-admin user.

Preserves the existing admin account (password unchanged).
Removes staff users and every business table (sales, stock, customers, etc.).

Usage:
  PYTHONPATH=. python scripts/wipe_keep_admin_only.py
"""
from __future__ import annotations

import os
import sys

import pymysql
from dotenv import load_dotenv

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
load_dotenv(os.path.join(_BASE, '.env'))

ADMIN_FALLBACK_NAME = 'U. Technologies'
ADMIN_FALLBACK_EMAIL = 'admin@udottechnologies.com'


def connect():
    host = os.environ.get('DB_HOST', 'localhost')
    port = int(os.environ.get('DB_PORT', '3306'))
    user = os.environ.get('DB_USER')
    password = os.environ.get('DB_PASSWORD')
    name = os.environ.get('DB_NAME')
    if not (user and password and name):
        raise SystemExit('DB_USER / DB_PASSWORD / DB_NAME required in .env')

    print(f'Connecting to {host}:{port}/{name} …')
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=name,
        charset='utf8mb4',
        connect_timeout=20,
        read_timeout=120,
        write_timeout=120,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def pick_admin(cur) -> dict | None:
    cur.execute(
        "SELECT id, name, email, password_hash, role, phone, status, created_at "
        "FROM users WHERE role = 'admin' AND status = 'active' ORDER BY id ASC"
    )
    admins = cur.fetchall()
    if not admins:
        cur.execute(
            "SELECT id, name, email, password_hash, role, phone, status, created_at "
            "FROM users WHERE role = 'admin' ORDER BY id ASC"
        )
        admins = cur.fetchall()
    if not admins:
        return None

    for row in admins:
        if (row.get('name') or '') == ADMIN_FALLBACK_NAME:
            return row
        if (row.get('email') or '').lower() == ADMIN_FALLBACK_EMAIL.lower():
            return row
    return admins[0]


def main():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT DATABASE() AS db')
            print('Database:', cur.fetchone()['db'])

            cur.execute('SHOW TABLES')
            key = cur.description[0][0]
            tables = [row[key] for row in cur.fetchall()]
            print(f'Tables: {len(tables)}')

            admin = None
            if 'users' in tables:
                admin = pick_admin(cur)
                if admin:
                    print(
                        f'Keeping admin: id={admin["id"]} name={admin["name"]!r} '
                        f'email={admin["email"]!r}'
                    )
                else:
                    print('WARNING: no admin user found — will wipe users too')

            cur.execute('SET FOREIGN_KEY_CHECKS=0')

            for table in tables:
                cur.execute(f'DELETE FROM `{table}`')
                print(f'  cleared {table}: {cur.rowcount} rows')

            if admin:
                cur.execute(
                    """
                    INSERT INTO users
                        (id, name, email, password_hash, role, phone, status, created_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        admin['id'],
                        admin['name'],
                        admin['email'],
                        admin['password_hash'],
                        admin.get('role') or 'admin',
                        admin.get('phone'),
                        admin.get('status') or 'active',
                        admin.get('created_at'),
                    ),
                )
                print(f'Restored admin id={admin["id"]}')

            cur.execute('SET FOREIGN_KEY_CHECKS=1')
            conn.commit()

            cur.execute('SELECT id, name, email, role, status FROM users')
            left = cur.fetchall()
            print(f'Users remaining: {len(left)}')
            for u in left:
                print(f'  - id={u["id"]} {u["name"]!r} ({u["role"]}, {u["status"]})')

            print('Done. All business data deleted; only super admin kept.')
            print('On next app start, system categories / default fuels may be re-seeded empty.')
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
