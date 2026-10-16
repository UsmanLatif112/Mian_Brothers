import os
import shutil
from flask import Flask, redirect, url_for, send_from_directory
from flask_login import LoginManager, current_user
from app.models import db, User

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = None
login_manager.login_message_category = 'warning'

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
UPLOAD_DIR = os.path.join(_BASE_DIR, 'uploads')


@login_manager.user_loader
def load_user(user_id):
    """Load user; recover if a prior request left the DB session invalid."""
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            return db.session.get(User, int(user_id))
        except Exception:
            return None


def media_url(filename, fallback='img/logo.png'):
    """URL for an uploaded agency logo, or static fallback."""
    if filename:
        path = os.path.join(UPLOAD_DIR, filename)
        if os.path.isfile(path):
            return url_for('uploaded_file', filename=filename)
    return url_for('static', filename=fallback)


def create_app():
    app = Flask(__name__)
    
    # Load configuration
    from app.config import Config
    app.config.from_object(Config)
    app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    
    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)

    # Request/response logs for cPanel: logs/app.log (+ stderr)
    from app.request_logging import setup_request_logging
    setup_request_logging(app)

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

    @app.context_processor
    def inject_branding():
        # Platform brand: OctaneFlow + U. Technologies + main app logo
        brand_name = 'OctaneFlow'
        brand_tag = 'U. Technologies'
        brand_logo = None  # None → static/img/logo.png
        brand_address = None
        brand_phone = None
        brand_email = None
        agency = None
        use_platform_logo = True

        def _is_octane_agency(row):
            return bool(row and 'octane' in (row.name or '').lower())

        try:
            from app.models import Agency
            if current_user.is_authenticated and getattr(current_user, 'agency_id', None):
                agency = Agency.query.get(current_user.agency_id)
                if agency:
                    brand_address = agency.address
                    brand_phone = agency.phone
                    brand_email = agency.email
                    if _is_octane_agency(agency):
                        brand_name = 'OctaneFlow'
                        brand_tag = 'U. Technologies'
                        use_platform_logo = True
                    else:
                        brand_name = agency.name or brand_name
                        brand_logo = agency.logo
                        brand_tag = ''
                        use_platform_logo = False
            elif not current_user.is_authenticated:
                # Guests: static platform brand — no Agency query (avoids MySQL
                # hit on every login/public page render under LiteSpeed).
                brand_name = 'OctaneFlow'
                brand_tag = 'U. Technologies'
                brand_address = 'U. Technologies'
                use_platform_logo = True
        except Exception:
            agency = None

        if use_platform_logo:
            logo_url = url_for('static', filename='img/logo.png')
        else:
            logo_url = media_url(brand_logo, 'img/logo.png')
        from app.tenancy import (
            is_super_admin, is_agency_admin, is_agency_manager, can_access_module, OPS_MODULES,
        )
        from app.utils import business_today, calendar_today
        wd = business_today()
        cal = calendar_today()
        return {
            'brand_name': brand_name,
            'brand_tag': brand_tag,
            'brand_logo_url': logo_url,
            'brand_favicon_url': logo_url,
            'brand_address': brand_address,
            'brand_phone': brand_phone,
            'brand_email': brand_email,
            'current_agency': agency,
            'is_super_admin': is_super_admin(),
            'is_agency_admin': is_agency_admin(),
            'is_agency_manager': is_agency_manager(),
            'can_access_module': can_access_module,
            'ops_modules': OPS_MODULES,
            'working_date': wd.isoformat(),
            'calendar_today': cal.isoformat(),
            'working_date_active': wd != cal,
        }

    @app.route('/media/uploads/<path:filename>')
    def uploaded_file(filename):
        return send_from_directory(UPLOAD_DIR, filename)
    
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
    from app.account import account_bp
    from app.journal import journal_bp
    from app.receipts import receipts_bp
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(pricing_bp, url_prefix='/pricing')
    app.register_blueprint(inventory_bp, url_prefix='/inventory')
    app.register_blueprint(purchasing_bp, url_prefix='/purchasing')
    app.register_blueprint(sales_bp, url_prefix='/sales')
    app.register_blueprint(journal_bp, url_prefix='/journal')
    app.register_blueprint(account_bp, url_prefix='/account')
    app.register_blueprint(customers_bp, url_prefix='/customers')
    app.register_blueprint(vendors_bp, url_prefix='/vendors')
    app.register_blueprint(expenses_bp, url_prefix='/expenses')
    app.register_blueprint(backup_bp, url_prefix='/backup')
    app.register_blueprint(receipts_bp, url_prefix='/receipts')

    @app.before_request
    def _recover_db_session():
        """Only clear a broken session — do NOT open a DB connection every request.

        Touching db.session.connection() on every hit made LiteSpeed workers
        hammer MySQL (spawn → query → timeout → respawn loop).
        """
        sess = db.session
        try:
            if not sess.is_active:
                sess.rollback()
        except Exception:
            try:
                sess.remove()
            except Exception:
                pass

    @app.before_request
    def enforce_module_access():
        """Team members may only open modules their agency admin granted."""
        from flask_login import current_user as cu
        from flask import request as req, flash as flash_msg
        from app.tenancy import module_for_endpoint, can_access_module as _can

        try:
            authenticated = bool(cu.is_authenticated)
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            return None

        if not authenticated:
            return None
        endpoint = req.endpoint or ''
        if endpoint.startswith('static') or endpoint in ('auth.login', 'auth.logout', 'uploaded_file', 'root'):
            return None
        # Auth users/team + backup are role-gated separately
        if endpoint.startswith('auth.') or endpoint.startswith('backup.'):
            return None
        module = module_for_endpoint(endpoint)
        if not module:
            return None
        if _can(module):
            return None
        flash_msg('You do not have access to that section. Ask your agency admin.', 'danger')
        if module != 'dashboard' and _can('dashboard'):
            return redirect(url_for('dashboard.index'))
        if _can('account'):
            return redirect(url_for('account.index'))
        return redirect(url_for('auth.logout'))

    @app.teardown_request
    def _teardown_db_session(exc):
        """Always clear the session after a request (especially after errors)."""
        try:
            if exc is not None:
                db.session.rollback()
        except Exception:
            pass
        try:
            db.session.remove()
        except Exception:
            pass

    # Root route redirect
    @app.route('/')
    def root():
        return redirect(url_for('dashboard.index'))

    # DB migrations are NEVER run from create_app / Passenger workers.
    # Run separately: PYTHONPATH=. python scripts/migrate_schema.py
    return app


