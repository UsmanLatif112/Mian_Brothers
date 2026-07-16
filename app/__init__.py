import os
from flask import Flask, redirect, url_for
from flask_login import LoginManager
from app.models import db, User

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = None
login_manager.login_message_category = 'warning'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def create_app():
    app = Flask(__name__)
    
    # Load configuration
    from app.config import Config
    app.config.from_object(Config)
    
    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)

    @app.template_filter('money')
    def money_filter(value):
        from jinja2 import Undefined
        if value is None or isinstance(value, Undefined):
            return "PKR 0.00"
        try:
            return f"PKR {float(value):,.2f}"
        except (TypeError, ValueError):
            return "PKR 0.00"

    app.jinja_env.globals['CURRENCY'] = 'PKR'
    
    # Register Blueprints
    from app.auth.routes import auth_bp
    from app.dashboard.routes import dashboard_bp
    from app.pricing.routes import pricing_bp
    from app.inventory.routes import inventory_bp
    from app.sales.routes import sales_bp
    from app.customers.routes import customers_bp
    from app.expenses import expenses_bp
    from app.purchasing import purchasing_bp
    from app.backup import backup_bp
    from app.vendors import vendors_bp
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(pricing_bp, url_prefix='/pricing')
    app.register_blueprint(inventory_bp, url_prefix='/inventory')
    app.register_blueprint(purchasing_bp, url_prefix='/purchasing')
    app.register_blueprint(sales_bp, url_prefix='/sales')
    app.register_blueprint(customers_bp, url_prefix='/customers')
    app.register_blueprint(vendors_bp, url_prefix='/vendors')
    app.register_blueprint(expenses_bp, url_prefix='/expenses')
    app.register_blueprint(backup_bp, url_prefix='/backup')
    
    # Root route redirect
    @app.route('/')
    def root():
        return redirect(url_for('dashboard.index'))
        
    # On each restart: ensure tables/schema exist. No default data is inserted —
    # fuel types, prices, inventory, machines, users, etc. are added manually.
    with app.app_context():
        db.create_all()
        ensure_inventory_schema()
        ensure_sales_schema()
        ensure_credit_sales_schema()
        ensure_journal_schema()
        ensure_customers_schema()
        ensure_vendors_schema()
        
    return app


def ensure_customers_schema():
    """Add optional old_book_no / previous_credit on customers for SQLite and MySQL."""
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    if 'customers' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('customers')}
    alters = []
    if 'old_book_no' not in existing:
        alters.append("ALTER TABLE customers ADD COLUMN old_book_no VARCHAR(50) NULL")
    if 'previous_credit' not in existing:
        alters.append("ALTER TABLE customers ADD COLUMN previous_credit NUMERIC(12, 2) NULL")

    if not alters:
        return

    with db.engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))
    print("Upgraded customers schema for old_book_no / previous_credit.")


def ensure_vendors_schema():
    """Create vendor tables/columns and backfill from existing purchase logs."""
    from sqlalchemy import text, inspect
    from app.models import Vendor, ItemPurchaseLog, StockEntry
    from app.vendors.service import (
        get_or_create_vendor,
        normalize_vendor_name,
        link_purchase_to_vendor,
        recalculate_vendor_balance,
    )

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    alters = []
    if 'item_purchase_logs' in tables:
        existing = {col['name'] for col in inspector.get_columns('item_purchase_logs')}
        if 'vendor_id' not in existing:
            alters.append('ALTER TABLE item_purchase_logs ADD COLUMN vendor_id INTEGER')
    if 'stock_entries' in tables:
        existing = {col['name'] for col in inspector.get_columns('stock_entries')}
        if 'vendor_id' not in existing:
            alters.append('ALTER TABLE stock_entries ADD COLUMN vendor_id INTEGER')

    if alters:
        with db.engine.begin() as conn:
            for stmt in alters:
                conn.execute(text(stmt))
        print('Upgraded purchase tables for vendor_id.')

    if 'vendors' not in tables:
        return

    names = set()
    if 'item_purchase_logs' in tables:
        for log in ItemPurchaseLog.query.filter(ItemPurchaseLog.vendor.isnot(None)).all():
            n = normalize_vendor_name(log.vendor)
            if n:
                names.add(n)
    if 'stock_entries' in tables:
        for entry in StockEntry.query.filter(StockEntry.supplier.isnot(None)).all():
            n = normalize_vendor_name(entry.supplier)
            if n:
                names.add(n)

    for name in names:
        get_or_create_vendor(name)
    db.session.commit()

    linked_any = False
    for log in ItemPurchaseLog.query.filter(
        ItemPurchaseLog.vendor_id.is_(None),
        ItemPurchaseLog.vendor.isnot(None),
    ).all():
        link_purchase_to_vendor(log.vendor, log, increment_balance=False)
        linked_any = True

    for entry in StockEntry.query.filter(
        StockEntry.vendor_id.is_(None),
        StockEntry.supplier.isnot(None),
    ).all():
        vendor = get_or_create_vendor(entry.supplier)
        if vendor:
            entry.vendor_id = vendor.id
            entry.supplier = vendor.name
            linked_any = True

    if linked_any or alters:
        db.session.commit()
        for vendor in Vendor.query.all():
            recalculate_vendor_balance(vendor)
        db.session.commit()
        if linked_any:
            print('Backfilled vendor links from purchase history.')


