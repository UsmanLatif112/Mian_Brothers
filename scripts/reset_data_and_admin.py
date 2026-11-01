"""
Wipe all application data and create the main super_admin user.

Usage:
  PYTHONPATH=. python scripts/reset_data_and_admin.py
"""
from sqlalchemy import text

from app import create_app, ensure_default_fuel_types
from app.models import db, User, Agency


ADMIN_NAME = 'U. Technologies'
ADMIN_EMAIL = 'admin@udottechnologies.com'
ADMIN_PASSWORD = 'DSJoker@@03360325'
AGENCY_NAME = 'OctaneFlow'


def main():
    app = create_app()
    with app.app_context():
        url = str(db.engine.url)
        print(f'Using database: {url.split("@")[-1] if "@" in url else url}')

        is_mysql = url.startswith('mysql')
        if is_mysql:
            db.session.execute(text('SET FOREIGN_KEY_CHECKS=0'))

        for table in reversed(db.metadata.sorted_tables):
            deleted = db.session.execute(table.delete()).rowcount
            print(f'  cleared {table.name}: {deleted} rows')

        if is_mysql:
            db.session.execute(text('SET FOREIGN_KEY_CHECKS=1'))

        db.session.commit()

        agency = Agency(
            name=AGENCY_NAME,
            address='U. Technologies',
            phone='',
            email='hello@udottechnologies.com',
            is_active=True,
        )
        db.session.add(agency)
        db.session.flush()

        admin = User(
            name=ADMIN_NAME,
            email=ADMIN_EMAIL,
            phone=None,
            role='super_admin',
            status='active',
            agency_id=agency.id,
            allowed_modules='*',
        )
        admin.set_password(ADMIN_PASSWORD)
        db.session.add(admin)
        db.session.commit()

        ensure_default_fuel_types(agency_id=agency.id)

        try:
            from app.inventory.categories import ensure_shop_categories
            ensure_shop_categories(agency_id=agency.id)
        except Exception as e:
            print(f'shop categories seed skipped: {e}')

        print('All data deleted.')
        print(f'Super admin: {ADMIN_NAME}')
        print(f'Login username: {ADMIN_NAME}')
        print(f'Account email:  {ADMIN_EMAIL}')
        print(f'Agency: {AGENCY_NAME} (id={agency.id})')
        print('Login password: (as provided)')


if __name__ == '__main__':
    main()