def run_schema_boot():
    """
    DDL + light seed pass — only via scripts/migrate_schema.py.
    Do not call from create_app() (Passenger multi-worker locks MySQL).
    """
    db.create_all()
    ensure_agency_schema()
    ensure_inventory_schema()
    ensure_sales_schema()
    ensure_credit_sales_schema()
    ensure_journal_schema()
    ensure_customers_schema()
    ensure_vendors_schema()
    ensure_expenses_schema()
    ensure_till_schema()
    ensure_item_price_log_schema()
    try:
        ensure_default_fuel_types()
    except Exception as e:
        print(f'default fuels seed skipped: {e}')
        try:
            db.session.rollback()
        except Exception:
            pass
    try:
        from app.models import Agency
        from app.inventory.categories import ensure_shop_categories
        for ag in Agency.query.all():
            ensure_shop_categories(agency_id=ag.id)
    except Exception as e:
        print(f'shop categories seed skipped: {e}')
        try:
            db.session.rollback()
        except Exception:
            pass
    ensure_meter_sale_rate_schema()
    ensure_fuel_price_effective_at_schema()
    # Wider liter columns: scripts/migrate_liter_precision.py (optional, one-time)


def ensure_agency_schema():
    """Create agencies table, add agency_id columns, seed OctaneFlow, migrate roles."""
    from sqlalchemy import text, inspect
    from app.models import Agency

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    is_mysql = str(db.engine.url).startswith('mysql')

    # Create agencies via create_all already; ensure columns if table exists empty
    if 'agencies' not in tables:
        db.create_all()
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())

    tenant_tables = [
        'users',
        'fuel_types',
        'fuel_prices',
        'inventory',
        'stock_entries',
        'machines',
        'meter_readings',
        'credit_sales',
        'expenses',
        'daily_cash_counts',
        'cash_taken',
        'daily_till_balances',
        'daily_fuel_stocks',
        'customers',
        'sales',
        'payments',
        'vendors',
        'vendor_payments',
        'shop_categories',
        'other_items',
        'item_purchase_logs',
        'item_price_logs',
    ]

    for table in tenant_tables:
        if table not in tables:
            continue
        cols = {c['name'] for c in inspector.get_columns(table)}
        if 'agency_id' in cols:
            continue
        try:
            with db.engine.begin() as conn:
                if is_mysql:
                    conn.execute(text(
                        f"ALTER TABLE `{table}` ADD COLUMN `agency_id` INT NULL"
                    ))
                else:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN agency_id INTEGER"
                    ))
        except Exception as e:
            print(f'agency_id add skip {table}: {e}')

    # Seed default OctaneFlow agency (U.Technologies)
    agency = Agency.query.filter(Agency.name.ilike('%octane%')).first()
    if agency is None:
        agency = Agency.query.order_by(Agency.id.asc()).first()
    if agency is None:
        logo_name = None
        src = os.path.join(_BASE_DIR, 'app', 'static', 'img', 'logo.png')
        if os.path.isfile(src):
            logo_name = 'octaneflow_logo.png'
            try:
                shutil.copy2(src, os.path.join(UPLOAD_DIR, logo_name))
            except Exception as e:
                print(f'logo copy skipped: {e}')
                logo_name = None
        agency = Agency(
            name='OctaneFlow',
            address='U. Technologies',
            phone='',
            email='hello@udottechnologies.com',
            logo=logo_name,
            is_active=True,
        )
        db.session.add(agency)
        db.session.commit()
    elif agency and 'octane' in (agency.name or '').lower():
        # Keep OctaneFlow agency linked to U. Technologies + main logo file
        changed = False
        if (agency.name or '').strip() != 'OctaneFlow':
            agency.name = 'OctaneFlow'
            changed = True
        addr_norm = (agency.address or '').replace('.', '').replace(' ', '').lower()
        if not (agency.address or '').strip() or addr_norm == 'utechnologies':
            agency.address = 'U. Technologies'
            changed = True
        src = os.path.join(_BASE_DIR, 'app', 'static', 'img', 'logo.png')
        if os.path.isfile(src):
            logo_name = 'octaneflow_logo.png'
            dest = os.path.join(UPLOAD_DIR, logo_name)
            try:
                shutil.copy2(src, dest)
                if agency.logo != logo_name:
                    agency.logo = logo_name
                    changed = True
            except Exception as e:
                print(f'logo sync skipped: {e}')
        if changed:
            db.session.commit()

    aid = agency.id

    # Ensure team-member module grants column
    if 'users' in tables:
        # Re-inspect after possible earlier alters
        inspector = inspect(db.engine)
        user_cols = {c['name'] for c in inspector.get_columns('users')}
        if 'allowed_modules' not in user_cols:
            try:
                with db.engine.begin() as conn:
                    if is_mysql:
                        conn.execute(text(
                            "ALTER TABLE `users` ADD COLUMN `allowed_modules` VARCHAR(500) NULL DEFAULT '*'"
                        ))
                    else:
                        conn.execute(text(
                            "ALTER TABLE users ADD COLUMN allowed_modules VARCHAR(500) DEFAULT '*'"
                        ))
            except Exception as e:
                print(f'allowed_modules add skip: {e}')
                try:
                    db.session.rollback()
                except Exception:
                    pass

    # Migrate legacy roles + attach users (do NOT force admin → super_admin;
    # admin is the agency-owner role).
    try:
        inspector = inspect(db.engine)
        user_cols = {c['name'] for c in inspector.get_columns('users')} if 'users' in inspector.get_table_names() else set()
        has_allowed = 'allowed_modules' in user_cols
        for user in User.query.all():
            changed = False
            if user.role == 'staff':
                user.role = 'user'
                changed = True
            if user.agency_id is None and user.role != 'super_admin':
                user.agency_id = aid
                changed = True
            if has_allowed and user.role == 'user' and not getattr(user, 'allowed_modules', None):
                user.allowed_modules = '*'
                changed = True
            if changed:
                db.session.add(user)
        db.session.commit()
    except Exception as e:
        print(f'user role/agency migrate skip: {e}')
        try:
            db.session.rollback()
        except Exception:
            pass

    # Backfill agency_id on tenant rows
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    for table in tenant_tables:
        if table == 'users' or table not in tables:
            continue
        cols = {c['name'] for c in inspector.get_columns(table)}
        if 'agency_id' not in cols:
            continue
        try:
            with db.engine.begin() as conn:
                conn.execute(text(
                    f"UPDATE {table} SET agency_id = :aid WHERE agency_id IS NULL"
                ), {'aid': aid})
        except Exception as e:
            print(f'agency backfill skip {table}: {e}')

    # Relax legacy global unique indexes so per-agency names can coexist
    _drop_legacy_unique_indexes(is_mysql)


