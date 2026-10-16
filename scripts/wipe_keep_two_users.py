"""
Wipe ALL business data. Keep only:
  - U. Technologies (super_admin) + OctaneFlow agency
  - Mian_filling_station (admin) + Mian Filling Station agency

Usage:
  PYTHONPATH=. python scripts/wipe_keep_two_users.py
"""
from __future__ import annotations

import os
import sys

import pymysql
from dotenv import load_dotenv

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
load_dotenv(os.path.join(_BASE, '.env'))

KEEP_USER_MATCHERS = (
    {'name': 'U. Technologies', 'email': 'admin@udottechnologies.com'},
    {'name': 'Mian_filling_station', 'email': 'mian_filling_station@gmail.com'},
)


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
        read_timeout=300,
        write_timeout=300,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def table_columns(cur, table: str) -> set[str]:
    cur.execute(f'SHOW COLUMNS FROM `{table}`')
    return {row['Field'] for row in cur.fetchall()}


def match_user(row: dict) -> bool:
    name = (row.get('name') or '').strip()
    email = (row.get('email') or '').strip().lower()
    for m in KEEP_USER_MATCHERS:
        if name == m['name'] or email == m['email'].lower():
            return True
    return False


def insert_row(cur, table: str, row: dict, only_cols: set[str] | None = None):
    cols = table_columns(cur, table)
    if only_cols:
        cols = cols & only_cols
    fields = [c for c in row.keys() if c in cols and row.get(c) is not None]
    # Always allow nullable fields that exist on the snapshot
    for c in row.keys():
        if c in (only_cols or table_columns(cur, table)) and c not in fields:
            # keep explicit Nones out except agency_id / allowed_modules etc. already handled
            pass
    # Prefer all columns present in both snapshot and table
    cols = table_columns(cur, table)
    fields = [c for c in row.keys() if c in cols]
    values = [row.get(c) for c in fields]
    placeholders = ', '.join(['%s'] * len(fields))
    field_sql = ', '.join(f'`{f}`' for f in fields)
    cur.execute(
        f'INSERT INTO `{table}` ({field_sql}) VALUES ({placeholders})',
        values,
    )


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

            keep_users = []
            if 'users' in tables:
                cur.execute('SELECT * FROM users ORDER BY id ASC')
                for row in cur.fetchall():
                    if match_user(row):
                        keep_users.append(row)
                        print(
                            f'Keeping user: id={row["id"]} name={row["name"]!r} '
                            f'email={row["email"]!r} role={row.get("role")!r} '
                            f'agency_id={row.get("agency_id")!r}'
                        )

            if len(keep_users) < 2:
                print(
                    f'WARNING: expected 2 keep-users, found {len(keep_users)}. Aborting.',
                    file=sys.stderr,
                )
                sys.exit(2)

            agency_ids = {
                int(u['agency_id'])
                for u in keep_users
                if u.get('agency_id') is not None
            }
            keep_agencies = []
            if 'agencies' in tables and agency_ids:
                cur.execute(
                    f"SELECT * FROM agencies WHERE id IN ({','.join(['%s'] * len(agency_ids))})",
                    list(agency_ids),
                )
                keep_agencies = cur.fetchall()
                for a in keep_agencies:
                    print(f'Keeping agency: id={a["id"]} name={a["name"]!r}')

            if not keep_agencies:
                print('WARNING: no agencies to restore for kept users.', file=sys.stderr)

            print('Wiping all tables…')
            cur.execute('SET FOREIGN_KEY_CHECKS=0')
            for table in tables:
                cur.execute(f'DELETE FROM `{table}`')
                print(f'  cleared {table}: {cur.rowcount} rows')

            if 'agencies' in tables:
                for agency in keep_agencies:
                    insert_row(cur, 'agencies', agency)
                    print(f'Restored agency id={agency["id"]} name={agency["name"]!r}')

            if 'users' in tables:
                for user in keep_users:
                    # Ensure roles stay as they were (super_admin / admin)
                    insert_row(cur, 'users', user)
                    print(
                        f'Restored user id={user["id"]} name={user["name"]!r} '
                        f'role={user.get("role")!r}'
                    )

            cur.execute('SET FOREIGN_KEY_CHECKS=1')
            conn.commit()

            cur.execute('SELECT id, name, email, role, status, agency_id FROM users')
            left = cur.fetchall()
            print(f'Users remaining: {len(left)}')
            for u in left:
                print(
                    f'  - id={u["id"]} {u["name"]!r} ({u["role"]}, {u["status"]}, '
                    f'agency_id={u["agency_id"]})'
                )

            if 'agencies' in tables:
                cur.execute('SELECT id, name FROM agencies')
                for a in cur.fetchall():
                    print(f'  agency: id={a["id"]} {a["name"]!r}')

            print('Done. All business data deleted; only the two users (+ agencies) kept.')
            print('Run: PYTHONPATH=. python scripts/migrate_schema.py  (re-seed fuels/categories)')
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
