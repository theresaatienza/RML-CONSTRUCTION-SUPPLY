from datetime import datetime
import random
import re
from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.models import get_db, create_notification, sync_customer_notifications
from app.routes import main_bp
from app.services import (
    expire_stale_reservations,
    find_and_reserve_delivery_slot,
    get_customer_cart_details,
)
from app.utils import (
    clean_order_code,
    format_notification_time,
    get_notification_date_group,
    login_required,
    validate_password_strength,
    validate_ph_phone,
)


@main_bp.route("/customer/dashboard")
@login_required("customer")
def customer_dashboard():
    user_id = session.get("user_id")
    conn = get_db()
    expire_stale_reservations(conn)
    sync_customer_notifications(conn, user_id)
    customer = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    orders = conn.execute(
        """
        SELECT o.*,
               (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id = o.id) AS item_count
        FROM orders o
        WHERE o.customer_id = ?
        ORDER BY o.created_at DESC
        """,
        (user_id,),
    ).fetchall()

    total_orders_count = len(orders)
    pending_orders_count = sum(1 for o in orders if o["order_status"] in ("pending", "pending_scheduling", "for_scheduling", "awaiting_confirmation"))
    processing_orders_count = sum(1 for o in orders if o["order_status"] in ("scheduled", "schedule_confirmed", "pending_payment", "pending_payment_verification", "payment_verified", "approved", "preparing"))
    active_deliveries_count = sum(1 for o in orders if o["order_status"] in ("ready_for_dispatch", "out_for_delivery", "in_transit") or (o["assigned_date"] and o["order_status"] not in ("delivered", "cancelled", "rejected")))
    awaiting_orders = [o for o in orders if o["order_status"] == "awaiting_confirmation"]
    awaiting_count = len(awaiting_orders)
    completed_orders_count = sum(1 for o in orders if o["order_status"] == "delivered")
    recent_orders = orders[:5]

    recent_updates = conn.execute(
        """
        SELECT n.*, o.order_code
        FROM notifications n
        LEFT JOIN orders o ON o.id = n.order_id
        WHERE n.user_id = ?
        ORDER BY n.created_at DESC
        LIMIT 6
        """,
        (user_id,),
    ).fetchall()

    unread_count = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (user_id,),
    ).fetchone()[0]

    conn.close()
    return render_template(
        "customer/dashboard.html",
        customer=customer,
        total_orders_count=total_orders_count,
        pending_orders_count=pending_orders_count,
        processing_orders_count=processing_orders_count,
        active_deliveries_count=active_deliveries_count,
        awaiting_count=awaiting_count,
        awaiting_orders=awaiting_orders,
        completed_orders_count=completed_orders_count,
        recent_orders=recent_orders,
        recent_updates=recent_updates,
        unread_count=unread_count,
    )


@main_bp.route("/customer/settings", methods=["GET", "POST"], endpoint="customer_settings")
@main_bp.route("/customer/profile", methods=["GET", "POST"], endpoint="customer_profile")
@login_required("customer")
def customer_settings():
    user_id = session.get("user_id")
    conn = get_db()
    customer = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if request.method == "POST":
        action = request.form.get("action")
        if action in ("update_profile", "update_info", "update_settings"):
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()

            if not name or not email or not phone or not address:
                flash("Please fill in all personal and delivery address fields.", "warning")
            elif not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                flash("Please enter a valid email address.", "danger")
            else:
                is_phone_valid, clean_phone = validate_ph_phone(phone)
                if not is_phone_valid:
                    flash("Please enter a valid Philippine mobile number (e.g. 09171234567 or +639171234567).", "danger")
                else:
                    existing_email = conn.execute(
                        "SELECT id FROM users WHERE LOWER(email) = ? AND id != ?",
                        (email, user_id),
                    ).fetchone()
                    if existing_email:
                        flash("This email address is already in use by another account.", "danger")
                    else:
                        conn.execute(
                            "UPDATE users SET name = ?, email = ?, phone = ?, address = ? WHERE id = ?",
                            (name, email, clean_phone, address, user_id),
                        )
                        conn.commit()
                        session["user_name"] = name
                        session["user_email"] = email
                        flash("Account profile and settings updated successfully.", "success")
                        conn.close()
                        return redirect(url_for("main.customer_settings"))

        elif action == "change_password":
            current_pwd = request.form.get("current_password", "")
            new_pwd = request.form.get("new_password", "")
            confirm_pwd = request.form.get("confirm_password", "") or request.form.get("confirm_new_password", "")

            if not current_pwd or not new_pwd:
                flash("Please fill in current and new password fields.", "warning")
            elif not check_password_hash(customer["password_hash"], current_pwd):
                flash("Current password is incorrect.", "danger")
            elif new_pwd != confirm_pwd:
                flash("New passwords do not match.", "danger")
            else:
                is_valid_pwd, pwd_err = validate_password_strength(new_pwd)
                if not is_valid_pwd:
                    flash(pwd_err, "danger")
                else:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new_pwd), user_id),
                    )
                    conn.commit()
                    flash("Password updated successfully! Please use your new password next time you sign in.", "success")
                    conn.close()
                    return redirect(url_for("main.customer_settings"))

    orders_count = conn.execute("SELECT COUNT(*) FROM orders WHERE customer_id = ?", (user_id,)).fetchone()[0]
    customer = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return render_template("customer/profile.html", customer=customer, orders_count=orders_count)