def _drop_legacy_unique_indexes(is_mysql):
    """Best-effort drop of old single-column unique indexes that block multi-agency."""
    from sqlalchemy import text, inspect

    # (table, likely index / constraint names)
    targets = [
        ('fuel_types', ['name', 'fuel_types_name_key', 'ix_fuel_types_name']),
        ('vendors', ['name', 'vendors_name_key', 'ix_vendors_name']),
        ('machines', ['name', 'machines_name_key', 'ix_machines_name']),
        ('shop_categories', ['key', 'shop_categories_key_key', 'ix_shop_categories_key']),
        ('inventory', ['fuel_type_id', 'inventory_fuel_type_id_key', 'ix_inventory_fuel_type_id']),
        ('daily_cash_counts', ['count_date', 'daily_cash_counts_count_date_key']),
        ('daily_till_balances', ['balance_date', 'daily_till_balances_balance_date_key']),
    ]

    try:
        with db.engine.begin() as conn:
            if is_mysql:
                for table, names in targets:
                    for idx in names:
                        try:
                            conn.execute(text(f"ALTER TABLE `{table}` DROP INDEX `{idx}`"))
                        except Exception:
                            pass
                # Discover leftover UNIQUE indexes on inventory.fuel_type_id alone
                try:
                    rows = conn.execute(text("SHOW INDEX FROM `inventory` WHERE Non_unique = 0")).mappings().all()
                    by_key = {}
                    for row in rows:
                        key = row.get('Key_name')
                        if not key or key == 'PRIMARY':
                            continue
                        by_key.setdefault(key, []).append(row.get('Column_name'))
                    for key, cols in by_key.items():
                        if cols == ['fuel_type_id']:
                            try:
                                conn.execute(text(f"ALTER TABLE `inventory` DROP INDEX `{key}`"))
                                print(f'Dropped legacy unique index inventory.{key}')
                            except Exception:
                                pass
                except Exception as e:
                    print(f'inventory unique scan skipped: {e}')
            else:
                for table, names in targets:
                    for idx in names:
                        try:
                            conn.execute(text(f'DROP INDEX IF EXISTS "{idx}"'))
                        except Exception:
                            pass
    except Exception as e:
        print(f'unique index drop skipped: {e}')

    # Ensure composite unique exists for inventory (agency_id, fuel_type_id)
    try:
        if is_mysql:
            inspector = inspect(db.engine)
            if 'inventory' in inspector.get_table_names():
                uniques = {tuple(u.get('column_names') or []) for u in inspector.get_unique_constraints('inventory')}
                if ('agency_id', 'fuel_type_id') not in uniques and ('fuel_type_id', 'agency_id') not in uniques:
                    with db.engine.begin() as conn:
                        try:
                            conn.execute(text(
                                "ALTER TABLE `inventory` "
                                "ADD UNIQUE KEY `uq_inventory_agency_fuel` (`agency_id`, `fuel_type_id`)"
                            ))
                        except Exception:
                            pass
    except Exception as e:
        print(f'inventory composite unique skipped: {e}')


