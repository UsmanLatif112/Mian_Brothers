"""
ONE-TIME: widen liter columns to NUMERIC(12,3).

Do NOT run this while the site is under heavy load. Prefer a quiet minute,
then restart the Python app afterward.

Usage:
  PYTHONPATH=. python scripts/migrate_liter_precision.py
"""
from app import create_app, ensure_liter_precision_schema


def main():
    # Boot app WITHOUT the migration (create_app no longer calls it).
    app = create_app()
    with app.app_context():
        print('Running liter precision migration...')
        ensure_liter_precision_schema()
        print('Done.')


if __name__ == '__main__':
    main()
