"""Full MySQL dump for moving OctaneFlow to a new server.

Reads DB_* from the project .env. Writes a .sql file you can import into an
empty database (phpMyAdmin or mysql client). Does not include CREATE DATABASE
or USE, so the import lands in whichever database you select.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.converters import conversions, escape_item

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

HOST = os.environ["DB_HOST"]
PORT = int(os.environ.get("DB_PORT", "3306"))
USER = os.environ["DB_USER"]
PASSWORD = os.environ["DB_PASSWORD"]
NAME = os.environ["DB_NAME"]

OUT_DIR = ROOT / "backups"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STAMP = datetime.now().strftime("%Y%m%d-%H%M")
OUT = OUT_DIR / f"octaneflow-{NAME}-{STAMP}.sql"

BATCH = 200


def q(conn, value):
    if value is None:
        return "NULL"
    return escape_item(value, conn.charset, mapping=conversions)


def main():
    print(f"Connecting to {HOST}:{PORT}/{NAME} as {USER} ...", flush=True)
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=NAME,
        charset="utf8mb4",
        connect_timeout=30,
        read_timeout=600,
        write_timeout=600,
        cursorclass=pymysql.cursors.SSCursor,
    )
    meta = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=NAME,
        charset="utf8mb4",
        connect_timeout=30,
        read_timeout=600,
        write_timeout=600,
    )
    try:
        with meta.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_TYPE
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_TYPE, TABLE_NAME
                """,
                (NAME,),
            )
            objects = cur.fetchall()
        tables = [n for n, t in objects if t == "BASE TABLE"]
        views = [n for n, t in objects if t == "VIEW"]
        print(f"Tables: {len(tables)}  Views: {len(views)}", flush=True)
        print(f"Writing {OUT}", flush=True)

        with OUT.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"-- OctaneFlow full dump of `{NAME}`\n"
                f"-- {datetime.now().isoformat(timespec='seconds')}\n"
                "-- Import into an EMPTY database on the new server.\n"
                "SET NAMES utf8mb4;\n"
                "SET FOREIGN_KEY_CHECKS=0;\n"
                "SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n"
                "SET TIME_ZONE='+00:00';\n\n"
            )
            for name in tables:
                _dump_table(conn, meta, fh, name)
            for name in views:
                _dump_view(meta, fh, name)
            _dump_triggers(meta, fh)
            _dump_routines(meta, fh)
            fh.write("SET FOREIGN_KEY_CHECKS=1;\n")
        size = OUT.stat().st_size
        print(f"DONE bytes={size} file={OUT}", flush=True)
    finally:
        conn.close()
        meta.close()


def _dump_table(conn, meta, fh, name):
    with meta.cursor() as cur:
        cur.execute(f"SHOW CREATE TABLE `{name}`")
        row = cur.fetchone()
        create_sql = row[1]
        cur.execute(f"SELECT COUNT(*) FROM `{name}`")
        count = cur.fetchone()[0]
    print(f"  {name}: {count} rows", flush=True)
    fh.write(f"\n-- Table `{name}` ({count} rows)\n")
    fh.write(f"DROP TABLE IF EXISTS `{name}`;\n")
    fh.write(create_sql + ";\n")
    if count == 0:
        return
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{name}`")
        columns = [d[0] for d in cur.description]
        col_sql = ", ".join(f"`{c}`" for c in columns)
        batch = []
        written = 0
        for row in cur:
            batch.append("(" + ", ".join(q(conn, v) for v in row) + ")")
            if len(batch) >= BATCH:
                fh.write(
                    f"INSERT INTO `{name}` ({col_sql}) VALUES\n"
                    + ",\n".join(batch)
                    + ";\n"
                )
                written += len(batch)
                batch.clear()
        if batch:
            fh.write(
                f"INSERT INTO `{name}` ({col_sql}) VALUES\n"
                + ",\n".join(batch)
                + ";\n"
            )
            written += len(batch)
        if written != count:
            print(f"  WARNING {name}: wrote {written} of {count}", flush=True)


def _dump_view(meta, fh, name):
    with meta.cursor() as cur:
        cur.execute(f"SHOW CREATE VIEW `{name}`")
        create_sql = cur.fetchone()[1]
    fh.write(f"\n-- View `{name}`\n")
    fh.write(f"DROP VIEW IF EXISTS `{name}`;\n")
    fh.write(create_sql + ";\n")


def _dump_triggers(meta, fh):
    with meta.cursor() as cur:
        cur.execute("SHOW TRIGGERS")
        triggers = cur.fetchall() or []
        for trig in triggers:
            trig_name = trig[0]
            cur.execute(f"SHOW CREATE TRIGGER `{trig_name}`")
            create_sql = cur.fetchone()[2]
            fh.write(f"\n-- Trigger `{trig_name}`\n")
            fh.write("DELIMITER $$\n")
            fh.write(create_sql + "$$\n")
            fh.write("DELIMITER ;\n")


def _dump_routines(meta, fh):
    with meta.cursor() as cur:
        cur.execute(
            """
            SELECT ROUTINE_NAME, ROUTINE_TYPE
            FROM information_schema.ROUTINES
            WHERE ROUTINE_SCHEMA = %s
            """,
            (NAME,),
        )
        routines = cur.fetchall() or []
        for routine_name, routine_type in routines:
            cur.execute(f"SHOW CREATE {routine_type} `{routine_name}`")
            create_sql = cur.fetchone()[2]
            fh.write(f"\n-- {routine_type} `{routine_name}`\n")
            fh.write("DELIMITER $$\n")
            fh.write(f"DROP {routine_type} IF EXISTS `{routine_name}`$$\n")
            fh.write(create_sql + "$$\n")
            fh.write("DELIMITER ;\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("FAIL:", type(exc).__name__, exc, file=sys.stderr)
        sys.exit(1)