def ensure_liter_precision_schema():
    """Allow up to 3 decimal places on liter/qty volume columns (e.g. 25.002).

    Uses a short MySQL lock wait so a blocked ALTER cannot hang the whole app boot.
    """
    from sqlalchemy import text, inspect

    url = str(db.engine.url)
    is_mysql = url.startswith('mysql')
    if not is_mysql:
        return  # SQLite affinity ignores numeric scale

    # table -> [(column, nullability)]
    targets = {
        'credit_sales': [('liters', 'NOT NULL')],
        'meter_readings': [
            ('opening_reading', 'NOT NULL'),
            ('closing_reading', 'NULL'),
            ('liters_sold', 'NULL'),
        ],
        'inventory': [
            ('current_stock_liters', 'NOT NULL'),
            ('reorder_threshold', 'NOT NULL'),
        ],
        'other_items': [('liters', 'NULL')],
        'stock_entries': [('liters_added', 'NOT NULL')],
        'item_purchase_logs': [('liters', 'NULL')],
        'sales': [('liters', 'NOT NULL')],
    }

    try:
        with db.engine.begin() as conn:
            # Fail fast if another session holds a metadata lock (common under Passenger).
            conn.execute(text('SET SESSION lock_wait_timeout = 3'))
            conn.execute(text('SET SESSION innodb_lock_wait_timeout = 3'))

            for table, cols in targets.items():
                for col_name, null_sql in cols:
                    row = conn.execute(text(
                        """
                        SELECT NUMERIC_SCALE
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME = :table
                          AND COLUMN_NAME = :col
                        """
                    ), {'table': table, 'col': col_name}).first()
                    if not row:
                        continue
                    scale = row[0]
                    if scale is not None and int(scale) >= 3:
                        continue
                    try:
                        conn.execute(text(
                            f"ALTER TABLE `{table}` MODIFY COLUMN `{col_name}` "
                            f"NUMERIC(12, 3) {null_sql}"
                        ))
                    except Exception as e:
                        # Don't block app start — retry on next boot if lock/timeout.
                        print(f"liter precision skip {table}.{col_name}: {e}")
    except Exception as e:
        print(f"liter precision migration skipped: {e}")


