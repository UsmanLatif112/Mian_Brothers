"""
Delete ALL application data. Keep only the super-admin user + OctaneFlow agency.

Preserves the existing admin/super_admin account (password unchanged).
Removes other users and every business table (sales, stock, customers, etc.).

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
AGENCY_NAME = 'OctaneFlow'


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


def table_columns(cur, table: str) -> set[str]:
    cur.execute(f'SHOW COLUMNS FROM `{table}`')
    return {row['Field'] for row in cur.fetchall()}


def pick_admin(cur) -> dict | None:
    cur.execute(
        """
        SELECT *
        FROM users
        WHERE role IN ('super_admin', 'admin') AND status = 'active'
        ORDER BY FIELD(role, 'super_admin', 'admin'), id ASC
        """
    )
    admins = cur.fetchall()
    if not admins:
        cur.execute(
            """
            SELECT *
            FROM users
            WHERE role IN ('super_admin', 'admin')
            ORDER BY FIELD(role, 'super_admin', 'admin'), id ASC
            """
        )
        admins = cur.fetchall()
    if not admins:
        cur.execute('SELECT * FROM users ORDER BY id ASC LIMIT 1')
        admins = cur.fetchall()
    if not admins:
        return None

    for row in admins:
        if (row.get('name') or '') == ADMIN_FALLBACK_NAME:
            return row
        if (row.get('email') or '').lower() == ADMIN_FALLBACK_EMAIL.lower():
            return row
    return admins[0]


def pick_agency(cur, tables: list[str], admin: dict | None) -> dict | None:
    if 'agencies' not in tables:
        return None
    if admin and admin.get('agency_id'):
        cur.execute('SELECT * FROM agencies WHERE id = %s', (admin['agency_id'],))
        row = cur.fetchone()
        if row:
            return row
    cur.execute(
        "SELECT * FROM agencies WHERE name LIKE %s ORDER BY id ASC LIMIT 1",
        ('%octane%',),
    )
    row = cur.fetchone()
    if row:
        return row
    cur.execute('SELECT * FROM agencies ORDER BY id ASC LIMIT 1')
    return cur.fetchone()


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
            agency = None
            if 'users' in tables:
                admin = pick_admin(cur)
                if admin:
                    print(
                        f'Keeping admin: id={admin["id"]} name={admin["name"]!r} '
                        f'email={admin["email"]!r} role={admin.get("role")!r}'
                    )
                else:
                    print('WARNING: no admin user found — will wipe users too')

            agency = pick_agency(cur, tables, admin)
            if agency:
                print(f'Keeping agency: id={agency["id"]} name={agency["name"]!r}')
            else:
                print('No agency found — will recreate OctaneFlow after wipe')

            cur.execute('SET FOREIGN_KEY_CHECKS=0')

            for table in tables:
                cur.execute(f'DELETE FROM `{table}`')
                print(f'  cleared {table}: {cur.rowcount} rows')

            agency_id = None
            if 'agencies' in tables:
                cols = table_columns(cur, 'agencies')
                if agency:
                    fields = ['id', 'name']
                    values = [agency['id'], agency.get('name') or AGENCY_NAME]
                    for col in ('address', 'phone', 'email', 'logo', 'is_active',
                                'created_at', 'updated_at'):
                        if col in cols and agency.get(col) is not None:
                            fields.append(col)
                            values.append(agency.get(col))
                    placeholders = ', '.join(['%s'] * len(fields))
                    field_sql = ', '.join(f'`{f}`' for f in fields)
                    cur.execute(
                        f'INSERT INTO agencies ({field_sql}) VALUES ({placeholders})',
                        values,
                    )
                    agency_id = agency['id']
                else:
                    cur.execute(
                        """
                        INSERT INTO agencies (name, address, email, is_active)
                        VALUES (%s, %s, %s, 1)
                        """,
                        (AGENCY_NAME, 'U.Technologies', ADMIN_FALLBACK_EMAIL),
                    )
                    agency_id = cur.lastrowid
                print(f'Restored agency id={agency_id}')

            if admin and 'users' in tables:
                cols = table_columns(cur, 'users')
                role = admin.get('role') or 'super_admin'
                if role in ('admin', 'staff'):
                    role = 'super_admin' if role == 'admin' else 'user'
                if role != 'super_admin':
                    role = 'super_admin'

                fields = ['id', 'name', 'email', 'password_hash', 'role', 'status']
                values = [
                    admin['id'],
                    admin['name'],
                    admin['email'],
                    admin['password_hash'],
                    role,
                    admin.get('status') or 'active',
                ]
                if 'phone' in cols:
                    fields.append('phone')
                    values.append(admin.get('phone'))
                if 'created_at' in cols and admin.get('created_at') is not None:
                    fields.append('created_at')
                    values.append(admin.get('created_at'))
                if 'agency_id' in cols and agency_id is not None:
                    fields.append('agency_id')
                    values.append(agency_id)

                placeholders = ', '.join(['%s'] * len(fields))
                field_sql = ', '.join(f'`{f}`' for f in fields)
                cur.execute(
                    f'INSERT INTO users ({field_sql}) VALUES ({placeholders})',
                    values,
                )
                print(f'Restored super_admin id={admin["id"]} role={role} agency_id={agency_id}')

            cur.execute('SET FOREIGN_KEY_CHECKS=1')
            conn.commit()

            if 'users' in tables:
                cur.execute('SELECT id, name, email, role, status FROM users')
                left = cur.fetchall()
                print(f'Users remaining: {len(left)}')
                for u in left:
                    print(f'  - id={u["id"]} {u["name"]!r} ({u["role"]}, {u["status"]})')

            print('Done. All business data deleted; only super admin (+ agency) kept.')
            print('Restart the app so default fuels / shop categories re-seed empty.')
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
