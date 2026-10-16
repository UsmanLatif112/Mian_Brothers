"""Light re-seed after wipe_keep_two_users: Petrol/Diesel + shop categories per agency."""
from app import create_app, ensure_default_fuel_types
from app.models import db, Agency
from app.inventory.categories import (
    ensure_other_item_stock_unit_column,
    ensure_shop_categories,
)


def main():
    app = create_app()
    with app.app_context():
        print('Ensuring stock_unit column…')
        try:
            ensure_other_item_stock_unit_column()
        except Exception as e:
            print(f'  stock_unit: {e}')

        print('Seeding default fuels…')
        ensure_default_fuel_types()

        print('Seeding shop categories…')
        for ag in Agency.query.all():
            ensure_shop_categories(agency_id=ag.id)
            print(f'  agency {ag.id} {ag.name!r}')

        db.session.commit()
        print('Seed done.')


if __name__ == '__main__':
    main()