def ensure_fuel_price_effective_at_schema():
    """Add effective_at on fuel_prices for same-day mid-day price changes."""
    from sqlalchemy import text, inspect
    from datetime import datetime, time as dt_time

    inspector = inspect(db.engine)
    if 'fuel_prices' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('fuel_prices')}
    if 'effective_at' not in existing:
        with db.engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE fuel_prices ADD COLUMN effective_at DATETIME NULL"
            ))

    from app.models import FuelPrice
    rows = FuelPrice.query.filter(FuelPrice.effective_at.is_(None)).all()
    if not rows:
        return
    for row in rows:
        day = row.effective_date
        if day is None and row.created_at:
            day = row.created_at.date() if hasattr(row.created_at, 'date') else row.created_at
        if day is None:
            continue
        if row.created_at and hasattr(row.created_at, 'time'):
            if hasattr(row.created_at, 'date') and row.created_at.date() == day:
                row.effective_at = row.created_at.replace(microsecond=0)
            else:
                row.effective_at = datetime.combine(day, row.created_at.time().replace(microsecond=0))
        else:
            row.effective_at = datetime.combine(day, dt_time.min)
    db.session.commit()


def ensure_default_fuel_types(agency_id=None):
    """Ensure Petrol and Diesel fuel types (+ inventory rows) exist per agency.

    Safe with legacy UNIQUE(inventory.fuel_type_id): never insert a second
    inventory row for the same fuel_type_id.
    """
    from sqlalchemy.exc import IntegrityError
    from app.models import FuelType, Inventory, Agency

    if agency_id is not None:
        agencies = [agency_id]
    else:
        agencies = [a.id for a in Agency.query.all()]
        if not agencies:
            return

    defaults = ('Petrol', 'Diesel')
    created = False

    try:
        with db.session.no_autoflush:
            for aid in agencies:
                for name in defaults:
                    fuel = (
                        FuelType.query
                        .filter(
                            db.func.lower(FuelType.name) == name.lower(),
                            FuelType.agency_id == aid,
                        )
                        .first()
                    )
                    if fuel is None:
                        shared = (
                            FuelType.query
                            .filter(db.func.lower(FuelType.name) == name.lower())
                            .order_by(FuelType.id.asc())
                            .first()
                        )
                        if shared is None:
                            fuel = FuelType(name=name, unit='Liter', agency_id=aid)
                            db.session.add(fuel)
                            try:
                                db.session.flush()
                                created = True
                            except IntegrityError:
                                db.session.rollback()
                                fuel = (
                                    FuelType.query
                                    .filter(db.func.lower(FuelType.name) == name.lower())
                                    .order_by(FuelType.id.asc())
                                    .first()
                                )
                        else:
                            fuel = shared
                            if fuel.agency_id is not None and fuel.agency_id != aid:
                                fuel.agency_id = None  # shared catalog
                                created = True

                    if fuel is None:
                        continue

                    # Any existing inventory for this fuel (legacy unique is on fuel_type_id)
                    inv = (
                        Inventory.query
                        .filter_by(fuel_type_id=fuel.id)
                        .order_by(Inventory.id.asc())
                        .first()
                    )
                    if inv is None:
                        db.session.add(Inventory(
                            fuel_type_id=fuel.id,
                            agency_id=aid,
                            current_stock_liters=0,
                            reorder_threshold=0,
                        ))
                        created = True
                    elif inv.agency_id is None:
                        inv.agency_id = aid
                        created = True
                    # else: row already exists — do not insert again

        if created:
            db.session.commit()
    except IntegrityError:
        db.session.rollback()
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f'ensure_default_fuel_types skipped: {e}')


