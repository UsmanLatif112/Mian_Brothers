"""
ONE-TIME: set current fuel prices and refresh meter sale_rate snapshots.

  Petrol  → 390.50 / L
  Diesel  → 404.30 / L

Default effective date = today (UTC). Optional:
  --date=2026-10-02

Updates:
  - FuelPrice row for that effective date
  - MeterReading.sale_rate for readings on/after that date
  - Fuel CreditSale rate/amount on/after that date (entry_type=sale)
  - Customer balances for affected credit sales

Usage:
  PYTHONPATH=. python scripts/set_fuel_prices_oct1_2026.py
  PYTHONPATH=. python scripts/set_fuel_prices_oct1_2026.py --date=2026-10-02
"""
from datetime import date, datetime, time

from app import create_app
from app.models import db, User, FuelType, FuelPrice, MeterReading, CreditSale, Customer
from app.customers.service import recalculate_customer_balance


PRICES = {
    'petrol': 390.50,
    'diesel': 404.30,
}


def _parse_day(argv):
    for arg in argv:
        if arg.startswith('--date='):
            return datetime.strptime(arg.split('=', 1)[1], '%Y-%m-%d').date()
    return datetime.utcnow().date()


def _fuel(name_key):
    return (
        FuelType.query
        .filter(FuelType.name.ilike(f'%{name_key}%'))
        .order_by(FuelType.id.asc())
        .first()
    )


def _admin_id():
    user = User.query.filter_by(role='admin').order_by(User.id.asc()).first()
    if user is None:
        user = User.query.order_by(User.id.asc()).first()
    return user.id if user else None


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    eff = _parse_day(argv)

    app = create_app()
    with app.app_context():
        user_id = _admin_id()
        if not user_id:
            print('No users found — abort.')
            return

        fuel_ids = {}
        for key, price in PRICES.items():
            fuel = _fuel(key)
            if not fuel:
                print(f'Fuel type matching "{key}" not found — skip.')
                continue
            fuel_ids[fuel.id] = (fuel.name, price)

            existing = FuelPrice.query.filter_by(
                fuel_type_id=fuel.id,
                effective_date=eff,
            ).all()
            for row in existing:
                db.session.delete(row)

            db.session.add(FuelPrice(
                fuel_type_id=fuel.id,
                price_per_liter=price,
                effective_date=eff,
                updated_by=user_id,
                created_at=datetime.combine(eff, time.min),
            ))
            print(f'Set {fuel.name} = PKR {price:.2f} effective {eff.isoformat()}')

        if not fuel_ids:
            print('No fuels updated — abort.')
            return

        db.session.flush()

        meters = (
            MeterReading.query
            .filter(
                MeterReading.reading_date >= eff,
                MeterReading.fuel_type_id.in_(list(fuel_ids.keys())),
            )
            .all()
        )
        meter_updated = 0
        for m in meters:
            name, price = fuel_ids[m.fuel_type_id]
            old = float(m.sale_rate) if m.sale_rate is not None else None
            m.sale_rate = price
            meter_updated += 1
            print(
                f'  meter #{m.id} {name} {m.reading_date}: '
                f'rate {old} → {price} (liters={float(m.liters_sold or 0):.2f})'
            )

        sales = (
            CreditSale.query
            .filter(
                CreditSale.sale_date >= eff,
                CreditSale.fuel_type_id.in_(list(fuel_ids.keys())),
                CreditSale.entry_type == 'sale',
            )
            .all()
        )
        sale_updated = 0
        customer_ids = set()
        for s in sales:
            name, price = fuel_ids[s.fuel_type_id]
            liters = float(s.liters or 0)
            discount = float(s.discount or 0)
            old_rate = float(s.rate or 0)
            old_amount = float(s.amount or 0)
            new_amount = max(round(liters * price - discount, 2), 0.0)
            s.rate = price
            s.amount = new_amount
            if s.customer_id:
                customer_ids.add(s.customer_id)
            sale_updated += 1
            print(
                f'  sale #{s.id} {name} {s.sale_date}: '
                f'rate {old_rate} → {price}, amount {old_amount:.2f} → {new_amount:.2f}'
            )

        db.session.flush()

        for cid in customer_ids:
            customer = Customer.query.get(cid)
            if customer:
                before = float(customer.current_balance_due or 0)
                recalculate_customer_balance(customer)
                after = float(customer.current_balance_due or 0)
                print(f'  customer #{cid} {customer.name}: due {before:.2f} → {after:.2f}')

        db.session.commit()
        print(
            f'Done. Prices set for {eff}. '
            f'Meters updated: {meter_updated}. Fuel sales updated: {sale_updated}. '
            f'Customers recalculated: {len(customer_ids)}.'
        )


if __name__ == '__main__':
    main()
