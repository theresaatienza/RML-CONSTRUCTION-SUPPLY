from datetime import datetime, timedelta
from functools import wraps
from flask import flash, redirect, request, session, url_for


def login_required(role=None):
    """Ensure user is logged in, verify role, and prevent cross-portal access."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please sign in to continue.", "warning")
                if role in ("admin", "staff"):
                    return redirect(url_for("main.admin_login"))
                elif role == "driver":
                    return redirect(url_for("main.driver_login"))
                else:
                    next_target = request.full_path if (request.query_string and request.query_string.strip()) else request.path
                    if next_target.endswith("?"):
                        next_target = next_target[:-1]
                    return redirect(url_for("main.login", next=next_target))

            user_role = session.get("role")
            if role:
                if role in ("admin", "staff"):
                    if user_role not in ("admin", "staff"):
                        flash("Access denied. Restricted to authorized administrators.", "danger")
                        if user_role == "customer":
                            return redirect(url_for("main.customer_dashboard"))
                        elif user_role == "driver":
                            return redirect(url_for("main.driver_dashboard"))
                        return redirect(url_for("main.admin_login"))
                elif role == "driver":
                    if user_role != "driver":
                        flash("Access denied. Restricted to delivery drivers.", "danger")
                        if user_role == "customer":
                            return redirect(url_for("main.customer_dashboard"))
                        elif user_role in ("admin", "staff"):
                            return redirect(url_for("main.admin_dashboard"))
                        return redirect(url_for("main.driver_login"))
                elif role == "customer":
                    if user_role != "customer":
                        flash("Please access customer features from an authorized customer session.", "warning")
                        if user_role in ("admin", "staff"):
                            return redirect(url_for("main.admin_dashboard"))
                        elif user_role == "driver":
                            return redirect(url_for("main.driver_dashboard"))
                        return redirect(url_for("main.login"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def check_login_rate_limit(conn, user_row):
    """Checks if account is temporarily locked (5 failed attempts -> 15 min lock)."""
    if not user_row:
        return False, 0
    locked_until_str = user_row["locked_until"] if "locked_until" in user_row.keys() else None
    if locked_until_str:
        try:
            locked_until = datetime.strptime(locked_until_str, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            if now < locked_until:
                diff_sec = (locked_until - now).total_seconds()
                rem_min = max(1, int(diff_sec // 60) + 1)
                return True, rem_min
        except Exception:
            pass
    return False, 0


def record_login_failure(conn, user_id):
    """Increments failed_login_attempts. Locks for 15 minutes upon reaching 5 failures."""
    if not user_id:
        return
    row = conn.execute("SELECT failed_login_attempts FROM users WHERE id = ?", (user_id,)).fetchone()
    current_attempts = (row["failed_login_attempts"] or 0) + 1 if row else 1
    if current_attempts >= 5:
        locked_until = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
            (current_attempts, locked_until, user_id),
        )
    else:
        conn.execute(
            "UPDATE users SET failed_login_attempts = ? WHERE id = ?",
            (current_attempts, user_id),
        )
    conn.commit()


def reset_login_failures(conn, user_id):
    """Resets failed attempts and unlocks account after successful authentication."""
    if not user_id:
        return
    conn.execute(
        "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (user_id,),
    )
    conn.commit()


def log_access_event(conn, action, affected_user, performed_by_id, performed_by_name, details=""):
    """Helper to safely record an access or permission management event."""
    try:
        aff_id = None
        aff_name = ""
        aff_email = ""
        aff_role = ""
        if affected_user is not None:
            try:
                aff_id = affected_user["id"]
            except Exception:
                pass
            try:
                aff_name = affected_user["name"] or ""
            except Exception:
                pass
            try:
                aff_email = affected_user["email"] or ""
            except Exception:
                pass
            try:
                aff_role = affected_user["role"] or ""
            except Exception:
                pass

        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1")
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()

        conn.execute(
            """
            INSERT INTO access_logs (action, affected_user_id, affected_user_name, affected_user_email, affected_user_role, performed_by, performed_by_name, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (action, aff_id, aff_name, aff_email, aff_role, performed_by_id, performed_by_name, details, ip),
        )
    except Exception:
        pass


def customer_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("main.login"))
        if session.get("role") not in ("customer", "admin"):
            flash("Customer access required.", "danger")
            return redirect(url_for("main.home"))
        return f(*args, **kwargs)

    return wrapped