def ensure_meter_sale_rate_schema():
    """Add sale_rate on meter_readings for mid-day price segment snapshots."""
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    if 'meter_readings' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('meter_readings')}
    if 'sale_rate' in existing:
        return

    with db.engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE meter_readings ADD COLUMN sale_rate NUMERIC(10, 2) NULL"
        ))


def ensure_item_price_log_schema():
    """Add effective_date to item_price_logs (SQLite + MySQL)."""
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    if 'item_price_logs' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('item_price_logs')}
    if 'effective_date' in existing:
        return

    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE item_price_logs ADD COLUMN effective_date DATE NULL"))

    # Backfill from created_at where possible
    with db.engine.begin() as conn:
        url = str(db.engine.url)
        if url.startswith('sqlite'):
            conn.execute(text(
                "UPDATE item_price_logs SET effective_date = date(created_at) "
                "WHERE effective_date IS NULL AND created_at IS NOT NULL"
            ))
        else:
            conn.execute(text(
                "UPDATE item_price_logs SET effective_date = DATE(created_at) "
                "WHERE effective_date IS NULL AND created_at IS NOT NULL"
            ))


def ensure_price_history_seed():
    """
    Idempotent fuel price history for Jul 2026 revision + initial Other/FT logs.

    Petrol/Diesel:
      2026-07-12 → 312.51 / 325.80
      2026-07-18 → 317.90 / 356.90

    Rows with effective_date after 2026-07-18 are left untouched.
    """
    from datetime import date, datetime
    from app.models import FuelType, FuelPrice, OtherItem, ItemPriceLog, User

    seed_day = date(2026, 7, 12)
    revise_day = date(2026, 7, 18)

    user = (
        User.query.filter(User.role.in_(('super_admin', 'admin')))
        .order_by(User.id.asc())
        .first()
    )
    if user is None:
        user = User.query.order_by(User.id.asc()).first()
    if user is None:
        return
    user_id = user.id

    fuel_seeds = {
        'petrol': [
            (seed_day, 312.51),
            (revise_day, 317.90),
        ],
        'diesel': [
            (seed_day, 325.80),
            (revise_day, 356.90),
        ],
    }

    for name_key, revisions in fuel_seeds.items():
        fuel = (
            FuelType.query
            .filter(FuelType.name.ilike(f'%{name_key}%'))
            .order_by(FuelType.id.asc())
            .first()
        )
        if not fuel:
            continue

        wanted = {eff: price for eff, price in revisions}
        # Keep only the seeded dates through revise_day; preserve later user revisions
        seen_dates = set()
        for row in FuelPrice.query.filter(
            FuelPrice.fuel_type_id == fuel.id,
            FuelPrice.effective_date <= revise_day,
        ).all():
            expected = wanted.get(row.effective_date)
            if expected is None:
                db.session.delete(row)
                continue
            seen_dates.add(row.effective_date)
            if abs(float(row.price_per_liter or 0) - float(expected)) >= 0.001:
                row.price_per_liter = expected

        for eff, price in revisions:
            if eff in seen_dates:
                continue
            db.session.add(FuelPrice(
                fuel_type_id=fuel.id,
                price_per_liter=price,
                effective_date=eff,
                updated_by=user_id,
                created_at=datetime.combine(eff, datetime.min.time()),
            ))

    # Initial Other/FT price logs if an item has none yet
    for item in OtherItem.query.all():
        logs = ItemPriceLog.query.filter_by(other_item_id=item.id).all()
        if logs:
            for log in logs:
                if log.effective_date is None:
                    log.effective_date = (
                        log.created_at.date()
                        if log.created_at and hasattr(log.created_at, 'date')
                        else seed_day
                    )
            continue
        db.session.add(ItemPriceLog(
            other_item_id=item.id,
            sale_price=float(item.sale_price or 0),
            cost_price=float(item.cost_price or 0) if item.cost_price is not None else None,
            effective_date=seed_day,
            updated_by=user_id,
            created_at=datetime.combine(seed_day, datetime.min.time()),
        ))

    db.session.commit()