@main_bp.route("/customer/tracking")
@login_required("customer")
def customer_tracking():
    user_id = session.get("user_id")
    conn = get_db()
    expire_stale_reservations(conn)

    deliveries = conn.execute(
        """
        SELECT o.id AS order_id, o.order_code, o.order_status, o.total_amount,
               o.delivery_address, o.assigned_date, o.assigned_time_slot,
               o.created_at AS order_date,
               d.id AS delivery_id, d.delivery_code, d.delivery_status,
               d.current_lat, d.current_lng, d.last_gps_update, d.delivered_at,
               d.recipient_received_by, d.quantity_received, d.completion_time,
               t.truck_code, t.plate_number, t.capacity AS truck_capacity,
               u_drv.name AS driver_name, u_drv.phone AS driver_phone
        FROM orders o
        LEFT JOIN deliveries d ON d.order_id = o.id
        LEFT JOIN trucks t ON t.id = d.truck_id
        LEFT JOIN users u_drv ON u_drv.id = d.driver_id
        WHERE o.customer_id = ?
        ORDER BY o.created_at DESC
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    active_deliveries = [
        d for d in deliveries
        if (d["delivery_status"] in ("en_route", "loading", "ready_for_dispatch", "authorized", "proposed", "assigned")
            or d["order_status"] in ("ready_for_dispatch", "out_for_delivery", "in_transit", "scheduled", "schedule_confirmed"))
    ]
    completed_deliveries = [
        d for d in deliveries
        if (d["delivery_status"] == "delivered" or d["order_status"] == "delivered")
    ]

    return render_template(
        "customer/delivery_tracking.html",
        deliveries=deliveries,
        active_deliveries=active_deliveries,
        completed_deliveries=completed_deliveries,
    )


@main_bp.route("/customer/notifications")
@login_required("customer")
def customer_notifications():
    user_id = session.get("user_id")
    conn = get_db()
    sync_customer_notifications(conn, user_id)

    filter_type = request.args.get("type", "").strip().lower()

    base_query = """
        SELECT n.*, o.order_code
        FROM notifications n
        LEFT JOIN orders o ON o.id = n.order_id
        WHERE n.user_id = ?
    """
    params = [user_id]

    if filter_type in ("order", "schedule", "payment", "delivery"):
        base_query += " AND n.notification_type = ?"
        params.append(filter_type)
    elif filter_type == "unread":
        base_query += " AND n.is_read = 0"

    base_query += " ORDER BY n.created_at DESC, n.id DESC"

    notifications_raw = conn.execute(base_query, tuple(params)).fetchall()

    all_notifs = conn.execute(
        "SELECT id, is_read, notification_type FROM notifications WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    unread_count = sum(1 for n in all_notifs if not n["is_read"])
    total_count = len(all_notifs)

    conn.close()

    now = datetime.now()
    notifications = []
    grouped_notifications = {
        "Today": [],
        "Yesterday": [],
        "Earlier": [],
    }

    for row in notifications_raw:
        item = dict(row)
        item["relative_time"] = format_notification_time(item.get("created_at"), now)
        grp = get_notification_date_group(item.get("created_at"), now)
        item["date_group"] = grp
        notifications.append(item)
        if grp in grouped_notifications:
            grouped_notifications[grp].append(item)
        else:
            grouped_notifications["Earlier"].append(item)

    return render_template(
        "customer/notifications.html",
        notifications=notifications,
        grouped_notifications=grouped_notifications,
        unread_count=unread_count,
        total_count=total_count,
        active_filter=filter_type,
    )


@main_bp.route("/customer/notifications/<int:notification_id>/read", methods=["POST"])
@login_required("customer")
def customer_notification_mark_read(notification_id):
    user_id = session.get("user_id")
    conn = get_db()
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    conn.commit()
    conn.close()
    flash("Notification marked as read.", "success")
    return redirect(url_for("main.customer_notifications"))


@main_bp.route("/customer/notifications/mark-all-read", methods=["POST"])
@login_required("customer")
def customer_notifications_mark_all_read():
    user_id = session.get("user_id")
    conn = get_db()
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()
    flash("All notifications marked as read.", "success")
    return redirect(url_for("main.customer_notifications"))


@main_bp.route("/customer/cart", methods=["GET"])
@login_required("customer")
def customer_cart():
    """Shopping Cart view: Allows reviewing and managing customer-selected products."""
    user_id = session.get("user_id")
    conn = get_db()
    cart_data = get_customer_cart_details(conn, user_id)
    conn.close()
    return render_template(
        "customer/cart.html",
        cart_items=cart_data["items"],
        subtotal=cart_data["subtotal"],
        delivery_fee=cart_data["delivery_fee"],
        vat_amount=cart_data["vat_amount"],
        total_amount=cart_data["total_amount"],
        total_items_count=cart_data["total_items_count"],
    )


@main_bp.route("/customer/catalog")
def customer_catalog():
    if session.get("role") in ("admin", "staff"):
        return redirect(url_for("main.admin_dashboard"))
    elif session.get("role") == "driver":
        return redirect(url_for("main.driver_dashboard"))

    if not session.get("user_id") or session.get("role") != "customer":
        flash("You need to sign in to access the complete product catalog.", "warning")
        next_target = request.full_path if (request.query_string and request.query_string.strip()) else request.path
        if next_target.endswith("?"):
            next_target = next_target[:-1]
        return redirect(url_for("main.login", next=next_target))

    search = request.args.get("q", "").strip() or request.args.get("search", "").strip()
    category_id = request.args.get("category", "").strip()
    stock_filter = request.args.get("stock", "").strip()
    sort_filter = request.args.get("sort", "").strip()
    min_price = request.args.get("min_price", "").strip()
    max_price = request.args.get("max_price", "").strip()

    conn = get_db()
    categories = conn.execute(
        """
        SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id AND p.is_active = 1) AS product_count
        FROM categories c
        WHERE (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id AND p.is_active = 1) > 0
        ORDER BY c.name
        """
    ).fetchall()

    query = """
        SELECT p.*, c.name AS category_name
        FROM products p
        JOIN categories c ON p.category_id = c.id
        WHERE p.is_active = 1
    """
    params = []
    if search:
        query += " AND (p.name LIKE ? OR p.description LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if category_id:
        query += " AND p.category_id = ?"
        params.append(category_id)
    if stock_filter == "instock":
        query += " AND p.stock_quantity > 0"
    if min_price:
        try:
            query += " AND p.price >= ?"
            params.append(float(min_price))
        except ValueError:
            pass
    if max_price:
        try:
            query += " AND p.price <= ?"
            params.append(float(max_price))
        except ValueError:
            pass

    if sort_filter == "price_asc":
        query += " ORDER BY p.price ASC"
    elif sort_filter == "price_desc":
        query += " ORDER BY p.price DESC"
    elif sort_filter == "name_asc":
        query += " ORDER BY p.name ASC"
    else:
        query += " ORDER BY p.id"

    products_raw = conn.execute(query, params).fetchall()

    # Load all active product variations
    all_vars = conn.execute(
        "SELECT * FROM product_variations WHERE is_available = 1 ORDER BY price ASC"
    ).fetchall()
    vars_by_prod = {}
    for v in all_vars:
        vars_by_prod.setdefault(v["product_id"], []).append(dict(v))

    products = []
    for p in products_raw:
        p_dict = dict(p)
        p_dict["variations"] = vars_by_prod.get(p["id"], [])
        products.append(p_dict)

    # Calculate cart total from customer database cart
    cart_total = 0.0
    if session.get("user_id"):
        c_items = conn.execute(
            """
            SELECT ci.quantity, COALESCE(pv.price, p.price) AS price
            FROM cart_items ci
            JOIN products p ON ci.product_id = p.id
            LEFT JOIN product_variations pv ON ci.variation_id = pv.id
            WHERE ci.user_id = ? AND p.is_active = 1
            """,
            (session["user_id"],),
        ).fetchall()
        for ci in c_items:
            cart_total += float(ci["quantity"]) * float(ci["price"])

    all_in_stock = all(p["stock_quantity"] > 0 for p in products)
    conn.close()

    return render_template(
        "customer/catalog.html",
        products=products,
        categories=categories,
        search=search,
        q=search,
        selected_category=category_id,
        stock_filter=stock_filter,
        sort_filter=sort_filter,
        min_price=min_price,
        max_price=max_price,
        cart_total=round(cart_total, 2),
        all_in_stock=all_in_stock,
    )


@main_bp.route("/customer/cart/add", methods=["POST"])
@login_required("customer")
def cart_add():
    user_id = session.get("user_id")
    product_id = request.form.get("product_id")
    variation_id = request.form.get("variation_id")
    try:
        qty = int(request.form.get("quantity", 1))
    except (ValueError, TypeError):
        qty = 1
    if qty < 1:
        qty = 1

    conn = get_db()
    prod = conn.execute(
        "SELECT id, name, unit, price, stock_quantity FROM products WHERE id = ? AND is_active = 1",
        (product_id,)
    ).fetchone()
    if not prod:
        conn.close()
        flash("Product not found or currently unavailable.", "danger")
        return redirect(url_for("main.customer_catalog"))

    display_name = prod["name"]
    max_stock = prod["stock_quantity"]
    var_id_int = None

    if variation_id and str(variation_id).strip():
        try:
            var_id_int = int(variation_id)
        except (ValueError, TypeError):
            var_id_int = None

    if var_id_int:
        var_row = conn.execute(
            "SELECT id, variation_name, price, unit, stock_quantity FROM product_variations WHERE id = ? AND product_id = ? AND is_available = 1",
            (var_id_int, product_id)
        ).fetchone()
        if var_row:
            display_name = f"{prod['name']} ({var_row['variation_name']})"
            max_stock = var_row["stock_quantity"]
        else:
            var_id_int = None

    if max_stock <= 0:
        conn.close()
        flash(f"Sorry, {display_name} is currently out of stock.", "warning")
        return redirect(request.referrer or url_for("main.customer_catalog"))

    # Check if item is already in user's cart in database
    if var_id_int:
        existing = conn.execute(
            "SELECT id, quantity FROM cart_items WHERE user_id = ? AND product_id = ? AND variation_id = ?",
            (user_id, product_id, var_id_int)
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, quantity FROM cart_items WHERE user_id = ? AND product_id = ? AND variation_id IS NULL",
            (user_id, product_id)
        ).fetchone()

    if existing:
        new_qty = existing["quantity"] + qty
        if max_stock > 0 and new_qty > max_stock:
            new_qty = max_stock
            flash(f"Updated {display_name} quantity in your cart to available stock ({max_stock}).", "warning")
        else:
            flash(f"Added {qty} item(s) to your cart: {display_name}.", "success")
        conn.execute(
            "UPDATE cart_items SET quantity = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (new_qty, existing["id"])
        )
    else:
        if max_stock > 0 and qty > max_stock:
            qty = max_stock
            flash(f"Added {qty} item(s) to your cart: {display_name} (adjusted to available stock).", "warning")
        else:
            flash(f"Added {qty} item(s) to your cart: {display_name}.", "success")
        conn.execute(
            "INSERT INTO cart_items (user_id, product_id, variation_id, quantity) VALUES (?, ?, ?, ?)",
            (user_id, product_id, var_id_int, qty)
        )

    conn.commit()
    conn.close()

    # Clear legacy session cart so database cart is single source of truth
    session.pop("cart", None)

    return redirect(request.referrer or url_for("main.customer_catalog"))


@main_bp.route("/customer/cart/update", methods=["POST"])
@login_required("customer")
def cart_update():
    user_id = session.get("user_id")
    cart_item_id = request.form.get("cart_item_id")
    try:
        qty = int(request.form.get("quantity", 0))
    except (ValueError, TypeError):
        qty = 0

    conn = get_db()
    item = None
    if cart_item_id and str(cart_item_id).isdigit():
        item = conn.execute(
            """
            SELECT ci.id, ci.user_id, ci.product_id, ci.variation_id, ci.quantity,
                   p.name AS product_name, p.stock_quantity AS product_stock, p.is_active,
                   pv.variation_name, pv.stock_quantity AS variation_stock
            FROM cart_items ci
            JOIN products p ON ci.product_id = p.id
            LEFT JOIN product_variations pv ON ci.variation_id = pv.id
            WHERE ci.id = ? AND ci.user_id = ?
            """,
            (int(cart_item_id), user_id)
        ).fetchone()

    # Fallback to cart_key (pid or pid:vid)
    if not item and request.form.get("cart_key"):
        cart_key = str(request.form.get("cart_key"))
        parts = cart_key.split(":")
        pid = int(parts[0]) if parts[0].isdigit() else 0
        vid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if vid:
            item = conn.execute(
                """
                SELECT ci.id, ci.user_id, ci.product_id, ci.variation_id, ci.quantity,
                       p.name AS product_name, p.stock_quantity AS product_stock, p.is_active,
                       pv.variation_name, pv.stock_quantity AS variation_stock
                FROM cart_items ci
                JOIN products p ON ci.product_id = p.id
                LEFT JOIN product_variations pv ON ci.variation_id = pv.id
                WHERE ci.user_id = ? AND ci.product_id = ? AND ci.variation_id = ?
                """,
                (user_id, pid, vid)
            ).fetchone()
        elif pid:
            item = conn.execute(
                """
                SELECT ci.id, ci.user_id, ci.product_id, ci.variation_id, ci.quantity,
                       p.name AS product_name, p.stock_quantity AS product_stock, p.is_active,
                       pv.variation_name, pv.stock_quantity AS variation_stock
                FROM cart_items ci
                JOIN products p ON ci.product_id = p.id
                LEFT JOIN product_variations pv ON ci.variation_id = pv.id
                WHERE ci.user_id = ? AND ci.product_id = ? AND ci.variation_id IS NULL
                """,
                (user_id, pid)
            ).fetchone()

    if not item:
        conn.close()
        flash("Cart item not found.", "warning")
        return redirect(url_for("main.customer_cart"))

    item_name = f"{item['product_name']} ({item['variation_name']})" if item['variation_name'] else item['product_name']
    available_stock = item['variation_stock'] if item['variation_id'] else item['product_stock']
    available_stock = int(available_stock or 0)

    if qty <= 0:
        conn.execute("DELETE FROM cart_items WHERE id = ? AND user_id = ?", (item["id"], user_id))
        conn.commit()
        conn.close()
        session.pop("cart", None)
        flash(f"Removed {item_name} from your cart.", "info")
        return redirect(url_for("main.customer_cart"))

    if available_stock > 0 and qty > available_stock:
        qty = available_stock
        flash(f"Quantity for {item_name} adjusted to maximum available stock ({available_stock}).", "warning")
    else:
        flash("Cart updated.", "success")

    conn.execute(
        "UPDATE cart_items SET quantity = ?, updated_at = datetime('now', 'localtime') WHERE id = ? AND user_id = ?",
        (qty, item["id"], user_id)
    )
    conn.commit()
    conn.close()
    session.pop("cart", None)
    return redirect(url_for("main.customer_cart"))


@main_bp.route("/customer/cart/remove/<int:item_id>", methods=["POST"])
@login_required("customer")
def cart_remove(item_id):
    user_id = session.get("user_id")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    if cur.rowcount > 0:
        flash("Item removed from your shopping cart.", "info")
    else:
        flash("Cart item not found.", "warning")
    conn.commit()
    conn.close()
    session.pop("cart", None)
    return redirect(url_for("main.customer_cart"))


@main_bp.route("/customer/cart/clear", methods=["POST"])
@login_required("customer")
def cart_clear():
    user_id = session.get("user_id")
    conn = get_db()
    conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    session.pop("cart", None)
    flash("Your shopping cart has been cleared.", "info")
    return redirect(url_for("main.customer_cart"))


@main_bp.route("/customer/checkout", methods=["GET", "POST"])
@login_required("customer")
def customer_checkout():
    user_id = session.get("user_id")
    conn = get_db()

    cart_data = get_customer_cart_details(conn, user_id)
    cart_items = cart_data["items"]
    subtotal = cart_data["subtotal"]
    delivery_fee = cart_data["delivery_fee"]
    vat_amount = cart_data["vat_amount"]
    total_amount = cart_data["total_amount"]

    if not cart_items:
        conn.close()
        flash("Your shopping cart is empty. Browse our catalog and add construction materials to get started.", "warning")
        return redirect(url_for("main.customer_cart"))

    if request.method == "POST":
        recipient_name = request.form.get("recipient_name", "").strip()
        phone = request.form.get("phone", "").strip() or request.form.get("recipient_phone", "").strip()
        delivery_address = request.form.get("delivery_address", "").strip()
        special_instructions = request.form.get("special_instructions", "").strip()

        if not recipient_name or not delivery_address:
            flash("Please provide recipient name and delivery address.", "warning")
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            conn.close()
            return render_template(
                "customer/checkout.html",
                cart_items=cart_items,
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                vat_amount=vat_amount,
                total_amount=total_amount,
                user=user,
            )

        # Automatic Delivery Availability Check & Proposal
        proposed_slot = find_and_reserve_delivery_slot(conn)

        if proposed_slot:
            order_status = "awaiting_confirmation"
            schedule_status = "proposed"
            assigned_date = proposed_slot["assigned_date"]
            assigned_time_slot = proposed_slot["assigned_time_slot"]
            reservation_expires_at = proposed_slot["expires_at"]
            flash(
                f"Your order has been submitted! We automatically proposed the next available delivery schedule: "
                f"{assigned_date} ({assigned_time_slot}). Please review and accept this schedule within "
                f"{proposed_slot['timeout_hours']} hours to proceed to payment.",
                "success",
            )
        else:
            order_status = "pending_scheduling"
            schedule_status = "unassigned"
            assigned_date = None
            assigned_time_slot = None
            reservation_expires_at = None
            flash(
                "Your order has been submitted! All immediate delivery schedules are currently fully booked. "
                "Our administrator will review your order and assign a delivery schedule as soon as capacity opens.",
                "info",
            )

        order_code = f"#{random.randint(1053, 9999)}"
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO orders (
                order_code, customer_id, recipient_name, phone, delivery_address,
                special_instructions, subtotal, delivery_fee, vat_amount, total_amount,
                order_status, schedule_status, assigned_date, assigned_time_slot,
                reservation_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_code,
                user_id,
                recipient_name,
                phone,
                delivery_address,
                special_instructions,
                subtotal,
                delivery_fee,
                vat_amount,
                total_amount,
                order_status,
                schedule_status,
                assigned_date,
                assigned_time_slot,
                reservation_expires_at,
            ),
        )
        order_id = cur.lastrowid

        for item in cart_items:
            var_id = item["variation_id"]
            var_name = item["variation_name"]
            cur.execute(
                """
                INSERT INTO order_items (order_id, product_id, variation_id, variation_name, quantity, unit_price, subtotal)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, item["product_id"], var_id, var_name, item["quantity"], item["price"], item["line_total"]),
            )
            # Deduct stock
            if var_id:
                cur.execute(
                    "UPDATE product_variations SET stock_quantity = MAX(0, stock_quantity - ?) WHERE id = ?",
                    (item["quantity"], var_id),
                )
            cur.execute(
                "UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?) WHERE id = ?",
                (item["quantity"], item["product_id"]),
            )

        if proposed_slot:
            while True:
                d_code = f"DLV-{datetime.now().strftime('%m%d')}-{random.randint(1000, 9999)}"
                if not conn.execute("SELECT id FROM deliveries WHERE delivery_code = ?", (d_code,)).fetchone():
                    break
            cur.execute(
                """
                INSERT INTO deliveries (
                    delivery_code, order_id, driver_id, truck_id, delivery_window,
                    delivery_status, current_lat, current_lng
                ) VALUES (?, ?, ?, ?, ?, 'proposed', 13.9411, 121.1631)
                """,
                (d_code, order_id, proposed_slot["driver_id"], proposed_slot["truck_id"], assigned_time_slot),
            )

        create_notification(
            conn,
            user_id,
            "Order Submitted Successfully",
            f"Order {order_code} for ₱{total_amount:,.2f} has been submitted and is queued for scheduling.",
            "order",
            order_id,
        )
        if proposed_slot:
            create_notification(
                conn,
                user_id,
                "Delivery Schedule Proposed",
                f"Delivery schedule for order {order_code} has been proposed: {assigned_date} ({assigned_time_slot}). Please review and accept.",
                "schedule",
                order_id,
            )

        # Clear customer's shopping cart in database and session only after order is placed
        cur.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
        session.pop("cart", None)

        conn.commit()
        conn.close()
        return redirect(url_for("main.customer_order_confirmation", order_id=order_id))

    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return render_template(
        "customer/checkout.html",
        cart_items=cart_items,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        vat_amount=vat_amount,
        total_amount=total_amount,
        user=user,
    )


