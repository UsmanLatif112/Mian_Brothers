from dotenv import load_dotenv
import os
import pymysql

load_dotenv(override=True)

user = os.environ["DB_USER"]
host = os.environ["DB_HOST"]
port = int(os.environ.get("DB_PORT", 3306))
name = os.environ["DB_NAME"]
password = os.environ["DB_PASSWORD"]

print(f"Trying {user} at {host}:{port}/{name} ...")
print(f"Password contains @: {'@' in password}")

try:
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=name,
        connect_timeout=20,
    )
    cur = conn.cursor()
    cur.execute("SELECT DATABASE(), USER(), VERSION()")
    print("SUCCESS:", cur.fetchone())
    cur.execute("SHOW TABLES")
    tables = [t[0] for t in cur.fetchall()]
    print(f"Tables: {len(tables)}")
    for t in tables:
        print(" -", t)
    conn.close()
except Exception as e:
    print("FAIL:", type(e).__name__, e)
