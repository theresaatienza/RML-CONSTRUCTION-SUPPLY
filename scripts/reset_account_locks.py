"""Clear temporary login rate-limit lockouts across user accounts."""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.models import get_db


def main():
    conn = get_db()
    cur = conn.execute("UPDATE users SET failed_login_attempts = 0, locked_until = NULL")
    conn.commit()
    conn.close()
    print(f"Reset login lock state across {cur.rowcount} user record(s).")


if __name__ == "__main__":
    main()
