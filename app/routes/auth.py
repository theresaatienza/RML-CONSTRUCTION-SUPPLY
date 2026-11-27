from datetime import datetime, timedelta
import json
import math
import os
import re
import secrets
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_transport_requests
    HAS_GOOGLE_AUTH = True
except ImportError:
    google_id_token = None
    google_transport_requests = None
    HAS_GOOGLE_AUTH = False

from app.models import get_db, create_notification
from app.routes import main_bp, verify_bp
from app.utils import (
    customer_required,
    check_login_rate_limit,
    record_login_failure,
    reset_login_failures,
    validate_email_format,
    validate_password_strength,
    validate_ph_phone,
    _validate_ph_phone,
    _validate_name,
    _validate_id_number,
)


@main_bp.route("/")
def home():
    role = session.get("role")
    if role in ("admin", "staff"):
        return redirect(url_for("main.admin_dashboard"))
    if role == "driver":
        return redirect(url_for("main.driver_dashboard"))

    conn = get_db()
    featured_products = conn.execute(
        """
        SELECT p.*, c.name AS category_name,
               (SELECT COUNT(*) FROM product_variations pv WHERE pv.product_id = p.id AND pv.is_available = 1) AS var_count,
               (SELECT MIN(pv.price) FROM product_variations pv WHERE pv.product_id = p.id AND pv.is_available = 1) AS min_var_price
        FROM products p
        JOIN categories c ON p.category_id = c.id
        WHERE p.is_active = 1
        ORDER BY CASE WHEN p.stock_quantity > 0 THEN 0 ELSE 1 END, p.id ASC
        LIMIT 6
        """
    ).fetchall()

    categories = conn.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id AND p.is_active = 1) AS product_count
        FROM categories c
        WHERE (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id AND p.is_active = 1) > 0
        ORDER BY c.id ASC
        """
    ).fetchall()
    conn.close()

    return render_template("customer/landing.html", featured_products=featured_products, categories=categories)


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("role") == "customer":
        next_url = request.args.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("main.customer_dashboard"))
    elif session.get("role") in ("admin", "staff"):
        return redirect(url_for("main.admin_dashboard"))
    elif session.get("role") == "driver":
        return redirect(url_for("main.driver_dashboard"))

    if request.method == "POST":
        identifier = (request.form.get("identifier") or request.form.get("email") or "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE LOWER(email) = ? AND is_active = 1",
            (identifier,),
        ).fetchone()

        if user:
            # Check if this account belongs to admin/staff or driver
            if user["role"] in ("admin", "staff"):
                conn.close()
                flash("Admin/Staff accounts must sign in via the restricted Admin Portal.", "warning")
                return redirect(url_for("main.admin_login"))
            elif user["role"] == "driver":
                conn.close()
                flash("Driver accounts must sign in via the restricted Driver Portal.", "warning")
                return redirect(url_for("main.driver_login"))

            # Rate limiting check
            is_locked, rem_min = check_login_rate_limit(conn, user)
            if is_locked:
                conn.close()
                flash(f"Account temporarily locked due to too many failed attempts. Please try again in {rem_min} minute(s).", "danger")
                return render_template("auth/login.html")

            if check_password_hash(user["password_hash"], password):
                reset_login_failures(conn, user["id"])
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                session["user_name"] = user["name"]
                conn.close()
                flash(f"Welcome back, {user['name']}!", "success")
                next_url = request.form.get("next") or request.args.get("next")
                if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("main.customer_dashboard"))
            else:
                record_login_failure(conn, user["id"])
                conn.close()
                flash("Invalid email or password.", "danger")
                return render_template("auth/login.html")
        else:
            conn.close()
            flash("Invalid email or password.", "danger")

    return render_template("auth/login.html", google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""))


@main_bp.route("/auth/google", methods=["POST"])
def google_auth():
    """Verify Google OAuth 2.0 ID token and authenticate or register customer account."""
    is_ajax = (
        request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )

    # 1. Extract payload & token
    credential = None
    g_csrf_token_body = None
    next_url = None

    if request.is_json:
        data = request.get_json(silent=True) or {}
        credential = (data.get("credential") or data.get("id_token") or "").strip()
        g_csrf_token_body = (data.get("g_csrf_token") or "").strip()
        next_url = data.get("next")
    else:
        credential = (request.form.get("credential") or request.form.get("id_token") or "").strip()
        g_csrf_token_body = (request.form.get("g_csrf_token") or "").strip()
        next_url = request.form.get("next")

    if not credential:
        msg = "Missing Google ID token credential."
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    # 2. CSRF Token verification if provided by Google Identity Services
    g_csrf_cookie = request.cookies.get("g_csrf_token")
    if g_csrf_cookie and g_csrf_token_body:
        if g_csrf_cookie != g_csrf_token_body:
            msg = "CSRF verification failed for Google Sign-In request."
            if is_ajax:
                return jsonify({"success": False, "error": msg}), 400
            flash(msg, "danger")
            return redirect(url_for("main.login"))

    # 3. Verify ID Token using Google's official authentication library
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()

    if not HAS_GOOGLE_AUTH:
        msg = "Google authentication service library is not installed on the server."
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 500
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    try:
        transport_req = google_transport_requests.Request()
        # verify_oauth2_token validates cryptographic signature against Google's certs,
        # checks expiration (exp), and verifies audience (aud) if client_id is provided
        id_info = google_id_token.verify_oauth2_token(
            credential,
            transport_req,
            audience=google_client_id if google_client_id else None,
        )
    except ValueError as val_err:
        msg = f"Invalid Google ID token: {str(val_err)}"
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))
    except Exception as err:
        msg = f"Google token verification failed: {str(err)}"
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    # 4. Validate Issuer explicitly
    issuer = id_info.get("iss", "")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        msg = "Invalid token issuer. Token was not issued by Google."
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    # 5. Validate Audience explicitly if configured
    if google_client_id and id_info.get("aud") != google_client_id:
        msg = "Token audience does not match configured Google OAuth Client ID."
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    # 6. Retrieve verified email, name, and profile picture
    email = (id_info.get("email") or "").strip().lower()
    if not email:
        msg = "Verified Google token does not contain an email address."
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    # Validate that Google verified the email address
    is_verified_by_google = id_info.get("email_verified")
    if not is_verified_by_google:
        msg = "Your Google account email has not been verified by Google. Unverified emails cannot be linked."
        if is_ajax:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("main.login"))

    google_sub = str(id_info.get("sub", "")).strip()
    full_name = (id_info.get("name") or id_info.get("given_name") or email.split("@")[0]).strip()
    profile_pic = (id_info.get("picture") or "").strip()

    # 7. Check whether email exists in the database
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?", (email,)).fetchone()

    # 8. Role protection: Never allow Google Sign-In to assign Admin or Driver privileges
    if user:
        if user["role"] in ("admin", "staff"):
            conn.close()
            msg = "Admin and staff accounts must sign in using credentials via the restricted Admin Portal."
            if is_ajax:
                return jsonify({"success": False, "error": msg, "redirect": url_for("main.admin_login")}), 403
            flash(msg, "warning")
            return redirect(url_for("main.admin_login"))
        elif user["role"] == "driver":
            conn.close()
            msg = "Driver accounts must sign in using credentials via the restricted Driver Portal."
            if is_ajax:
                return jsonify({"success": False, "error": msg, "redirect": url_for("main.driver_login")}), 403
            flash(msg, "warning")
            return redirect(url_for("main.driver_login"))
        elif user["role"] != "customer":
            conn.close()
            msg = "Google Sign-In is only permitted for customer accounts."
            if is_ajax:
                return jsonify({"success": False, "error": msg}), 403
            flash(msg, "danger")
            return redirect(url_for("main.login"))

        # Account status check
        if not user["is_active"]:
            conn.close()
            msg = "Your customer account is currently inactive. Please contact support."
            if is_ajax:
                return jsonify({"success": False, "error": msg}), 403
            flash(msg, "danger")
            return redirect(url_for("main.login"))

        # Rate limiting check
        is_locked, rem_min = check_login_rate_limit(conn, user)
        if is_locked:
            conn.close()
            msg = f"Account temporarily locked due to too many failed attempts. Please try again in {rem_min} minute(s)."
            if is_ajax:
                return jsonify({"success": False, "error": msg}), 429
            flash(msg, "danger")
            return redirect(url_for("main.login"))

        # Safe customer authentication: update google_id and avatar_url, reset failed attempts
        reset_login_failures(conn, user["id"])
        conn.execute(
            """
            UPDATE users
            SET google_id = COALESCE(NULLIF(?, ''), google_id),
                avatar_url = CASE WHEN ? != '' THEN ? ELSE avatar_url END,
                is_verified = 1
            WHERE id = ?
            """,
            (google_sub, profile_pic, profile_pic, user["id"]),
        )
        conn.commit()
        user_id = user["id"]
        user_name = user["name"]
        conn.close()
    else:
        # 9. New customer: automatically create customer account
        # Generate secure random unguessable password hash to satisfy NOT NULL constraint
        pwd_hash = generate_password_hash(secrets.token_urlsafe(32))
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (name, email, phone, address, password_hash, role, is_active, is_verified, google_id, avatar_url, auth_provider)
            VALUES (?, ?, '', '', ?, 'customer', 1, 1, ?, ?, 'google')
            """,
            (full_name, email, pwd_hash, google_sub, profile_pic),
        )
        user_id = cur.lastrowid
        user_name = full_name
        conn.commit()

        # Send welcome notification to new customer
        create_notification(
            conn,
            user_id,
            "Welcome to RML Construction Supply!",
            "Your customer account was successfully created via Google Sign-In. You can now browse our catalog, place orders, and track site deliveries.",
            "account",
        )
        conn.commit()
        conn.close()

    # 10. Establish authenticated Flask session
    session.clear()
    session["user_id"] = user_id
    session["role"] = "customer"
    session["user_name"] = user_name
    session.permanent = True

    # 11. Redirect customer to customer dashboard
    target_url = next_url or request.args.get("next")
    if not target_url or not target_url.startswith("/") or target_url.startswith("//"):
        target_url = url_for("main.customer_dashboard")

    if is_ajax:
        return jsonify({
            "success": True,
            "message": f"Welcome back, {user_name}!" if user else f"Welcome to RML Construction Supply, {user_name}!",
            "redirect_url": target_url,
            "user": {
                "id": user_id,
                "name": user_name,
                "email": email,
                "role": "customer",
            },
        })
    else:
        flash(f"Welcome back, {user_name}!" if user else f"Welcome to RML Construction Supply, {user_name}!", "success")
        return redirect(target_url)


