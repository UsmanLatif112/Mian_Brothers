"""
Separate DB migration for OctaneFlow (not part of app boot).

App workers (Passenger / run.py) only serve HTTP — they never ALTER TABLE.
Run this script by hand after deploys that add columns/tables.

Usage (cPanel Terminal, quiet window):

  cd ~/octaneflow.udottechnologies.com
  source ~/virtualenv/octaneflow.udottechnologies.com/3.13/bin/activate
  PYTHONPATH=. python scripts/migrate_schema.py
  mkdir -p tmp && touch tmp/restart.txt

Optional one-time liter precision widen:
  PYTHONPATH=. python scripts/migrate_liter_precision.py
"""
from app import create_app, run_schema_boot


def main():
    app = create_app()
    with app.app_context():
        print('Migrating schema (create_all + ensure_* + light seeds)...')
        run_schema_boot()
        print('Migration finished.')
        print('Restart the Python app: mkdir -p tmp && touch tmp/restart.txt')


if __name__ == '__main__':
    main()
