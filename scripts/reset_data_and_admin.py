"""
Wipe all application data and create the main admin user.

Usage (on server, inside app venv):
  python scripts/reset_data_and_admin.py
"""
from sqlalchemy import text

from app import create_app
from app.models import db, User


ADMIN_NAME = "U. Technologies"
ADMIN_EMAIL = "admin@udottechnologies.com"
ADMIN_PASSWORD = "DSJoker@@0336325"


def main():
    app = create_app()
    with app.app_context():
        url = str(db.engine.url)
        print(f"Using database: {url.split('@')[-1] if '@' in url else url}")

        # Disable FK checks (MySQL); no-op harmlessly on SQLite for our truncate path
        is_mysql = url.startswith("mysql")
        if is_mysql:
            db.session.execute(text("SET FOREIGN_KEY_CHECKS=0"))

        # Delete every row in dependency-safe order
        for table in reversed(db.metadata.sorted_tables):
            deleted = db.session.execute(table.delete()).rowcount
            print(f"  cleared {table.name}: {deleted} rows")

        if is_mysql:
            db.session.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        db.session.commit()

        admin = User(
            name=ADMIN_NAME,
            email=ADMIN_EMAIL,
            phone=None,
            role="admin",
            status="active",
        )
        admin.set_password(ADMIN_PASSWORD)
        db.session.add(admin)
        db.session.commit()

        print("All data deleted.")
        print(f"Admin created: {ADMIN_NAME}")
        print(f"Login username: {ADMIN_NAME}")
        print(f"Account email:  {ADMIN_EMAIL}")
        print("Login password: (as provided)")


if __name__ == "__main__":
    main()
