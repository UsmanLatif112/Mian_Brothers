"""
ONE-TIME: insert previous / opening stock directly into the DB (no vendor).

Run once on the server:
  PYTHONPATH=. python scripts/seed_opening_stock.py
"""
from app import create_app
from app.opening_stock import seed_opening_stock


def main():
    app = create_app()
    with app.app_context():
        seeded = seed_opening_stock(force=True)
        if seeded:
            print('Done. Opening stock written to DB (one-time).')
        else:
            print('Skipped: no admin user found. Create a user first.')


if __name__ == '__main__':
    main()