@main_bp.route("/register", methods=["GET", "POST"])
def register():
    if session.get("role") == "customer":
        return redirect(url_for("main.customer_dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not phone or not address or not password:
            flash("All fields are required. Please fill in all fields.", "warning")
            return render_template("auth/register.html")

        if not validate_email_format(email):
            flash("Please enter a valid email address.", "warning")
            return render_template("auth/register.html")

        is_phone_valid, clean_phone = validate_ph_phone(phone)
        if not is_phone_valid:
            flash("Please enter a valid Philippine mobile number (e.g., 09171234567 or +639171234567).", "warning")
            return render_template("auth/register.html")

        if password != confirm_password:
            flash("Passwords do not match. Please verify your password confirmation.", "danger")
            return render_template("auth/register.html")

        is_valid_pwd, pwd_err = validate_password_strength(password)
        if not is_valid_pwd:
            flash(pwd_err, "danger")
            return render_template("auth/register.html")

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,)).fetchone()
        if existing:
            conn.close()
            flash("An account with that email already exists. Please sign in.", "warning")
            return redirect(url_for("main.login"))

        conn.execute(
            """
            INSERT INTO users (name, email, phone, address, password_hash, role, is_active, is_verified)
            VALUES (?, ?, ?, ?, ?, 'customer', 1, 1)
            """,
            (name, email, clean_phone, address, generate_password_hash(password)),
        )
        conn.commit()
        conn.close()

        flash("Account created successfully! You can now sign in.", "success")
        return redirect(url_for("main.login"))

    return render_template("auth/register.html")


