"""Create all OctaneFlow tables on the configured MySQL database and seed defaults."""
from sqlalchemy import inspect, text
from app import create_app
from app.models import db


def main():
    app = create_app()
    with app.app_context():
        host = str(db.engine.url).split('@')[-1]
        print(f'Using database: {host}')
        db.session.execute(text('SELECT 1'))
        db.create_all()
        tables = sorted(inspect(db.engine).get_table_names())
        print(f'Created/verified {len(tables)} tables:')
        for name in tables:
            print(f'  - {name}')
        print('Done.')


if __name__ == '__main__':
    main()
