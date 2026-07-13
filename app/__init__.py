import os
from flask import Flask, redirect, url_for
from flask_login import LoginManager
from app.models import (
    db, User, FuelType, FuelPrice, Inventory, SMSTemplate,
    OtherItem, ItemPurchaseLog, ItemPriceLog, Machine
)

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
    from app.sms.routes import sms_bp
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(pricing_bp, url_prefix='/pricing')
    app.register_blueprint(inventory_bp, url_prefix='/inventory')
    app.register_blueprint(sales_bp, url_prefix='/sales')
    app.register_blueprint(customers_bp, url_prefix='/customers')
    app.register_blueprint(sms_bp, url_prefix='/sms')
    
    # Root route redirect
    @app.route('/')
    def root():
        return redirect(url_for('dashboard.index'))
        
    # Database seeding and initialization
    with app.app_context():
        # Create database tables if they do not exist
        db.create_all()
        ensure_inventory_schema()
        ensure_sales_schema()
        ensure_credit_sales_schema()
        
        # Seed initial data if database is empty
        seed_database()
        
    return app


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


def seed_database():
    # 1. Seed users
    if not User.query.first():
        # Create admin
        admin = User(
            name="Admin User",
            email="admin@fuel.com",
            role="admin",
            phone="1234567890",
            status="active"
        )
        admin.set_password("admin123")
        
        # Create staff
        staff = User(
            name="Staff Operator",
            email="staff@fuel.com",
            role="staff",
            phone="0987654321",
            status="active"
        )
        staff.set_password("staff123")
        
        db.session.add(admin)
        db.session.add(staff)
        db.session.commit()
        
        # Refresh to get IDs
        admin = User.query.filter_by(role='admin').first()
        
        # 2. Seed fuel types
        petrol = FuelType(name="Petrol", unit="Liter")
        diesel = FuelType(name="Diesel", unit="Liter")
        db.session.add(petrol)
        db.session.add(diesel)
        db.session.commit()
        
        # 3. Seed prices
        p_price = FuelPrice(fuel_type_id=petrol.id, price_per_liter=1.45, updated_by=admin.id)
        d_price = FuelPrice(fuel_type_id=diesel.id, price_per_liter=1.32, updated_by=admin.id)
        db.session.add(p_price)
        db.session.add(d_price)
        
        # 4. Seed initial inventory
        p_inv = Inventory(fuel_type_id=petrol.id, current_stock_liters=10000.00, reorder_threshold=1500.00)
        d_inv = Inventory(fuel_type_id=diesel.id, current_stock_liters=10000.00, reorder_threshold=1500.00)
        db.session.add(p_inv)
        db.session.add(d_inv)
        
        # 5. Seed templates
        templates = [
            SMSTemplate(
                type="receipt",
                template_text="Dear {{name}}, thank you for purchasing {{liters}}L of {{fuel}} for PKR {{amount}}. Your outstanding balance is PKR {{due}}.",
                created_by=admin.id
            ),
            SMSTemplate(
                type="due_reminder",
                template_text="Dear {{name}}, this is a friendly reminder that you have a pending credit balance of PKR {{due}} with our fuel station. Please settle it at your earliest convenience.",
                created_by=admin.id
            ),
            SMSTemplate(
                type="offer",
                template_text="Hello {{name}}! Refuel today and get 5% cashback on your next purchase. Valid till end of the month.",
                created_by=admin.id
            ),
            SMSTemplate(
                type="price_update",
                template_text="Dear Customer, new fuel prices are active from {{date}}. Petrol: PKR {{petrol}}/L, Diesel: PKR {{diesel}}/L.",
                created_by=admin.id
            )
        ]
        db.session.add_all(templates)
        db.session.commit()
        print("Database successfully seeded with default users, fuels, prices, stock, and SMS templates!")

    # Seed other shop items (runs even if users already exist)
    if not OtherItem.query.first():
        other_items = [
            OtherItem(category='mobile', name='Engine Oil 1L', company='Shell', item_type='5W-30', vendor='Shell Distributor', cost_price=4.50, sale_price=6.00, liters=1.00, quantity=48),
            OtherItem(category='mobile', name='Coolant 1L', company='Castrol', item_type='Green Coolant', vendor='Castrol Dealer', cost_price=3.00, sale_price=4.50, liters=1.00, quantity=36),
            OtherItem(category='filter', name='Oil Filter', company='Bosch', item_type='Oil Filter', vendor='Bosch Parts', cost_price=2.50, sale_price=4.00, quantity=24),
            OtherItem(category='other', name='Air Freshener', company=None, item_type=None, vendor='Local Supplier', cost_price=0.80, sale_price=1.50, quantity=60),
            OtherItem(category='other', name='Bottled Water', company=None, item_type=None, vendor='Local Supplier', cost_price=0.20, sale_price=0.50, quantity=120),
        ]
        db.session.add_all(other_items)
        db.session.commit()
        print("Other items inventory seeded!")

    # Seed dispensing machines
    if not Machine.query.first():
        petrol = FuelType.query.filter_by(name='Petrol').first()
        diesel = FuelType.query.filter_by(name='Diesel').first()
        if petrol and diesel:
            machines = [
                Machine(name='Petrol Machine 1', fuel_type_id=petrol.id),
                Machine(name='Petrol Machine 2', fuel_type_id=petrol.id),
                Machine(name='Diesel Machine 1', fuel_type_id=diesel.id),
                Machine(name='Diesel Machine 2', fuel_type_id=diesel.id),
            ]
            db.session.add_all(machines)
            db.session.commit()
            print("Dispensing machines seeded!")
