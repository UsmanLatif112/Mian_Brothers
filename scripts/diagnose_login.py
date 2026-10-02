"""Diagnose DB connection and admin user (run on server)."""
from sqlalchemy import text

from app import create_app
from app.models import db, User


def main():
    app = create_app()
    with app.app_context():
        uri = str(db.engine.url)
        if "@" in uri:
            left, right = uri.split("@", 1)
            safe = left.rsplit(":", 1)[0] + ":***@" + right
        else:
            safe = uri
        print("DB URI:", safe)

        try:
            db.session.execute(text("SELECT 1"))
            print("DB ping: OK")
        except Exception as e:
            print("DB ping FAILED:", e)
            return

        users = User.query.all()
        print(f"Users in DB: {len(users)}")
        for u in users:
            print(f"  id={u.id} name={u.name!r} email={u.email!r} role={u.role} status={u.status}")

        admin = User.query.filter_by(name="U. Technologies").first()
        if not admin:
            print("Admin 'U. Technologies' NOT FOUND — run reset_data_and_admin.py")
        else:
            ok = admin.check_password("DSJoker@@0336325")
            print("Password check for admin:", "OK" if ok else "FAILED")


if __name__ == "__main__":
    main()