@main_bp.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out from the Customer Portal.", "info")
    return redirect(url_for("main.login"))


@main_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or not validate_email_format(email):
            flash("Please enter a valid email address.", "warning")
            return render_template("auth/forgot_password.html")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE LOWER(email) = ? AND role = 'customer'", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_expires_at = ? WHERE id = ?",
                (token, expires_at, user["id"]),
            )
            conn.commit()
            conn.close()

            reset_url = url_for("main.reset_password", token=token, _external=True)
            flash(f"Password reset link generated for {email}! (Local link: {reset_url})", "info")
            return redirect(url_for("main.login"))
        else:
            conn.close()
            flash("If an account exists with that email address, password reset instructions have been generated.", "info")
            return redirect(url_for("main.login"))

    return render_template("auth/forgot_password.html")


@main_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = conn.execute(
        "SELECT * FROM users WHERE reset_token = ? AND reset_token_expires_at > ?",
        (token, now_str),
    ).fetchone()

    if not user:
        conn.close()
        flash("Invalid or expired password reset link. Please submit a new request.", "danger")
        return redirect(url_for("main.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not password or password != confirm_password:
            flash("Passwords do not match.", "danger")
            email_val = user["email"]
            conn.close()
            return render_template("auth/reset_password.html", token=token, user_email=email_val)

        is_valid_pwd, pwd_err = validate_password_strength(password)
        if not is_valid_pwd:
            flash(pwd_err, "danger")
            email_val = user["email"]
            conn.close()
            return render_template("auth/reset_password.html", token=token, user_email=email_val)

        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expires_at = NULL WHERE id = ?",
            (generate_password_hash(password), user["id"]),
        )
        conn.commit()
        conn.close()

        flash("Your password has been reset successfully! You can now sign in with your new password.", "success")
        return redirect(url_for("main.login"))

    email_val = user["email"]
    conn.close()
    return render_template("auth/reset_password.html", token=token, user_email=email_val)


@main_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("role") in ("admin", "staff"):
        return redirect(url_for("main.admin_dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE (LOWER(email) = ? OR staff_id = ?) AND is_active = 1",
            (identifier.lower(), identifier),
        ).fetchone()

        if user:
            if user["role"] not in ("admin", "staff"):
                conn.close()
                flash("Access denied. Authorized administrative personnel only.", "danger")
                return render_template("auth/admin_login.html")

            is_locked, rem_min = check_login_rate_limit(conn, user)
            if is_locked:
                conn.close()
                flash(f"Account locked due to multiple failed login attempts. Try again in {rem_min} minute(s).", "danger")
                return render_template("auth/admin_login.html")

            if check_password_hash(user["password_hash"], password):
                reset_login_failures(conn, user["id"])
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                session["user_name"] = user["name"]
                conn.close()
                flash(f"Welcome, {user['name']}! (Admin Console)", "success")
                return redirect(url_for("main.admin_dashboard"))
            else:
                record_login_failure(conn, user["id"])
                conn.close()
                flash("Invalid administrator credentials.", "danger")
                return render_template("auth/admin_login.html")
        else:
            conn.close()
            flash("Invalid administrator credentials.", "danger")

    return render_template("auth/admin_login.html")


@main_bp.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Administrator session closed.", "info")
    return redirect(url_for("main.admin_login"))


@main_bp.route("/driver/login", methods=["GET", "POST"])
def driver_login():
    if session.get("role") == "driver":
        return redirect(url_for("main.driver_dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE (LOWER(email) = ? OR staff_id = ?) AND is_active = 1",
            (identifier.lower(), identifier),
        ).fetchone()

        if user:
            if user["role"] != "driver":
                conn.close()
                flash("Access denied. Authorized delivery drivers only.", "danger")
                return render_template("auth/driver_login.html")

            is_locked, rem_min = check_login_rate_limit(conn, user)
            if is_locked:
                conn.close()
                flash(f"Account locked due to multiple failed attempts. Try again in {rem_min} minute(s).", "danger")
                return render_template("auth/driver_login.html")

            if check_password_hash(user["password_hash"], password):
                reset_login_failures(conn, user["id"])
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                session["user_name"] = user["name"]
                conn.close()
                flash(f"Signed in as Delivery Driver: {user['name']}", "success")
                return redirect(url_for("main.driver_dashboard"))
            else:
                record_login_failure(conn, user["id"])
                conn.close()
                flash("Invalid driver credentials.", "danger")
                return render_template("auth/driver_login.html")
        else:
            conn.close()
            flash("Invalid driver credentials.", "danger")

    return render_template("auth/driver_login.html")


@main_bp.route("/driver/logout")
def driver_logout():
    session.clear()
    flash("Driver session closed.", "info")
    return redirect(url_for("main.driver_login"))


@verify_bp.route("/customer/verification/step1", methods=["GET", "POST"])
@verify_bp.route("/customer/verification/step2", methods=["GET", "POST"])
@verify_bp.route("/customer/verification/step3", methods=["GET", "POST"])
@customer_required
def redirect_old_steps():
    return redirect(url_for("verify.wizard"))


@verify_bp.route("/customer/verification", methods=["GET", "POST"])
@customer_required
def wizard():

    user_id = session.get("user_id")
    conn = get_db()
    customer = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    current_step = session.get("verify_step", 1)
    if current_step not in (1, 2, 3):
        current_step = 1

    if request.method == "POST":
        nav = request.form.get("nav", "")

        # Handle Back navigation without changing URL
        if nav == "back_to_1":
            session["verify_step"] = 1
            conn.close()
            return redirect(url_for("verify.wizard"))
        if nav == "back_to_2":
            if session.get("verify_step1") and customer["face_descriptor"]:
                session["verify_step"] = 2
            else:
                session["verify_step"] = 1
            conn.close()
            return redirect(url_for("verify.wizard"))

        # Handle Step 1 Submission: Facial Recognition + ID OCR
        if current_step == 1 or request.form.get("wizard_step") == "1":
            id_type = request.form.get("id_type", "").strip()
            id_number = request.form.get("id_number", "").strip()
            id_ocr_name = request.form.get("id_ocr_name", "").strip()
            id_ocr_text = request.form.get("id_ocr_text", "").strip()
            ocr_address = request.form.get("ocr_address", "").strip()

            # 1. Enforce Face Scan
            if not customer["face_descriptor"]:
                conn.close()
                flash("Facial scan is required. Please capture and enroll your Face ID first.", "warning")
                return render_template("customer/verify_step1.html", customer=customer)

            # 2. Enforce ID OCR Scan (must have actual OCR text extracted from ID image)
            if not id_ocr_text or len(id_ocr_text) < 10:
                conn.close()
                flash("Government ID OCR scan is required. Please upload your ID photo and click 'Scan ID with OCR'.", "warning")
                return render_template("customer/verify_step1.html", customer=customer)

            # 3. Validate ID Fields & verify extracted ID Number matches OCR text
            name_err = _validate_name(id_ocr_name, "Full Name on ID")
            id_err = _validate_id_number(id_number)
            if not id_type or name_err or id_err:
                conn.close()
                flash(name_err or id_err or "Please select a valid ID Type.", "warning")
                return render_template("customer/verify_step1.html", customer=customer)

            clean_id = re.sub(r"[^A-Za-z0-9]", "", id_number).lower()
            clean_ocr = re.sub(r"[^A-Za-z0-9]", "", id_ocr_text).lower()
            if clean_id not in clean_ocr:
                conn.close()
                flash("ID Number does not match the scanned Government ID text. Please re-scan your ID.", "danger")
                return render_template("customer/verify_step1.html", customer=customer)

            session["verify_step1"] = {
                "id_type": id_type,
                "id_number": id_number,
                "id_ocr_name": id_ocr_name,
                "id_ocr_text": id_ocr_text,
                "ocr_address": ocr_address[:200],
            }
            conn.execute(
                "UPDATE users SET id_type = ?, id_number = ?, id_ocr_name = ?, id_ocr_text = ? WHERE id = ?",
                (id_type, id_number, id_ocr_name, id_ocr_text, user_id),
            )
            conn.commit()
            conn.close()
            session["verify_step"] = 2
            return redirect(url_for("verify.wizard"))

        # Handle Step 2 Submission: Basic Information
        if current_step == 2 and request.form.get("wizard_step") == "2":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            birthdate = request.form.get("birthdate", "").strip()
            gender = request.form.get("gender", "").strip()
            address = request.form.get("address", "").strip()

            step2_draft = {
                "name": name, "email": email, "phone": phone,
                "birthdate": birthdate, "gender": gender, "address": address,
            }

            name_err = _validate_name(name, "Full / Company Name")
            if name_err:
                conn.close()
                flash(name_err, "warning")
                return render_template("customer/verify_step2.html", customer=customer, step1_data=session.get("verify_step1", {}), step2_data=step2_draft)

            if not email or len(email) > 100 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                conn.close()
                flash("Please enter a valid email address (max 100 characters).", "danger")
                return render_template("customer/verify_step2.html", customer=customer, step1_data=session.get("verify_step1", {}), step2_data=step2_draft)

            is_phone_ok, clean_phone = _validate_ph_phone(phone)
            if not is_phone_ok:
                conn.close()
                flash("Please enter a valid Philippine mobile number (e.g. 09171234567).", "danger")
                return render_template("customer/verify_step2.html", customer=customer, step1_data=session.get("verify_step1", {}), step2_data=step2_draft)

            if not birthdate or not re.match(r"^\d{4}-\d{2}-\d{2}$", birthdate):
                conn.close()
                flash("Please enter a valid birthdate.", "warning")
                return render_template("customer/verify_step2.html", customer=customer, step1_data=session.get("verify_step1", {}), step2_data=step2_draft)

            if not address or len(address) < 10 or len(address) > 200:
                conn.close()
                flash("Delivery address must be between 10 and 200 characters.", "warning")
                return render_template("customer/verify_step2.html", customer=customer, step1_data=session.get("verify_step1", {}), step2_data=step2_draft)

            dup = conn.execute(
                "SELECT id FROM users WHERE LOWER(email) = ? AND id != ?",
                (email, user_id),
            ).fetchone()
            if dup:
                conn.close()
                flash("That email address is already used by another account.", "danger")
                return render_template("customer/verify_step2.html", customer=customer, step1_data=session.get("verify_step1", {}), step2_data=step2_draft)

            session["verify_step2"] = {
                "name": name,
                "email": email,
                "phone": clean_phone,
                "birthdate": birthdate,
                "gender": gender or "Prefer not to say",
                "address": address,
            }
            conn.close()
            session["verify_step"] = 3
            return redirect(url_for("verify.wizard"))

        # Handle Step 3 Submission: Confirm 1 & 2
        if current_step == 3 and request.form.get("wizard_step") == "3":
            step1_data = session.get("verify_step1")
            step2_data = session.get("verify_step2")

            if not customer["face_descriptor"] or not step1_data or not step2_data:
                conn.close()
                session["verify_step"] = 1
                flash("Verification session incomplete. Please complete Step 1 and Step 2.", "warning")
                return redirect(url_for("verify.wizard"))

            conn.execute(
                """
                UPDATE users
                SET name = ?, email = ?, phone = ?, birthdate = ?, gender = ?, address = ?,
                    id_type = ?, id_number = ?, id_ocr_name = ?, id_ocr_text = ?,
                    id_verified = 1, id_verified_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (
                    step2_data["name"],
                    step2_data["email"],
                    step2_data["phone"],
                    step2_data.get("birthdate", ""),
                    step2_data.get("gender", ""),
                    step2_data["address"],
                    step1_data["id_type"],
                    step1_data["id_number"],
                    step1_data["id_ocr_name"],
                    step1_data.get("id_ocr_text", ""),
                    user_id,
                ),
            )
            conn.commit()
            conn.close()

            session["user_name"] = step2_data["name"]
            session["user_email"] = step2_data["email"]
            session.pop("verify_step", None)
            session.pop("verify_step1", None)
            session.pop("verify_step2", None)

            flash("Verification and basic information confirmed and saved!", "success")
            return redirect(url_for("main.customer_settings"))

    # Server-side guard on GET: enforce prerequisites before rendering Step 2 or Step 3
    if current_step == 3 and (not customer["face_descriptor"] or not session.get("verify_step1") or not session.get("verify_step2")):
        current_step = 2 if (customer["face_descriptor"] and session.get("verify_step1")) else 1
        session["verify_step"] = current_step

    if current_step == 2 and (not customer["face_descriptor"] or not session.get("verify_step1")):
        current_step = 1
        session["verify_step"] = 1

    conn.close()
    if current_step == 2:
        return render_template(
            "customer/verify_step2.html",
            customer=customer,
            step1_data=session.get("verify_step1", {}),
            step2_data=session.get("verify_step2", {}),
        )
    if current_step == 3:
        return render_template(
            "customer/verify_step3.html",
            customer=customer,
            step1_data=session.get("verify_step1", {}),
            step2_data=session.get("verify_step2", {}),
        )
    return render_template("customer/verify_step1.html", customer=customer)


@verify_bp.route("/api/customer/face/enroll", methods=["POST"])
@customer_required
def api_face_enroll():
    data = request.get_json(silent=True) or {}
    descriptor = data.get("descriptor")
    if not isinstance(descriptor, list) or len(descriptor) != 128:
        return jsonify({"ok": False, "error": "Invalid 128-d face descriptor."}), 400
    try:
        clean_desc = [float(x) for x in descriptor]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Descriptor must be numeric."}), 400

    user_id = session.get("user_id")
    conn = get_db()
    conn.execute(
        "UPDATE users SET face_descriptor = ?, face_enrolled_at = datetime('now', 'localtime') WHERE id = ?",
        (json.dumps(clean_desc), user_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Face ID enrolled successfully!"})


@verify_bp.route("/api/customer/face/remove", methods=["POST"])
@customer_required
def api_face_remove():
    user_id = session.get("user_id")
    conn = get_db()
    conn.execute(
        "UPDATE users SET face_descriptor = NULL, face_enrolled_at = NULL WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()
    session["verify_step"] = 1
    return jsonify({"ok": True, "message": "Face ID removed."})


@verify_bp.route("/api/auth/face-login", methods=["POST"])
def api_face_login():
    data = request.get_json(silent=True) or {}
    descriptor = data.get("descriptor")
    email_hint = (data.get("email") or "").strip().lower()

    if not isinstance(descriptor, list) or len(descriptor) != 128:
        return jsonify({"ok": False, "error": "Invalid face descriptor."}), 400
    try:
        probe = [float(x) for x in descriptor]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid face descriptor format."}), 400

    conn = get_db()
    if email_hint:
        candidates = conn.execute(
            "SELECT * FROM users WHERE role = 'customer' AND is_active = 1 AND face_descriptor IS NOT NULL AND LOWER(email) = ?",
            (email_hint,),
        ).fetchall()
    else:
        candidates = conn.execute(
            "SELECT * FROM users WHERE role = 'customer' AND is_active = 1 AND face_descriptor IS NOT NULL"
        ).fetchall()

    best_user = None
    best_dist = 999.0
    for u in candidates:
        try:
            stored = json.loads(u["face_descriptor"])
            if len(stored) != 128:
                continue
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(probe, stored)))
            if dist < best_dist:
                best_dist = dist
                best_user = u
        except Exception:
            continue

    if best_user and best_dist <= 0.52:
        session["user_id"] = best_user["id"]
        session["role"] = best_user["role"]
        session["user_name"] = best_user["name"]
        session["user_email"] = best_user["email"]
        conn.close()
        return jsonify({
            "ok": True,
            "name": best_user["name"],
            "distance": round(best_dist, 4),
            "redirect": url_for("main.customer_dashboard"),
        })

    conn.close()
    return jsonify({
        "ok": False,
        "error": "Face not recognized. Please enroll Face ID in Customer Verification first.",
    }), 401

