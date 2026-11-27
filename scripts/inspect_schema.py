"""Inspect active SQLite database tables, columns, and row counts."""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.models import get_db


def main():
    conn = get_db()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"Database Tables ({len(tables)}): {tables}")
    for tbl in tables:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  - {tbl:<24} rows={count:<5} columns={cols}")
    conn.close()


if __name__ == "__main__":
    main()