def ensure_till_schema():
    """Create / upgrade till carry-forward tables."""
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    if 'cash_taken' not in tables:
        return

    with db.engine.begin() as conn:
        cash_cols = {col['name'] for col in inspector.get_columns('cash_taken')}
        cash_alters = []
        if 'taken_date' not in cash_cols:
            cash_alters.append("ALTER TABLE cash_taken ADD COLUMN taken_date DATE")
        if 'amount' not in cash_cols:
            cash_alters.append("ALTER TABLE cash_taken ADD COLUMN amount NUMERIC(12, 2) NOT NULL DEFAULT 0")
        if 'person_name' not in cash_cols:
            cash_alters.append("ALTER TABLE cash_taken ADD COLUMN person_name VARCHAR(120) NOT NULL DEFAULT ''")
        if 'note' not in cash_cols:
            cash_alters.append("ALTER TABLE cash_taken ADD COLUMN note VARCHAR(255)")
        if 'recorded_by' not in cash_cols:
            cash_alters.append("ALTER TABLE cash_taken ADD COLUMN recorded_by INTEGER")
        if 'created_at' not in cash_cols:
            cash_alters.append("ALTER TABLE cash_taken ADD COLUMN created_at DATETIME")

        for stmt in cash_alters:
            conn.execute(text(stmt))

        if 'daily_till_balances' in tables:
            bal_cols = {col['name'] for col in inspector.get_columns('daily_till_balances')}
            bal_alters = []
            if 'balance_date' not in bal_cols:
                bal_alters.append("ALTER TABLE daily_till_balances ADD COLUMN balance_date DATE")
            if 'previous_balance' not in bal_cols:
                bal_alters.append("ALTER TABLE daily_till_balances ADD COLUMN previous_balance NUMERIC(12, 2) NOT NULL DEFAULT 0")
            if 'remaining_balance' not in bal_cols:
                bal_alters.append("ALTER TABLE daily_till_balances ADD COLUMN remaining_balance NUMERIC(12, 2) NOT NULL DEFAULT 0")
            if 'updated_at' not in bal_cols:
                bal_alters.append("ALTER TABLE daily_till_balances ADD COLUMN updated_at DATETIME")
            if 'updated_by' not in bal_cols:
                bal_alters.append("ALTER TABLE daily_till_balances ADD COLUMN updated_by INTEGER")

            for stmt in bal_alters:
                conn.execute(text(stmt))


