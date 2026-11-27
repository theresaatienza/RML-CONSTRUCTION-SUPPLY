from datetime import datetime, timedelta
import re


def validate_password_strength(password):
    """
    Validates password against strict security policy:
    1. Minimum 8 characters.
    2. At least one uppercase letter (A-Z).
    3. At least one lowercase letter (a-z).
    4. At least one number (0-9).
    5. At least one special character (@, $, !, %, *, ?, &, #, etc.).
    6. No spaces allowed.
    7. Reject common/weak passwords.
    """
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if " " in password:
        return False, "Password must not contain spaces."
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter (A-Z)."
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter (a-z)."
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one number (0-9)."
    if not re.search(r"[@$!%*?&#^()_\-+=\[\]{}|\\:;\"'<>,.~`]", password):
        return False, "Password must contain at least one special character (@, $, !, %, *, ?, &, #, etc.)."

    common_passwords = {
        "password", "password123", "12345678", "123456789", "qwerty123",
        "admin123", "welcome123", "letmein123", "rml12345", "construction123",
        "rmlconstruction", "adminadmin", "password1"
    }
    if password.lower() in common_passwords:
        return False, "This password is too common or easily guessed. Please choose a stronger password."
    return True, None


def validate_ph_phone(phone):
    """
    Validates Philippine mobile numbers:
    Accepts 09XXXXXXXXX (11 digits) or +639XXXXXXXXX (13 chars).
    """
    if not phone:
        return False, None
    clean = re.sub(r"[\s\-\(\)]", "", phone.strip())
    if re.match(r"^09\d{9}$", clean):
        return True, clean
    if re.match(r"^\+639\d{9}$", clean):
        return True, clean
    return False, None


def validate_email_format(email):
    """Validates email format."""
    if not email:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email.strip()))


def clean_order_code(code):
    """Normalize order code for display without repeating '#' prefixes."""
    if not code:
        return ""
    code_str = str(code).strip()
    return code_str if code_str.startswith("#") else f"#{code_str}"


def format_notification_time(dt_str, now=None):
    if not dt_str:
        return ""
    if now is None:
        now = datetime.now()
    try:
        if len(dt_str) == 10:
            dt = datetime.strptime(dt_str, "%Y-%m-%d")
        elif len(dt_str) >= 19:
            dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.fromisoformat(dt_str)
    except Exception:
        return dt_str

    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "Just now"
    elif seconds < 60:
        return f"{seconds}s ago" if seconds > 5 else "Just now"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins} minute{'s' if mins > 1 else ''} ago"
    elif seconds < 86400 and dt.date() == now.date():
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    elif dt.date() == (now.date() - timedelta(days=1)):
        return f"Yesterday at {dt.strftime('%I:%M %p')}"
    elif dt.year == now.year:
        return dt.strftime("%b %d, %I:%M %p")
    else:
        return dt.strftime("%b %d, %Y, %I:%M %p")


def get_notification_date_group(dt_str, now=None):
    if not dt_str:
        return "Earlier"
    if now is None:
        now = datetime.now()
    try:
        dt = datetime.strptime(dt_str[:10], "%Y-%m-%d").date()
        if dt == now.date():
            return "Today"
        elif dt == (now.date() - timedelta(days=1)):
            return "Yesterday"
        else:
            return "Earlier"
    except Exception:
        return "Earlier"


def normalize_image_url(url):
    """Normalize local product image paths for static serving while preserving full URLs."""
    if not url:
        return "https://images.unsplash.com/photo-1589939705384-5185137a7f0f?w=500"
    u = str(url).strip()
    if u.startswith(("http://", "https://", "/")):
        return u
    if u.startswith("static/"):
        return "/" + u
    return "/static/" + u.lstrip("/")


def _validate_ph_phone(phone):
    cleaned = re.sub(r"[\s\-()]", "", phone or "")
    if re.match(r"^(09\d{9}|\+639\d{9})$", cleaned):
        return True, cleaned
    return False, cleaned


def _validate_name(name, label="Name"):
    if not name or len(name) < 2:
        return f"{label} must be at least 2 characters."
    if len(name) > 60:
        return f"{label} is too long (maximum 60 characters)."
    if not re.match(r"^[A-Za-z0-9\s.,'\-&]+$", name):
        return f"{label} contains invalid characters."
    return None


def _validate_id_number(id_number):
    if not id_number or len(id_number) < 4 or len(id_number) > 40:
        return "ID Number must be between 4 and 40 characters."
    if not re.match(r"^[A-Za-z0-9\-\s]+$", id_number):
        return "ID Number contains invalid characters."
    return None