@main_bp.route("/customer/orders/<int:order_id>/confirmation")
@login_required("customer")
def customer_order_confirmation(order_id):
    """Post-checkout confirmation view displaying submitted order details and waiting notice."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.customer_orders"))

    if session.get("role") != "admin" and order["customer_id"] != session["user_id"]:
        conn.close()
        flash("Unauthorized access to this order.", "danger")
        return redirect(url_for("main.customer_orders"))

    items = conn.execute(
        """
        SELECT oi.*, p.name, p.unit FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (order_id,),
    ).fetchall()

    delivery = conn.execute(
        """
        SELECT d.*, u.name AS driver_name, t.truck_code, t.plate_number
        FROM deliveries d
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        WHERE d.order_id = ?
        """,
        (order_id,),
    ).fetchone()
    conn.close()

    return render_template(
        "customer/order_confirmation.html",
        order=order,
        items=items,
        delivery=delivery,
    )


@main_bp.route("/customer/payment/<int:order_id>", methods=["GET", "POST"])
@login_required("customer")
def customer_payment(order_id):
    """Multi-step payment flow matching high-fidelity mockups.
    Enforces that payment cannot be accessed until schedule is approved by customer."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.customer_orders"))

    # Security check: only order owner or admin
    if session.get("role") != "admin" and order["customer_id"] != session["user_id"]:
        conn.close()
        flash("Unauthorized access to this order.", "danger")
        return redirect(url_for("main.customer_orders"))

    # Strict workflow check: payment is NOT available until admin assigned and customer accepted schedule
    if order["order_status"] in ("pending", "pending_scheduling", "for_scheduling", "awaiting_confirmation", "cancelled", "rejected"):
        conn.close()
        if order["order_status"] == "awaiting_confirmation":
            flash("Please review and accept your admin-assigned delivery schedule before proceeding to payment.", "warning")
            return redirect(url_for("main.customer_orders"))
        elif order["order_status"] in ("cancelled", "rejected"):
            flash("Payment cannot be processed for cancelled orders.", "danger")
            return redirect(url_for("main.customer_orders"))
        else:
            flash("Payment is not available yet. Please wait for an administrator to review your order and assign an available delivery schedule.", "warning")
            return redirect(url_for("main.customer_orders"))

    items = conn.execute(
        """
        SELECT oi.*, p.name, p.unit FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (order_id,),
    ).fetchall()

    existing_payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()

    # Step navigation: 1 (method select), 2 (details), 3 (confirmation), 4 (approved)
    step = request.args.get("step", type=int) or 1
    method = request.args.get("method", "") or (existing_payment["method"] if existing_payment else "cash")

    if request.method == "POST":
        action = request.form.get("action", "")
        method = request.form.get("method", method or "cash")

        if action == "to_details":
            conn.close()
            return redirect(url_for("main.customer_payment", order_id=order_id, step=2, method=method))

        elif action == "review_receipt":
            # Pass details in session temporarily for preview
            session["payment_draft"] = {
                "order_id": order_id,
                "method": method,
                "payer_name": request.form.get("payer_name", "").strip() or order["recipient_name"],
                "mobile_number": request.form.get("mobile_number", "").strip() or order["phone"],
                "bank_used": request.form.get("bank_used", "").strip(),
                "reference_number": request.form.get("reference_number", "").strip() or request.form.get("reference_no", "").strip(),
                "receipt_note": request.form.get("receipt_note", "").strip(),
                "payment_datetime": request.form.get("payment_datetime", "").strip() or datetime.now().strftime("%d %b %Y · %I:%M %p"),
                "amount": float(request.form.get("amount", order["total_amount"]) or order["total_amount"]),
            }
            conn.close()
            return redirect(url_for("main.customer_payment", order_id=order_id, step=3, method=method))

        elif action == "confirm_payment":
            draft = session.pop("payment_draft", {})
            payer_name = draft.get("payer_name") or request.form.get("payer_name") or order["recipient_name"]
            bank_used = draft.get("bank_used") or request.form.get("bank_used", "")
            ref = draft.get("reference_number") or request.form.get("reference_number") or request.form.get("reference_no", "")
            note = draft.get("receipt_note") or request.form.get("receipt_note", "")

            if method == "cash":
                ref = ref or f"COD-{clean_order_code(order['order_code']).replace('#', '')}"
                new_payment_status = "payment_due"
                flash_msg = "Cash on Delivery arrangement confirmed! Awaiting administrator approval before release for delivery."
            else:
                new_payment_status = "pending_verification"
                flash_msg = "QR payment information submitted! Awaiting administrator verification before release for delivery."

            if existing_payment:
                conn.execute(
                    """
                    UPDATE payments
                    SET method=?, reference_number=?, receipt_note=?, payer_name=?, bank_used=?,
                        payment_status=?, paid_at=datetime('now','localtime'), rejection_reason=NULL
                    WHERE order_id=?
                    """,
                    (method, ref, note, payer_name, bank_used, new_payment_status, order_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO payments (order_id, method, reference_number, receipt_note, payer_name, bank_used, amount, payment_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, method, ref, note, payer_name, bank_used, order["total_amount"], new_payment_status),
                )

            # Move order status to pending_payment_verification
            conn.execute(
                """
                UPDATE orders
                SET order_status = 'pending_payment_verification',
                    rejection_reason = NULL
                WHERE id = ?
                """,
                (order_id,),
            )
            create_notification(
                conn,
                session["user_id"],
                "Payment Submitted",
                f"Payment for order {clean_order_code(order['order_code'])} ({method.upper()}) was submitted and is pending administrator verification.",
                "payment",
                order_id,
            )
            conn.commit()
            conn.close()
            flash(flash_msg, "success")
            return redirect(url_for("main.customer_payment", order_id=order_id, step=4, method=method))

    draft = session.get("payment_draft", {})
    conn.close()
    return render_template(
        "customer/payment.html",
        order=order,
        items=items,
        payment=existing_payment,
        draft=draft,
        step=step,
        method=method,
    )


@main_bp.route("/customer/orders")
@login_required("customer")
def customer_orders():
    conn = get_db()
    expire_stale_reservations(conn)
    orders = conn.execute(
        """
        SELECT o.*, p.method AS payment_method, p.payment_status, p.reference_number,
               p.rejection_reason AS payment_rejection_reason,
               d.delivery_status, d.delivery_code,
               t.truck_code, t.plate_number, t.capacity AS truck_capacity,
               u_drv.name AS driver_name, u_drv.phone AS driver_phone
        FROM orders o
        LEFT JOIN payments p ON o.id = p.order_id
        LEFT JOIN deliveries d ON o.id = d.order_id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u_drv ON d.driver_id = u_drv.id
        WHERE o.customer_id = ?
        ORDER BY o.id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return render_template("customer/orders.html", orders=orders)


@main_bp.route("/customer/order/<int:order_id>/accept-schedule", methods=["POST"])
@login_required("customer")
def customer_accept_schedule(order_id):
    conn = get_db()
    expire_stale_reservations(conn)

    order = conn.execute(
        "SELECT * FROM orders WHERE id = ? AND customer_id = ?",
        (order_id, session["user_id"]),
    ).fetchone()

    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.customer_orders"))

    # Backend validation: check if reservation has expired or order is not awaiting confirmation
    if order["order_status"] != "awaiting_confirmation":
        conn.close()
        if order["schedule_status"] == "expired":
            flash("Your proposed delivery schedule reservation has expired. Please check with administrator for an updated schedule.", "warning")
        else:
            flash(f"Order {clean_order_code(order['order_code'])} is not currently awaiting schedule confirmation.", "warning")
        return redirect(url_for("main.customer_orders"))

    if order["reservation_expires_at"]:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if order["reservation_expires_at"] <= now_str:
            expire_stale_reservations(conn)
            conn.close()
            flash("Your schedule reservation has expired. The reserved delivery slot has been released.", "warning")
            return redirect(url_for("main.customer_orders"))

    conn.execute(
        """
        UPDATE orders
        SET order_status = 'schedule_confirmed',
            schedule_status = 'accepted',
            schedule_confirmed_at = datetime('now', 'localtime')
        WHERE id = ?
        """,
        (order_id,),
    )
    conn.execute(
        "UPDATE deliveries SET delivery_status = 'confirmed' WHERE order_id = ?",
        (order_id,),
    )
    create_notification(
        conn,
        session["user_id"],
        "Schedule Confirmed",
        f"You confirmed the delivery schedule for order {clean_order_code(order['order_code'])}. You may now proceed to payment.",
        "schedule",
        order_id,
    )
    conn.commit()
    conn.close()
    flash(
        f"Delivery schedule for {clean_order_code(order['order_code'])} confirmed ({order['assigned_date']}, {order['assigned_time_slot']})! Please choose your preferred payment method below.",
        "success",
    )
    return redirect(url_for("main.customer_payment", order_id=order_id, step=1))


@main_bp.route("/customer/order/<int:order_id>/cancel", methods=["POST"])
@login_required()
def customer_cancel_order(order_id):
    conn = get_db()
    is_admin = session.get("role") == "admin"
    if is_admin:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    else:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND customer_id = ?",
            (order_id, session["user_id"]),
        ).fetchone()

    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.customer_orders" if not is_admin else "main.admin_orders"))

    # Backend validation: cannot cancel if out for delivery, in transit, delivered, or already cancelled
    if order["order_status"] in ("out_for_delivery", "in_transit", "delivered", "cancelled"):
        conn.close()
        flash("This order has already progressed beyond the stage where cancellation is allowed.", "danger")
        return redirect(url_for("main.customer_orders" if not is_admin else "main.admin_orders"))

    cancellation_reason = request.form.get("cancellation_reason", "").strip() or "Cancelled by customer (schedule inconvenient or requested cancellation)"

    # 1. Update order status to 'cancelled'
    conn.execute(
        """
        UPDATE orders
        SET order_status = 'cancelled',
            schedule_status = 'cancelled',
            rejection_reason = ?
        WHERE id = ?
        """,
        (cancellation_reason, order_id),
    )

    # 2. Release assigned delivery resources (truck & driver)
    delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order_id,)).fetchone()
    if delivery:
        if delivery["truck_id"]:
            other_active = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE truck_id = ? AND id != ? AND delivery_status IN ('assigned', 'loading', 'en_route')",
                (delivery["truck_id"], delivery["id"]),
            ).fetchone()[0]
            if other_active == 0:
                conn.execute("UPDATE trucks SET status = 'available' WHERE id = ?", (delivery["truck_id"],))
        conn.execute("DELETE FROM deliveries WHERE id = ?", (delivery["id"],))

    # 3. Restore inventory if stock was previously deducted
    logs = conn.execute(
        "SELECT * FROM inventory_logs WHERE remarks LIKE ?",
        (f"%Order {clean_order_code(order['order_code'])}%",),
    ).fetchall()
    if logs:
        order_items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
        for item in order_items:
            conn.execute(
                "UPDATE products SET stock_quantity = stock_quantity + ? WHERE id = ?",
                (item["quantity"], item["product_id"]),
            )
            conn.execute(
                "INSERT INTO inventory_logs (product_id, movement_type, quantity, remarks, created_by) VALUES (?, 'stock_in', ?, ?, ?)",
                (item["product_id"], item["quantity"], f"Cancelled Order {clean_order_code(order['order_code'])} stock restored", session.get("user_id")),
            )

    create_notification(
        conn,
        order["customer_id"],
        "Order Cancelled",
        f"Order {clean_order_code(order['order_code'])} has been cancelled.",
        "order",
        order_id,
    )

    conn.commit()
    conn.close()
    flash(
        f"Order {clean_order_code(order['order_code'])} has been cancelled. Reserved truck, driver, and delivery resources have been released.",
        "info",
    )
    return redirect(url_for("main.customer_orders" if not is_admin else "main.admin_orders"))


@main_bp.route("/customer/orders/<int:order_id>/tracking")
@login_required("customer")
def customer_order_tracking(order_id):
    conn = get_db()
    expire_stale_reservations(conn)
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.customer_orders"))

    if session.get("role") != "admin" and order["customer_id"] != session["user_id"]:
        conn.close()
        flash("Unauthorized access to this order tracking.", "danger")
        return redirect(url_for("main.customer_orders"))

    items = conn.execute(
        """
        SELECT oi.*, p.name, p.unit FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (order_id,),
    ).fetchall()
    payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
    delivery = conn.execute(
        """
        SELECT d.*, u.name AS driver_name, u.phone AS driver_phone,
               t.truck_code, t.plate_number
        FROM deliveries d
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        WHERE d.order_id = ?
        """,
        (order_id,),
    ).fetchone()
    conn.close()

    return render_template(
        "customer/tracking.html",
        order=order,
        items=items,
        payment=payment,
        delivery=delivery,
    )


@main_bp.route("/customer/payment-history")
@login_required("customer")
def customer_payment_history():
    conn = get_db()
    payments = conn.execute(
        """
        SELECT p.*, o.order_code, o.created_at AS order_date, o.id AS order_id
        FROM payments p
        JOIN orders o ON p.order_id = o.id
        WHERE o.customer_id = ?
        ORDER BY p.id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return render_template("customer/payment_history.html", payments=payments)


@main_bp.route("/customer/support")
@login_required("customer")
def customer_support():
    return render_template("customer/support.html")


@main_bp.route("/order/<int:order_id>/invoice")
@login_required()
def order_invoice(order_id):
    """Generate and display clean printable tax invoice for an order."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.home"))

    user_role = session.get("role")
    if user_role not in ("admin", "staff") and order["customer_id"] != session.get("user_id"):
        conn.close()
        flash("Unauthorized access to this invoice.", "danger")
        return redirect(url_for("main.customer_orders"))

    customer = conn.execute("SELECT * FROM users WHERE id = ?", (order["customer_id"],)).fetchone()
    items = conn.execute(
        """
        SELECT oi.*, p.name, p.unit FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (order_id,),
    ).fetchall()
    payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
    delivery = conn.execute(
        """
        SELECT d.*, u.name AS driver_name, t.truck_code, t.plate_number
        FROM deliveries d
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        WHERE d.order_id = ?
        """,
        (order_id,),
    ).fetchone()
    conn.close()

    return render_template(
        "order_invoice.html",
        order=order,
        customer=customer,
        items=items,
        payment=payment,
        delivery=delivery,
    )