def ensure_journal_schema():
    """Add amount_paid / entry_type on credit_sales for SQLite and MySQL."""
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    if 'credit_sales' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('credit_sales')}
    alters = []
    url = str(db.engine.url)
    is_sqlite = url.startswith('sqlite')

    if 'amount_paid' not in existing:
        if is_sqlite:
            alters.append("ALTER TABLE credit_sales ADD COLUMN amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0")
        else:
            alters.append("ALTER TABLE credit_sales ADD COLUMN amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0")
    if 'entry_type' not in existing:
        if is_sqlite:
            alters.append("ALTER TABLE credit_sales ADD COLUMN entry_type VARCHAR(20) NOT NULL DEFAULT 'sale'")
        else:
            alters.append("ALTER TABLE credit_sales ADD COLUMN entry_type VARCHAR(20) NOT NULL DEFAULT 'sale'")

    if not alters:
        return

    with db.engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))
    print("Upgraded credit_sales for amount_paid / entry_type (journal).")


def ensure_inventory_schema():
    """Add new columns to other_items if upgrading from the older schema."""
    from sqlalchemy import text

    if not str(db.engine.url).startswith('sqlite'):
        return

    alters = []
    with db.engine.begin() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
        if 'other_items' not in tables:
            return

        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(other_items)"))}
        if 'category' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN category VARCHAR(20) NOT NULL DEFAULT 'other'")
        if 'company' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN company VARCHAR(100)")
        if 'item_type' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN item_type VARCHAR(100)")
        if 'vendor' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN vendor VARCHAR(100)")
        if 'cost_price' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN cost_price NUMERIC(10, 2) NOT NULL DEFAULT 0")
        if 'sale_price' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN sale_price NUMERIC(10, 2) NOT NULL DEFAULT 0")
        if 'liters' not in existing:
            alters.append("ALTER TABLE other_items ADD COLUMN liters NUMERIC(12, 2)")

        for stmt in alters:
            conn.execute(text(stmt))

    if alters:
        print("Upgraded other_items schema for category inventory.")


