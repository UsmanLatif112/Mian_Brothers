"""
Wipe ALL business data. Keep:
  - all users
  - default fuel types Petrol + Diesel (with empty inventory)

Deletes sales, purchases, vendors, customers, machines, prices, expenses, till, etc.

Usage (on server, inside app venv):
  PYTHONPATH=. python scripts/wipe_keep_users_fuels.py
"""
from sqlalchemy import text, inspect

from app import create_app, ensure_default_fuel_types
from app.models import db, FuelType, Inventory


# Tables that must stay (rows kept or restored after wipe)
KEEP_TABLES = {'users'}


def main():
    app = create_app()
    with app.app_context():
        url = str(db.engine.url)
        print(f"Using database: {url.split('@')[-1] if '@' in url else url}")

        is_mysql = url.startswith('mysql')
        if is_mysql:
            db.session.execute(text('SET FOREIGN_KEY_CHECKS=0'))

        # Clear every mapped table except users
        for table in reversed(db.metadata.sorted_tables):
            if table.name in KEEP_TABLES:
                print(f"  kept {table.name}")
                continue
            deleted = db.session.execute(table.delete()).rowcount
            print(f"  cleared {table.name}: {deleted} rows")

        # Also clear any extra tables (e.g. sms_*) that models don't map
        inspector = inspect(db.engine)
        mapped = {t.name for t in db.metadata.sorted_tables}
        for name in inspector.get_table_names():
            if name in KEEP_TABLES or name in mapped:
                continue
            try:
                deleted = db.session.execute(text(f'DELETE FROM `{name}`')).rowcount
                print(f"  cleared extra {name}: {deleted} rows")
            except Exception as e:
                print(f"  skip {name}: {e}")

        if is_mysql:
            db.session.execute(text('SET FOREIGN_KEY_CHECKS=1'))

        db.session.commit()

        # Restore Petrol + Diesel (+ zero inventory)
        ensure_default_fuel_types()

        fuels = FuelType.query.order_by(FuelType.name.asc()).all()
        for fuel in fuels:
            inv = Inventory.query.filter_by(fuel_type_id=fuel.id).first()
            stock = float(inv.current_stock_liters) if inv else 0
            print(f"  fuel ready: {fuel.name} (stock {stock:.2f} L)")

        from app.models import User
        users = User.query.order_by(User.id.asc()).all()
        print(f"Users kept: {len(users)}")
        for u in users:
            print(f"  - {u.name!r} ({u.role}, {u.status})")

        print('Done. All history cleared. Users + Petrol/Diesel kept.')


if __name__ == '__main__':
    main()