def ensure_expenses_schema():
    """Add settle/return-to-till columns on expenses for SQLite and MySQL."""
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    if 'expenses' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('expenses')}
    alters = []
    url = str(db.engine.url)
    is_sqlite = url.startswith('sqlite')

    if 'is_settled' not in existing:
        if is_sqlite:
            alters.append("ALTER TABLE expenses ADD COLUMN is_settled BOOLEAN NOT NULL DEFAULT 0")
        else:
            alters.append("ALTER TABLE expenses ADD COLUMN is_settled TINYINT(1) NOT NULL DEFAULT 0")
    if 'settled_date' not in existing:
        alters.append("ALTER TABLE expenses ADD COLUMN settled_date DATE NULL")
    if 'settled_by' not in existing:
        alters.append("ALTER TABLE expenses ADD COLUMN settled_by INTEGER NULL")
    if 'settle_note' not in existing:
        alters.append("ALTER TABLE expenses ADD COLUMN settle_note VARCHAR(255) NULL")
    if 'settle_method' not in existing:
        alters.append("ALTER TABLE expenses ADD COLUMN settle_method VARCHAR(20) NOT NULL DEFAULT 'cash'")
    if 'settle_customer_id' not in existing:
        alters.append("ALTER TABLE expenses ADD COLUMN settle_customer_id INTEGER NULL")
    if 'settle_credit_sale_id' not in existing:
        alters.append("ALTER TABLE expenses ADD COLUMN settle_credit_sale_id INTEGER NULL")

    if not alters:
        return

    with db.engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))
        # One-time repair only when settle columns were just added
        try:
            conn.execute(text(
                "UPDATE expenses SET is_settled = 1 "
                "WHERE settled_date IS NOT NULL AND (is_settled = 0 OR is_settled IS NULL)"
            ))
        except Exception:
            pass
    print("Upgraded expenses schema for settle / return-to-till.")


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
    """Ensure vendor_id columns exist. Heavy history backfill is one-time only.

    Never scan purchase logs on every boot — that can hang the whole app if
    MySQL holds a metadata lock on item_purchase_logs (e.g. stuck ALTER).
    """
    from sqlalchemy import text, inspect

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    alters = []
    if 'item_purchase_logs' in tables:
        existing = {col['name'] for col in inspector.get_columns('item_purchase_logs')}
        if 'vendor_id' not in existing:
            alters.append('ALTER TABLE item_purchase_logs ADD COLUMN vendor_id INTEGER')
        if 'payment_status' not in existing:
            alters.append(
                "ALTER TABLE item_purchase_logs ADD COLUMN payment_status VARCHAR(20) NOT NULL DEFAULT 'unpaid'"
            )
    if 'stock_entries' in tables:
        existing = {col['name'] for col in inspector.get_columns('stock_entries')}
        if 'vendor_id' not in existing:
            alters.append('ALTER TABLE stock_entries ADD COLUMN vendor_id INTEGER')
    if 'vendor_payments' in tables:
        existing = {col['name'] for col in inspector.get_columns('vendor_payments')}
        if 'purchase_log_id' not in existing:
            alters.append('ALTER TABLE vendor_payments ADD COLUMN purchase_log_id INTEGER')

    if not alters:
        return

    try:
        with db.engine.begin() as conn:
            if str(db.engine.url).startswith('mysql'):
                conn.execute(text('SET SESSION lock_wait_timeout = 3'))
            for stmt in alters:
                conn.execute(text(stmt))
        print('Upgraded vendor purchase schema.')
    except Exception as e:
        print(f'vendor schema upgrade skipped: {e}')


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
    if 'discount' not in existing:
        alters.append("ALTER TABLE credit_sales ADD COLUMN discount NUMERIC(12, 2) NOT NULL DEFAULT 0")
    if 'overpayment' not in existing:
        alters.append("ALTER TABLE credit_sales ADD COLUMN overpayment NUMERIC(12, 2) NOT NULL DEFAULT 0")

    if not alters:
        return

    with db.engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))
    print("Upgraded credit_sales for amount_paid / entry_type / discount / overpayment (journal).")


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