def ensure_sales_schema():
    """Upgrade meter_readings for daily machine workflow (nullable closing)."""
    from sqlalchemy import text

    if not str(db.engine.url).startswith('sqlite'):
        return

    with db.engine.begin() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
        if 'meter_readings' not in tables:
            return

        # PRAGMA row: (cid, name, type, notnull, dflt_value, pk)
        columns = list(conn.execute(text("PRAGMA table_info(meter_readings)")))
        existing = {row[1]: row for row in columns}
        closing_not_null = existing.get('closing_reading') and existing['closing_reading'][3] == 1
        needs_rebuild = ('machine_id' not in existing) or closing_not_null

        if not needs_rebuild:
            alters = []
            if 'closed_by' not in existing:
                alters.append("ALTER TABLE meter_readings ADD COLUMN closed_by INTEGER")
            if 'closed_at' not in existing:
                alters.append("ALTER TABLE meter_readings ADD COLUMN closed_at DATETIME")
            for stmt in alters:
                conn.execute(text(stmt))
            if alters:
                print("Upgraded meter_readings schema for machine daily sales.")
            return

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS meter_readings_new (
                id INTEGER NOT NULL PRIMARY KEY,
                machine_id INTEGER,
                dispenser_nozzle_id VARCHAR(50),
                fuel_type_id INTEGER NOT NULL,
                opening_reading NUMERIC(12, 2) NOT NULL,
                closing_reading NUMERIC(12, 2),
                liters_sold NUMERIC(12, 2),
                reading_date DATE NOT NULL,
                recorded_by INTEGER NOT NULL,
                closed_by INTEGER,
                closed_at DATETIME,
                FOREIGN KEY(machine_id) REFERENCES machines (id),
                FOREIGN KEY(fuel_type_id) REFERENCES fuel_types (id),
                FOREIGN KEY(recorded_by) REFERENCES users (id),
                FOREIGN KEY(closed_by) REFERENCES users (id)
            )
        """))

        cols = set(existing.keys())
        copyable = [
            'id', 'dispenser_nozzle_id', 'fuel_type_id', 'opening_reading',
            'closing_reading', 'liters_sold', 'reading_date', 'recorded_by'
        ]
        insert_cols = [c for c in copyable if c in cols]
        if insert_cols:
            joined = ', '.join(insert_cols)
            conn.execute(text(
                f"INSERT INTO meter_readings_new ({joined}) SELECT {joined} FROM meter_readings"
            ))

        conn.execute(text("DROP TABLE meter_readings"))
        conn.execute(text("ALTER TABLE meter_readings_new RENAME TO meter_readings"))
        print("Rebuilt meter_readings table for daily machine sales workflow.")


def ensure_credit_sales_schema():
    """Allow shop items, nullable fuel, and walk-in paid sales (nullable customer)."""
    from sqlalchemy import text

    if not str(db.engine.url).startswith('sqlite'):
        return

    with db.engine.begin() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
        if 'credit_sales' not in tables:
            return

        columns = list(conn.execute(text("PRAGMA table_info(credit_sales)")))
        existing = {row[1]: row for row in columns}
        fuel_not_null = existing.get('fuel_type_id') and existing['fuel_type_id'][3] == 1
        customer_not_null = existing.get('customer_id') and existing['customer_id'][3] == 1
        missing_other = 'other_item_id' not in existing
        needs_rebuild = fuel_not_null or customer_not_null or missing_other

        if not needs_rebuild:
            return

        if missing_other and not fuel_not_null and not customer_not_null:
            conn.execute(text("ALTER TABLE credit_sales ADD COLUMN other_item_id INTEGER"))
            print("Added other_item_id to credit_sales.")
            return

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS credit_sales_new (
                id INTEGER NOT NULL PRIMARY KEY,
                customer_id INTEGER,
                machine_id INTEGER,
                fuel_type_id INTEGER,
                other_item_id INTEGER,
                sale_date DATE NOT NULL,
                vehicle_number VARCHAR(50),
                liters NUMERIC(12, 2) NOT NULL,
                rate NUMERIC(10, 2) NOT NULL,
                amount NUMERIC(12, 2) NOT NULL,
                payment_status VARCHAR(20) NOT NULL,
                remarks VARCHAR(255),
                recorded_by INTEGER NOT NULL,
                created_at DATETIME,
                FOREIGN KEY(customer_id) REFERENCES customers (id),
                FOREIGN KEY(machine_id) REFERENCES machines (id),
                FOREIGN KEY(fuel_type_id) REFERENCES fuel_types (id),
                FOREIGN KEY(other_item_id) REFERENCES other_items (id),
                FOREIGN KEY(recorded_by) REFERENCES users (id)
            )
        """))

        has_other = 'other_item_id' in existing
        if has_other:
            conn.execute(text("""
                INSERT INTO credit_sales_new (
                    id, customer_id, machine_id, fuel_type_id, other_item_id, sale_date,
                    vehicle_number, liters, rate, amount, payment_status, remarks,
                    recorded_by, created_at
                )
                SELECT
                    id, customer_id, machine_id, fuel_type_id, other_item_id, sale_date,
                    vehicle_number, liters, rate, amount, payment_status, remarks,
                    recorded_by, created_at
                FROM credit_sales
            """))
        else:
            conn.execute(text("""
                INSERT INTO credit_sales_new (
                    id, customer_id, machine_id, fuel_type_id, other_item_id, sale_date,
                    vehicle_number, liters, rate, amount, payment_status, remarks,
                    recorded_by, created_at
                )
                SELECT
                    id, customer_id, machine_id, fuel_type_id, NULL, sale_date,
                    vehicle_number, liters, rate, amount, payment_status, remarks,
                    recorded_by, created_at
                FROM credit_sales
            """))

        conn.execute(text("DROP TABLE credit_sales"))
        conn.execute(text("ALTER TABLE credit_sales_new RENAME TO credit_sales"))
        print("Rebuilt credit_sales for shop stock sales and walk-in paid entries.")

