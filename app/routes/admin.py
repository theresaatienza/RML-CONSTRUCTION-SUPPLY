from datetime import datetime, timedelta
import random
from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from app.models import get_db, create_notification
from app.routes import main_bp
from app.services import (
    RML_DEPOT_ADDRESS,
    RML_DEPOT_LAT,
    RML_DEPOT_LNG,
    RML_DEPOT_NAME,
    expire_stale_reservations,
    get_delivery_setting,
    is_gps_stale,
    resolve_delivery_coords,
)
from app.utils import (
    clean_order_code,
    log_access_event,
    login_required,
    normalize_image_url,
)


@main_bp.route("/admin/deliveries/<int:delivery_id>/tracking")
@login_required("admin")
def admin_delivery_live_tracking(delivery_id):
    """Dedicated full-page Live Delivery Route & GPS Tracking console for Administrator."""
    conn = get_db()
    deliv = conn.execute(
        """
        SELECT d.*,
               u.name AS driver_name, u.phone AS driver_phone,
               t.truck_code, t.plate_number, t.capacity AS truck_capacity,
               o.id AS order_id, o.order_code, o.order_status, o.recipient_name,
               o.phone AS recipient_phone, o.delivery_address, o.special_instructions,
               o.assigned_date, o.assigned_time_slot, o.total_amount
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        WHERE d.id = ?
        """,
        (delivery_id,),
    ).fetchone()

    if not deliv:
        conn.close()
        flash("Delivery record not found.", "danger")
        return redirect(url_for("main.admin_deliveries"))

    # Resolve destination coordinates
    dest_lat = deliv["dest_lat"]
    dest_lng = deliv["dest_lng"]
    if not dest_lat or not dest_lng:
        dest_lat, dest_lng = resolve_delivery_coords(deliv["delivery_address"])
        conn.execute("UPDATE deliveries SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, delivery_id))
        conn.execute("UPDATE orders SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, deliv["order_id"]))
        conn.commit()

    # Retrieve all active deliveries for quick switcher
    active_deliveries = conn.execute(
        """
        SELECT d.id, d.delivery_code, d.delivery_status, o.order_code, o.recipient_name, t.truck_code
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        ORDER BY d.id DESC
        """
    ).fetchall()

    # Retrieve latest breadcrumbs
    breadcrumbs = conn.execute(
        """
        SELECT latitude, longitude, accuracy, recorded_at
        FROM delivery_locations
        WHERE delivery_id = ?
        ORDER BY id DESC
        LIMIT 15
        """,
        (delivery_id,),
    ).fetchall()

    items = conn.execute(
        """
        SELECT oi.*, p.name AS product_name, p.unit
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (deliv["order_id"],),
    ).fetchall()
    conn.close()

    is_stale = is_gps_stale(deliv["last_gps_update"], deliv["delivery_status"])

    return render_template(
        "admin/delivery_live_tracking.html",
        delivery=deliv,
        active_deliveries=active_deliveries,
        breadcrumbs=breadcrumbs,
        items=items,
        is_stale=is_stale,
        depot_lat=RML_DEPOT_LAT,
        depot_lng=RML_DEPOT_LNG,
        depot_name=RML_DEPOT_NAME,
        depot_address=RML_DEPOT_ADDRESS,
        dest_lat=dest_lat,
        dest_lng=dest_lng,
    )


@main_bp.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    conn = get_db()
    total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    total_products = conn.execute("SELECT COUNT(*) FROM products WHERE is_active = 1").fetchone()[0]
    total_customers = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'customer' AND is_active = 1").fetchone()[0]
    total_revenue = conn.execute("SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE order_status IN ('payment_verified', 'ready_for_dispatch', 'out_for_delivery', 'in_transit', 'delivered')").fetchone()[0]
    pending_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('pending', 'pending_scheduling')").fetchone()[0]
    low_stock = conn.execute("SELECT COUNT(*) FROM products WHERE stock_quantity <= low_stock_threshold").fetchone()[0]
    in_progress_deliveries = conn.execute("SELECT COUNT(*) FROM deliveries WHERE delivery_status IN ('assigned', 'loading', 'en_route')").fetchone()[0]
    stock_in = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM inventory_logs WHERE movement_type = 'stock_in'").fetchone()[0]
    stock_out = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM inventory_logs WHERE movement_type = 'stock_out'").fetchone()[0]
    payments_count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    ready_dispatch = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE order_status IN ('for_scheduling', 'approved', 'awaiting_confirmation', 'schedule_confirmed', 'scheduled')"
    ).fetchone()[0]
    pending_receipts = conn.execute("SELECT COUNT(*) FROM payments WHERE payment_status = 'pending'").fetchone()[0]

    recent_orders = conn.execute(
        """
        SELECT o.*, u.name AS customer_name, p.method AS payment_method, p.payment_status
        FROM orders o
        JOIN users u ON o.customer_id = u.id
        LEFT JOIN payments p ON o.id = p.order_id
        ORDER BY o.id DESC LIMIT 6
        """
    ).fetchall()

    recent_deliveries = conn.execute(
        """
        SELECT d.*, o.order_code, o.delivery_address, t.truck_code, u.name AS driver_name
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u ON d.driver_id = u.id
        ORDER BY d.id DESC LIMIT 4
        """
    ).fetchall()

    conn.close()

    return render_template(
        "admin/dashboard.html",
        total_orders=total_orders,
        total_products=total_products,
        total_customers=total_customers,
        total_revenue=total_revenue,
        pending_orders=pending_orders,
        low_stock=low_stock,
        in_progress_deliveries=in_progress_deliveries,
        stock_in=stock_in,
        stock_out=stock_out,
        payments_count=payments_count,
        ready_dispatch=ready_dispatch,
        pending_receipts=pending_receipts,
        recent_orders=recent_orders,
        recent_deliveries=recent_deliveries,
    )


@main_bp.route("/admin/orders")
@login_required("admin")
def admin_orders():
    tab = request.args.get("tab", "pending")
    conn = get_db()
    expire_stale_reservations(conn)

    base_sql = """
        SELECT o.*, u.name AS customer_name, u.phone AS customer_phone,
               p.method AS payment_method, p.payment_status, p.reference_number AS payment_ref,
               p.rejection_reason AS payment_rejection_reason,
               u_auth.name AS authorized_by_name,
               MIN(CASE WHEN pr.stock_quantity >= oi.quantity THEN 1 ELSE 0 END) AS all_in_stock
        FROM orders o
        JOIN users u ON o.customer_id = u.id
        LEFT JOIN payments p ON o.id = p.order_id
        LEFT JOIN users u_auth ON o.authorized_by = u_auth.id
        LEFT JOIN order_items oi ON o.id = oi.order_id
        LEFT JOIN products pr ON oi.product_id = pr.id
    """
    if tab in ("pending", "pending_scheduling"):
        where = " WHERE o.order_status IN ('pending', 'pending_scheduling')"
    elif tab == "for_scheduling":
        where = " WHERE o.order_status IN ('for_scheduling', 'approved')"
    elif tab == "awaiting_confirmation":
        where = " WHERE o.order_status = 'awaiting_confirmation'"
    elif tab == "payment_verification":
        where = " WHERE o.order_status IN ('schedule_confirmed', 'pending_payment', 'pending_payment_verification')"
    elif tab == "ready_authorization":
        where = " WHERE o.order_status = 'payment_verified'"
    elif tab in ("scheduled", "ready", "ready_for_dispatch"):
        where = " WHERE o.order_status IN ('ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit', 'preparing')"
    elif tab in ("cancelled", "rejected"):
        where = " WHERE o.order_status IN ('cancelled', 'rejected')"
    else:
        where = ""

    orders = conn.execute(
        base_sql + where + " GROUP BY o.id ORDER BY o.id DESC"
    ).fetchall()

    counts = {
        "pending": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('pending', 'pending_scheduling')").fetchone()[0],
        "for_scheduling": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('for_scheduling', 'approved')").fetchone()[0],
        "awaiting_confirmation": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status='awaiting_confirmation'").fetchone()[0],
        "payment_verification": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('schedule_confirmed', 'pending_payment', 'pending_payment_verification')").fetchone()[0],
        "ready_authorization": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'payment_verified'").fetchone()[0],
        "scheduled": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit', 'preparing')").fetchone()[0],
        "cancelled": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('cancelled', 'rejected')").fetchone()[0],
        "all": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
    }

    items_by_order = {}
    all_items = conn.execute(
        """
        SELECT oi.*, p.name, p.unit, p.stock_quantity,
               (p.stock_quantity >= oi.quantity) AS is_available,
               MAX(0, p.stock_quantity - oi.quantity) AS remaining_after
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        """
    ).fetchall()
    for item in all_items:
        items_by_order.setdefault(item["order_id"], []).append(item)

    trucks = conn.execute("SELECT * FROM trucks ORDER BY truck_code").fetchall()
    drivers = conn.execute("SELECT * FROM users WHERE role = 'driver' AND is_active = 1 ORDER BY name").fetchall()

    deliv_rows = conn.execute(
        """
        SELECT d.*, t.truck_code, t.plate_number, t.capacity AS truck_capacity, u.name AS driver_name, u.phone AS driver_phone
        FROM deliveries d
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u ON d.driver_id = u.id
        """
    ).fetchall()
    deliveries_by_order = {d["order_id"]: d for d in deliv_rows}

    existing_schedules = conn.execute(
        """
        SELECT o.id, o.order_code, o.assigned_date, o.assigned_time_slot, o.order_status,
               t.truck_code, t.plate_number, u.name AS driver_name
        FROM orders o
        JOIN deliveries d ON o.id = d.order_id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u ON d.driver_id = u.id
        WHERE o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'pending_payment_verification', 'payment_verified', 'ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit')
          AND o.assigned_date IS NOT NULL
        ORDER BY o.assigned_date ASC, o.assigned_time_slot ASC
        """
    ).fetchall()

    conn.close()
    return render_template(
        "admin/orders.html",
        orders=orders,
        tab=tab,
        counts=counts,
        pending_count=counts["pending"],
        for_scheduling_count=counts["for_scheduling"],
        awaiting_count=counts["awaiting_confirmation"],
        scheduled_count=counts["scheduled"],
        cancelled_count=counts["cancelled"],
        all_count=counts["all"],
        items_by_order=items_by_order,
        trucks=trucks,
        drivers=drivers,
        deliveries_by_order=deliveries_by_order,
        existing_schedules=existing_schedules,
    )


@main_bp.route("/admin/orders/<int:order_id>/assign-schedule", methods=["POST"])
@login_required("admin")
def admin_order_assign_schedule(order_id):
    assigned_date = request.form.get("assigned_date", "").strip()
    assigned_time_slot = request.form.get("assigned_time_slot", "").strip()
    try:
        truck_id = int(request.form.get("truck_id", 0))
        driver_id = int(request.form.get("driver_id", 0))
    except (ValueError, TypeError):
        truck_id = 0
        driver_id = 0

    approval_notes = request.form.get("approval_notes", "").strip()

    if not assigned_date or not assigned_time_slot or not truck_id or not driver_id:
        flash("Please provide delivery date, time slot, truck, and driver.", "warning")
        return redirect(url_for("main.admin_orders", tab="for_scheduling"))

    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.admin_orders"))

    # CONFLICT CHECK: Is this truck or driver already assigned to another order on the same date and time slot?
    conflict = conn.execute(
        """
        SELECT o.order_code, o.assigned_date, o.assigned_time_slot,
               t.truck_code, t.plate_number, u.name AS driver_name,
               (d.truck_id = ?) AS truck_conflict,
               (d.driver_id = ?) AS driver_conflict
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u ON d.driver_id = u.id
        WHERE o.id != ?
          AND o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'pending_payment_verification', 'payment_verified', 'ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit')
          AND o.assigned_date = ?
          AND o.assigned_time_slot = ?
          AND (d.truck_id = ? OR d.driver_id = ?)
        """,
        (truck_id, driver_id, order_id, assigned_date, assigned_time_slot, truck_id, driver_id),
    ).fetchone()

    if conflict:
        conn.close()
        conflict_msg = []
        if conflict["truck_conflict"]:
            conflict_msg.append(f"Truck {conflict['truck_code']} ({conflict['plate_number']})")
        if conflict["driver_conflict"]:
            conflict_msg.append(f"Driver {conflict['driver_name']}")
        reason = " and ".join(conflict_msg)
        flash(
            f"Scheduling Conflict: {reason} is already booked for Order {clean_order_code(conflict['order_code'])} on {assigned_date} ({assigned_time_slot}). Please select a different truck, driver, or time slot.",
            "danger",
        )
        return redirect(url_for("main.admin_orders", tab="for_scheduling"))

    # If pending or pending_scheduling, deduct stock if not yet deducted
    if order["order_status"] in ("pending", "pending_scheduling"):
        items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
        for item in items:
            conn.execute(
                "UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?) WHERE id = ?",
                (item["quantity"], item["product_id"]),
            )
            conn.execute(
                "INSERT INTO inventory_logs (product_id, movement_type, quantity, remarks, created_by) VALUES (?, 'stock_out', ?, ?, ?)",
                (item["product_id"], item["quantity"], f"Approved & Scheduled Order {clean_order_code(order['order_code'])}", session["user_id"]),
            )

    conn.execute(
        """
        UPDATE orders
        SET assigned_date = ?,
            assigned_time_slot = ?,
            preferred_date = ?,
            delivery_slot = ?,
            order_status = 'awaiting_confirmation',
            schedule_status = 'proposed',
            approval_notes = COALESCE(NULLIF(?, ''), approval_notes)
        WHERE id = ?
        """,
        (assigned_date, assigned_time_slot, assigned_date, assigned_time_slot, approval_notes, order_id),
    )

    existing_deliv = conn.execute("SELECT id FROM deliveries WHERE order_id = ?", (order_id,)).fetchone()
    if existing_deliv:
        conn.execute(
            """
            UPDATE deliveries
            SET driver_id = ?, truck_id = ?, delivery_window = ?, delivery_status = 'assigned'
            WHERE order_id = ?
            """,
            (driver_id, truck_id, assigned_time_slot, order_id),
        )
    else:
        while True:
            d_code = f"DLV-{datetime.now().strftime('%m%d')}-{random.randint(1000, 9999)}"
            if not conn.execute("SELECT id FROM deliveries WHERE delivery_code = ?", (d_code,)).fetchone():
                break
        conn.execute(
            """
            INSERT INTO deliveries (delivery_code, order_id, driver_id, truck_id, delivery_window, delivery_status, current_lat, current_lng)
            VALUES (?, ?, ?, ?, ?, 'assigned', 13.9411, 121.1631)
            """,
            (d_code, order_id, driver_id, truck_id, assigned_time_slot),
        )

    create_notification(
        conn,
        order["customer_id"],
        "Delivery Schedule Proposed",
        f"Admin assigned a delivery schedule for order {clean_order_code(order['order_code'])}: {assigned_date} ({assigned_time_slot}). Please review and accept.",
        "schedule",
        order_id,
    )

    conn.commit()
    conn.close()
    flash(
        f"Delivery schedule for {clean_order_code(order['order_code'])} assigned ({assigned_date}, {assigned_time_slot}). Customer notified to accept.",
        "success",
    )
    return redirect(url_for("main.admin_orders", tab="awaiting_confirmation"))


@main_bp.route("/admin/orders/<int:order_id>/decision", methods=["POST"])
@login_required("admin")
def admin_order_decision(order_id):
    decision = request.form.get("decision")
    reason = request.form.get("rejection_reason", "").strip()
    approval_notes = request.form.get("approval_notes", "").strip()
    conn = get_db()

    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.admin_orders"))

    if decision == "approve":
        items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
        for item in items:
            conn.execute(
                "UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?) WHERE id = ?",
                (item["quantity"], item["product_id"]),
            )
            conn.execute(
                "INSERT INTO inventory_logs (product_id, movement_type, quantity, remarks, created_by) VALUES (?, 'stock_out', ?, ?, ?)",
                (item["product_id"], item["quantity"], f"Approved Order {clean_order_code(order['order_code'])}", session["user_id"]),
            )
        conn.execute(
            "UPDATE orders SET order_status = 'for_scheduling', approval_notes = ? WHERE id = ?",
            (approval_notes, order_id),
        )
        flash(f"Order {clean_order_code(order['order_code'])} approved and stock reserved. Ready for delivery scheduling.", "success")
        conn.commit()
        conn.close()
        return redirect(url_for("main.admin_orders", tab="for_scheduling"))

    elif decision == "reject":
        conn.execute(
            "UPDATE orders SET order_status = 'cancelled', schedule_status = 'cancelled', rejection_reason = ? WHERE id = ?",
            (reason or "Rejected upon admin review", order_id),
        )
        flash(f"Order {clean_order_code(order['order_code'])} rejected.", "warning")
        conn.commit()
        conn.close()
        return redirect(url_for("main.admin_orders", tab="pending"))

    conn.commit()
    conn.close()
    return redirect(url_for("main.admin_orders"))


@main_bp.route("/admin/orders/<int:order_id>/authorize-delivery", methods=["POST"])
@login_required("admin")
def admin_order_authorize_delivery(order_id):
    """Admin delivery authorization gate: Ensures payment verified & schedule accepted before driver release."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.admin_orders"))

    payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
    delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order_id,)).fetchone()

    # Rule 1: Delivery schedule must have been confirmed / accepted
    if not order["assigned_date"] or order["schedule_status"] != "accepted":
        conn.close()
        flash(f"Cannot authorize delivery: Delivery schedule for {clean_order_code(order['order_code'])} has not been confirmed by customer.", "warning")
        return redirect(url_for("main.admin_orders", tab="ready_authorization"))

    # Rule 2: Delivery assignment (truck and driver) must exist
    if not delivery or not delivery["driver_id"] or not delivery["truck_id"]:
        conn.close()
        flash(f"Cannot authorize delivery: No truck and driver assigned to {clean_order_code(order['order_code'])}. Please assign logistics first.", "warning")
        return redirect(url_for("main.admin_orders", tab="ready_authorization"))

    # Rule 3: Payment must be verified (or COD arrangement approved)
    if not payment:
        conn.close()
        flash(f"Cannot authorize delivery: No payment record found for {clean_order_code(order['order_code'])}.", "danger")
        return redirect(url_for("main.admin_orders", tab="ready_authorization"))

    if payment["method"] == "qr" and payment["payment_status"] not in ("verified", "paid"):
        conn.close()
        flash(f"Cannot authorize delivery: QR Payment for {clean_order_code(order['order_code'])} has not been verified yet. Admin must confirm payment receipt first.", "danger")
        return redirect(url_for("main.admin_payments"))

    if payment["method"] == "cash" and order["order_status"] not in ("payment_verified", "ready_for_dispatch") and payment["payment_status"] not in ("payment_due", "verified", "paid"):
        conn.close()
        flash(f"Cannot authorize delivery: Cash on Delivery arrangement for {clean_order_code(order['order_code'])} has not been approved yet.", "warning")
        return redirect(url_for("main.admin_payments"))

    # Prevent duplicate authorization
    if order["order_status"] == "ready_for_dispatch" and delivery["delivery_status"] == "authorized":
        conn.close()
        flash(f"Order {clean_order_code(order['order_code'])} is already authorized for dispatch.", "info")
        return redirect(url_for("main.admin_orders", tab="scheduled"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE orders
        SET order_status = 'ready_for_dispatch',
            authorized_by = ?,
            authorized_at = ?
        WHERE id = ?
        """,
        (session["user_id"], now_str, order_id),
    )
    conn.execute(
        """
        UPDATE deliveries
        SET delivery_status = 'authorized',
            authorized_by = ?,
            authorized_at = ?
        WHERE id = ?
        """,
        (session["user_id"], now_str, delivery["id"]),
    )
    create_notification(
        conn,
        order["customer_id"],
        "Ready for Dispatch",
        f"Order {clean_order_code(order['order_code'])} has been authorized for delivery. Assigned truck is preparing for dispatch.",
        "delivery",
        order_id,
    )
    conn.commit()
    conn.close()
    flash(f"Order {clean_order_code(order['order_code'])} authorized for delivery! The assigned driver has been released to begin dispatch.", "success")
    return redirect(url_for("main.admin_orders", tab="scheduled"))


@main_bp.route("/admin/orders/<int:order_id>/cancel", methods=["POST"])
@login_required("admin")
def admin_order_cancel(order_id):
    """Admin-controlled cancellation for eligible orders prior to road dispatch."""
    conn = get_db()
    order = conn.execute(
        """
        SELECT o.*, u.name AS customer_name, u.email AS customer_email
        FROM orders o
        JOIN users u ON o.customer_id = u.id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()

    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("main.admin_orders"))

    # Eligibility Validation: Do not allow ordinary cancellation for dispatched or completed orders
    INELIGIBLE_STATUSES = ("out_for_delivery", "in_transit", "delivered", "cancelled", "rejected")
    if order["order_status"] in INELIGIBLE_STATUSES:
        conn.close()
        flash(
            f"Order {clean_order_code(order['order_code'])} cannot be cancelled because its status is '{order['order_status'].replace('_', ' ')}'. "
            "Dispatched orders must undergo the formal delivery exception or return workflow.",
            "danger",
        )
        return redirect(url_for("main.admin_orders"))

    # Extract cancellation reason and notes
    reason_category = request.form.get("reason_category", "").strip() or "Business decision"
    cancellation_notes = request.form.get("cancellation_notes", "").strip()
    if cancellation_notes:
        full_reason = f"{reason_category}: {cancellation_notes}"
    else:
        full_reason = reason_category

    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    admin_id = session.get("user_id")
    admin_name = session.get("user_name", "Admin")

    # 1. Update Order Status and store responsible admin + cancellation timestamp
    conn.execute(
        """
        UPDATE orders
        SET order_status = 'cancelled',
            schedule_status = 'cancelled',
            rejection_reason = ?,
            approval_notes = COALESCE(approval_notes, '') || ?,
            authorized_by = ?,
            authorized_at = ?
        WHERE id = ?
        """,
        (
            full_reason,
            f" [Cancelled by Admin {admin_name} on {now_time}: {full_reason}]",
            admin_id,
            now_time,
            order_id,
        ),
    )

    # 2. Release assigned delivery and truck resources
    delivery = conn.execute("SELECT * FROM deliveries WHERE order_id = ?", (order_id,)).fetchone()
    if delivery:
        if delivery["truck_id"]:
            other_active = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE truck_id = ? AND id != ? AND delivery_status IN ('assigned', 'loading', 'en_route')",
                (delivery["truck_id"], delivery["id"]),
            ).fetchone()[0]
            if other_active == 0:
                conn.execute("UPDATE trucks SET status = 'available' WHERE id = ?", (delivery["truck_id"],))
        # Keep delivery record retained with status 'cancelled'
        conn.execute("UPDATE deliveries SET delivery_status = 'cancelled' WHERE id = ?", (delivery["id"],))

    # 3. Inventory Stock Restoration:
    # Ensure stock is restored only once and recorded in Stock Movement History
    already_restored = conn.execute(
        "SELECT COUNT(*) FROM inventory_logs WHERE movement_type = 'stock_in' AND remarks LIKE ?",
        (f"%Cancelled Order {clean_order_code(order['order_code'])}%",),
    ).fetchone()[0]

    if already_restored == 0:
        order_items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
        for item in order_items:
            # Restore product yard stock
            conn.execute(
                "UPDATE products SET stock_quantity = stock_quantity + ? WHERE id = ?",
                (item["quantity"], item["product_id"]),
            )
            # Restore variation stock if applicable
            if item["variation_id"]:
                conn.execute(
                    "UPDATE product_variations SET stock_quantity = stock_quantity + ? WHERE id = ?",
                    (item["quantity"], item["variation_id"]),
                )
            # Record in Stock Movement History (inventory_logs)
            conn.execute(
                """
                INSERT INTO inventory_logs (product_id, movement_type, quantity, remarks, created_by, created_at)
                VALUES (?, 'stock_in', ?, ?, ?, ?)
                """,
                (
                    item["product_id"],
                    item["quantity"],
                    f"Restored stock from Cancelled Order {clean_order_code(order['order_code'])} (Reason: {reason_category})",
                    admin_id,
                    now_time,
                ),
            )

    # 4. Payment Handling:
    # Keep payment status separate from order status.
    payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
    payment_notice = ""
    if payment:
        if payment["payment_status"] in ("verified", "paid"):
            # Flag verified payments for financial refund review without marking as refunded
            flag_note = f" [FLAGGED FOR REFUND REVIEW: Order cancelled by admin on {now_time}. Reason: {reason_category}]"
            conn.execute(
                "UPDATE payments SET receipt_note = COALESCE(receipt_note, '') || ? WHERE id = ?",
                (flag_note, payment["id"]),
            )
            payment_notice = " Notice: This order had a verified payment. It has been flagged for financial refund review."
        elif payment["payment_status"] == "pending_verification":
            void_note = f" [QR Verification Voided: Order cancelled by admin on {now_time}. Reason: {reason_category}]"
            conn.execute(
                "UPDATE payments SET receipt_note = COALESCE(receipt_note, '') || ? WHERE id = ?",
                (void_note, payment["id"]),
            )
            payment_notice = " Notice: Pending QR payment verification has been voided."
        elif payment["payment_status"] in ("pending", "payment_due", "unpaid"):
            unpaid_note = f" [Order cancelled by admin: Payment collection voided on {now_time}]"
            conn.execute(
                "UPDATE payments SET receipt_note = COALESCE(receipt_note, '') || ? WHERE id = ?",
                (unpaid_note, payment["id"]),
            )

    # 5. Customer Notification via existing notification system
    create_notification(
        conn,
        order["customer_id"],
        "Order Cancelled",
        f"Your order {clean_order_code(order['order_code'])} was cancelled by administration. Reason: {full_reason}.",
        "order",
        order_id,
    )

    # 6. Audit Logging in access_logs
    log_access_event(
        conn,
        "Order Cancelled by Admin",
        {"id": order["customer_id"], "name": order["customer_name"], "email": order["customer_email"], "role": "customer"},
        admin_id,
        admin_name,
        f"Cancelled order {clean_order_code(order['order_code'])}. Reason: {full_reason}",
    )

    conn.commit()
    conn.close()

    flash(
        f"Order {clean_order_code(order['order_code'])} has been successfully cancelled. Customer notified and stock restored.{payment_notice}",
        "warning",
    )
    return redirect(url_for("main.admin_orders", tab="all"))


@main_bp.route("/admin/inventory", methods=["GET", "POST"])
@login_required("admin")
def admin_inventory():
    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_product":
            name = request.form.get("name", "").strip()
            category_id = request.form.get("category_id")
            description = request.form.get("description", "").strip()
            unit = request.form.get("unit", "pc").strip()
            price = float(request.form.get("price", 0))
            stock_quantity = int(request.form.get("stock_quantity", 0))
            low_stock_threshold = int(request.form.get("low_stock_threshold", 20))
            image_url = normalize_image_url(request.form.get("image_url", "").strip()) if request.form.get("image_url", "").strip() else "https://images.unsplash.com/photo-1589939705384-5185137a7f0f?w=500&auto=format&fit=crop"

            conn.execute(
                "INSERT INTO products (category_id, name, description, unit, price, stock_quantity, low_stock_threshold, image_url, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (category_id, name, description, unit, price, stock_quantity, low_stock_threshold, image_url),
            )
            conn.commit()
            flash(f"Product '{name}' added successfully.", "success")

        elif action == "update_product":
            product_id = int(request.form.get("product_id"))
            name = request.form.get("name", "").strip()
            category_id = request.form.get("category_id")
            description = request.form.get("description", "").strip()
            unit = request.form.get("unit", "pc").strip()
            price = float(request.form.get("price", 0))
            stock_quantity = int(request.form.get("stock_quantity", 0))
            low_stock_threshold = int(request.form.get("low_stock_threshold", 20))
            raw_img = request.form.get("image_url", "").strip()
            image_url = normalize_image_url(raw_img) if raw_img else ""

            if image_url:
                conn.execute(
                    """
                    UPDATE products
                    SET category_id=?, name=?, description=?, unit=?, price=?, stock_quantity=?, low_stock_threshold=?, image_url=?
                    WHERE id=?
                    """,
                    (category_id, name, description, unit, price, stock_quantity, low_stock_threshold, image_url, product_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                    SET category_id=?, name=?, description=?, unit=?, price=?, stock_quantity=?, low_stock_threshold=?
                    WHERE id=?
                    """,
                    (category_id, name, description, unit, price, stock_quantity, low_stock_threshold, product_id),
                )
            conn.commit()
            flash(f"Product '{name}' updated successfully.", "success")

        elif action == "add_variation":
            product_id = int(request.form.get("product_id"))
            variation_name = request.form.get("variation_name", "").strip()
            price = float(request.form.get("price", 0))
            unit = request.form.get("unit", "").strip()
            stock_quantity = int(request.form.get("stock_quantity", 50))

            conn.execute(
                """
                INSERT INTO product_variations (product_id, variation_name, price, unit, stock_quantity, is_available)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (product_id, variation_name, price, unit, stock_quantity),
            )
            conn.commit()
            flash(f"Variation '{variation_name}' added successfully.", "success")

        elif action == "update_variation":
            variation_id = int(request.form.get("variation_id"))
            variation_name = request.form.get("variation_name", "").strip()
            price = float(request.form.get("price", 0))
            unit = request.form.get("unit", "").strip()
            stock_quantity = int(request.form.get("stock_quantity", 0))
            is_available = int(request.form.get("is_available", 1))

            conn.execute(
                """
                UPDATE product_variations
                SET variation_name = ?, price = ?, unit = ?, stock_quantity = ?, is_available = ?
                WHERE id = ?
                """,
                (variation_name, price, unit, stock_quantity, is_available, variation_id),
            )
            conn.commit()
            flash(f"Variation '{variation_name}' updated successfully.", "success")

        elif action == "delete_variation":
            variation_id = int(request.form.get("variation_id"))
            conn.execute("UPDATE product_variations SET is_available = 0 WHERE id = ?", (variation_id,))
            conn.commit()
            flash("Variation deactivated successfully.", "info")

        elif action == "stock_movement":
            product_id = int(request.form.get("product_id"))
            movement_type = request.form.get("movement_type", "stock_in")
            quantity = int(request.form.get("quantity", 0))
            supplier_ref = request.form.get("reference", "").strip()
            remarks_text = request.form.get("remarks", "").strip()
            full_remarks = f"{supplier_ref} · {remarks_text}" if supplier_ref else remarks_text

            if movement_type == "stock_in":
                conn.execute("UPDATE products SET stock_quantity = stock_quantity + ? WHERE id = ?", (quantity, product_id))
            else:
                conn.execute("UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?) WHERE id = ?", (quantity, product_id))

            conn.execute(
                "INSERT INTO inventory_logs (product_id, movement_type, quantity, remarks, created_by) VALUES (?, ?, ?, ?, ?)",
                (product_id, movement_type, quantity, full_remarks, session["user_id"]),
            )
            conn.commit()
            flash("Stock movement successfully recorded.", "success")

        conn.close()
        return redirect(url_for("main.admin_inventory"))

    products = [dict(p) for p in conn.execute(
        "SELECT p.*, c.name AS category_name FROM products p JOIN categories c ON p.category_id = c.id WHERE p.is_active = 1 ORDER BY p.id"
    ).fetchall()]

    all_variations = [dict(v) for v in conn.execute(
        "SELECT * FROM product_variations WHERE is_available = 1 ORDER BY price ASC"
    ).fetchall()]
    vars_by_product = {}
    for v in all_variations:
        vars_by_product.setdefault(v["product_id"], []).append(v)
    for p in products:
        p["variations"] = vars_by_product.get(p["id"], [])

    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    low_stock_count = conn.execute("SELECT COUNT(*) FROM products WHERE stock_quantity <= low_stock_threshold AND is_active = 1").fetchone()[0]
    logs = conn.execute(
        """
        SELECT l.*, p.name AS product_name, u.name AS staff_name
        FROM inventory_logs l
        JOIN products p ON l.product_id = p.id
        LEFT JOIN users u ON l.created_by = u.id
        ORDER BY l.id DESC LIMIT 15
        """
    ).fetchall()
    conn.close()

    return render_template(
        "admin/inventory.html",
        products=products,
        categories=categories,
        low_stock_count=low_stock_count,
        logs=logs,
    )


@main_bp.route("/admin/deliveries", methods=["GET", "POST"])
@login_required("admin")
def admin_deliveries():
    conn = get_db()
    expire_stale_reservations(conn)

    if request.method == "POST":
        try:
            order_id = int(request.form.get("order_id", 0))
            driver_id = int(request.form.get("driver_id", 0))
            truck_id = int(request.form.get("truck_id", 0))
        except (ValueError, TypeError):
            order_id = driver_id = truck_id = 0

        assigned_date = request.form.get("assigned_date", "").strip() or datetime.now().strftime("%Y-%m-%d")
        assigned_time_slot = request.form.get("assigned_time_slot", "").strip() or request.form.get("delivery_window", "Morning (08:00 - 12:00)").strip()

        if not order_id or not driver_id or not truck_id:
            flash("Please select an order, driver, and truck.", "warning")
            return redirect(url_for("main.admin_deliveries"))

        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            conn.close()
            flash("Order not found.", "danger")
            return redirect(url_for("main.admin_deliveries"))

        # CONFLICT CHECK: Does truck or driver have overlapping delivery on assigned_date & time slot?
        conflict = conn.execute(
            """
            SELECT o.order_code, o.assigned_date, o.assigned_time_slot,
                   t.truck_code, t.plate_number, u.name AS driver_name,
                   (d.truck_id = ?) AS truck_conflict,
                   (d.driver_id = ?) AS driver_conflict
            FROM deliveries d
            JOIN orders o ON d.order_id = o.id
            LEFT JOIN trucks t ON d.truck_id = t.id
            LEFT JOIN users u ON d.driver_id = u.id
            WHERE o.id != ?
              AND o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'scheduled', 'out_for_delivery', 'in_transit')
              AND o.assigned_date = ?
              AND o.assigned_time_slot = ?
              AND (d.truck_id = ? OR d.driver_id = ?)
            """,
            (truck_id, driver_id, order_id, assigned_date, assigned_time_slot, truck_id, driver_id),
        ).fetchone()

        if conflict:
            conn.close()
            conflict_msg = []
            if conflict["truck_conflict"]:
                conflict_msg.append(f"Truck {conflict['truck_code']} ({conflict['plate_number']})")
            if conflict["driver_conflict"]:
                conflict_msg.append(f"Driver {conflict['driver_name']}")
            reason = " and ".join(conflict_msg)
            flash(
                f"Scheduling Conflict: {reason} is already booked for Order {clean_order_code(conflict['order_code'])} on {assigned_date} ({assigned_time_slot}). Please select an alternate vehicle, driver, or time slot.",
                "danger",
            )
            return redirect(url_for("main.admin_deliveries"))

        existing = conn.execute("SELECT id FROM deliveries WHERE order_id = ?", (order_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE deliveries SET driver_id=?, truck_id=?, delivery_window=?, delivery_status='assigned' WHERE order_id=?",
                (driver_id, truck_id, assigned_time_slot, order_id),
            )
        else:
            while True:
                d_code = f"DLV-{datetime.now().strftime('%m%d')}-{random.randint(1000, 9999)}"
                if not conn.execute("SELECT id FROM deliveries WHERE delivery_code = ?", (d_code,)).fetchone():
                    break
            conn.execute(
                """
                INSERT INTO deliveries (delivery_code, order_id, driver_id, truck_id, delivery_window, delivery_status, current_lat, current_lng)
                VALUES (?, ?, ?, ?, ?, 'assigned', 13.9411, 121.1631)
                """,
                (d_code, order_id, driver_id, truck_id, assigned_time_slot),
            )

        new_status = "scheduled" if order["schedule_status"] == "accepted" else "awaiting_confirmation"
        new_sched_status = "accepted" if order["schedule_status"] == "accepted" else "proposed"
        conn.execute(
            """
            UPDATE orders
            SET assigned_date = ?,
                assigned_time_slot = ?,
                preferred_date = ?,
                delivery_slot = ?,
                order_status = ?,
                schedule_status = ?
            WHERE id = ?
            """,
            (assigned_date, assigned_time_slot, assigned_date, assigned_time_slot, new_status, new_sched_status, order_id),
        )
        conn.commit()
        flash(f"Delivery scheduled for {assigned_date} ({assigned_time_slot})! Order status: {new_status.replace('_', ' ').title()}.", "success")
        conn.close()
        return redirect(url_for("main.admin_deliveries"))

    counts = {
        "for_scheduling": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('for_scheduling', 'approved')").fetchone()[0],
        "awaiting_confirmation": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'awaiting_confirmation'").fetchone()[0],
        "scheduled": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('scheduled', 'schedule_confirmed')").fetchone()[0],
        "en_route": conn.execute("SELECT COUNT(*) FROM deliveries WHERE delivery_status = 'en_route'").fetchone()[0],
        "delivered": conn.execute("SELECT COUNT(*) FROM deliveries WHERE delivery_status = 'delivered'").fetchone()[0],
        "assigned": conn.execute("SELECT COUNT(*) FROM deliveries WHERE delivery_status IN ('assigned', 'loading')").fetchone()[0],
        "queued": conn.execute("SELECT COUNT(*) FROM orders WHERE order_status IN ('for_scheduling', 'approved')").fetchone()[0],
    }

    ready_orders = conn.execute(
        """
        SELECT o.*, u.name AS customer_name
        FROM orders o
        JOIN users u ON o.customer_id = u.id
        WHERE o.order_status IN ('pending', 'pending_scheduling', 'for_scheduling', 'approved', 'awaiting_confirmation', 'schedule_confirmed', 'scheduled')
        ORDER BY o.id DESC
        """
    ).fetchall()

    drivers = conn.execute("SELECT * FROM users WHERE role='driver' AND is_active=1 ORDER BY name").fetchall()
    trucks = conn.execute("SELECT * FROM trucks ORDER BY truck_code").fetchall()
    deliveries = conn.execute(
        """
        SELECT d.*, o.order_code, o.recipient_name, o.delivery_address, o.assigned_date, o.assigned_time_slot, o.order_status,
               u.name AS driver_name, t.truck_code, t.plate_number,
               p.payment_status, p.method AS payment_method
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN payments p ON o.id = p.order_id
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        ORDER BY d.id DESC
        """
    ).fetchall()
    conn.close()

    return render_template(
        "admin/deliveries.html",
        counts=counts,
        ready_orders=ready_orders,
        drivers=drivers,
        trucks=trucks,
        deliveries=deliveries,
    )


@main_bp.route("/admin/delivery-availability", methods=["GET", "POST"])
@login_required("admin")
def admin_delivery_availability():
    """Admin delivery schedule availability and capacity management."""
    conn = get_db()
    expire_stale_reservations(conn)

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "update_settings":
            try:
                timeout_h = max(1, int(request.form.get("reservation_timeout_hours", 24)))
                cap = max(1, int(request.form.get("default_slot_capacity", 3)))
                lead = max(0, int(request.form.get("lead_time_days", 1)))
                horizon = max(1, int(request.form.get("horizon_days", 14)))
            except (ValueError, TypeError):
                timeout_h, cap, lead, horizon = 24, 3, 1, 14

            settings_to_save = [
                ("reservation_timeout_hours", str(timeout_h)),
                ("default_slot_capacity", str(cap)),
                ("lead_time_days", str(lead)),
                ("horizon_days", str(horizon)),
            ]
            for k, v in settings_to_save:
                conn.execute(
                    """
                    INSERT INTO delivery_settings (setting_key, setting_value)
                    VALUES (?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
                    """,
                    (k, v),
                )
            conn.commit()
            flash("Delivery availability settings updated successfully.", "success")
            conn.close()
            return redirect(url_for("main.admin_delivery_availability"))

        elif action == "toggle_slot":
            delivery_date = request.form.get("delivery_date", "").strip()
            time_slot = request.form.get("time_slot", "").strip()
            if delivery_date and time_slot:
                row = conn.execute(
                    "SELECT is_available, max_capacity FROM delivery_availability WHERE delivery_date = ? AND time_slot = ?",
                    (delivery_date, time_slot),
                ).fetchone()
                default_cap = int(get_delivery_setting(conn, "default_slot_capacity", 3))
                if row:
                    new_avail = 0 if row["is_available"] == 1 else 1
                    conn.execute(
                        "UPDATE delivery_availability SET is_available = ? WHERE delivery_date = ? AND time_slot = ?",
                        (new_avail, delivery_date, time_slot),
                    )
                else:
                    new_avail = 0
                    conn.execute(
                        """
                        INSERT INTO delivery_availability (delivery_date, time_slot, max_capacity, is_available, notes)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (delivery_date, time_slot, default_cap, new_avail, "Manually blocked"),
                    )
                conn.commit()
                status_str = "Enabled" if new_avail == 1 else "Blocked / Blackout"
                flash(f"Slot {delivery_date} {time_slot} is now {status_str}.", "success")
            conn.close()
            return redirect(url_for("main.admin_delivery_availability"))

        elif action == "update_slot":
            delivery_date = request.form.get("delivery_date", "").strip()
            time_slot = request.form.get("time_slot", "").strip()
            try:
                max_capacity = max(1, int(request.form.get("max_capacity", 3)))
            except (ValueError, TypeError):
                max_capacity = 3
            is_available = 1 if request.form.get("is_available") in ("1", "on", "true") else 0
            notes = request.form.get("notes", "").strip()

            if delivery_date and time_slot:
                conn.execute(
                    """
                    INSERT INTO delivery_availability (delivery_date, time_slot, max_capacity, is_available, notes)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(delivery_date, time_slot) DO UPDATE SET
                        max_capacity = excluded.max_capacity,
                        is_available = excluded.is_available,
                        notes = excluded.notes
                    """,
                    (delivery_date, time_slot, max_capacity, is_available, notes),
                )
                conn.commit()
                flash(f"Updated capacity & availability for {delivery_date} ({time_slot}).", "success")
            conn.close()
            return redirect(url_for("main.admin_delivery_availability"))

        elif action == "bulk_generate":
            try:
                days_ahead = max(1, int(request.form.get("days_ahead", 14)))
                default_cap = max(1, int(request.form.get("default_capacity", 3)))
            except (ValueError, TypeError):
                days_ahead, default_cap = 14, 3

            today_d = datetime.now().date()
            std_slots = ["Morning (08:00 - 12:00)", "Afternoon (13:00 - 17:00)"]
            for i in range(1, days_ahead + 1):
                day_target = today_d + timedelta(days=i)
                day_str = day_target.strftime("%Y-%m-%d")
                is_avail = 0 if day_target.weekday() == 6 else 1
                day_note = "Sunday - Closed" if day_target.weekday() == 6 else None
                for s_name in std_slots:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO delivery_availability (delivery_date, time_slot, max_capacity, is_available, notes)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (day_str, s_name, default_cap, is_avail, day_note),
                    )
            conn.commit()
            flash(f"Populated delivery schedule availability for the next {days_ahead} days.", "success")
            conn.close()
            return redirect(url_for("main.admin_delivery_availability"))

        elif action == "toggle_truck":
            try:
                truck_id = int(request.form.get("truck_id", 0))
            except (ValueError, TypeError):
                truck_id = 0
            new_status = request.form.get("status", "available").strip()
            if truck_id and new_status in ("available", "maintenance"):
                conn.execute("UPDATE trucks SET status = ? WHERE id = ?", (new_status, truck_id))
                conn.commit()
                flash(f"Truck status updated to {new_status.title()}.", "success")
            conn.close()
            return redirect(url_for("main.admin_delivery_availability"))

        elif action == "toggle_driver":
            try:
                driver_id = int(request.form.get("driver_id", 0))
                is_active = int(request.form.get("is_active", 1))
            except (ValueError, TypeError):
                driver_id = 0
                is_active = 1
            if driver_id:
                conn.execute("UPDATE users SET is_active = ? WHERE id = ? AND role = 'driver'", (is_active, driver_id))
                conn.commit()
                flash("Driver availability updated.", "success")
            conn.close()
            return redirect(url_for("main.admin_delivery_availability"))

    # GET: Prepare settings, metrics, and schedule matrix
    settings = {
        "reservation_timeout_hours": int(get_delivery_setting(conn, "reservation_timeout_hours", 24)),
        "default_slot_capacity": int(get_delivery_setting(conn, "default_slot_capacity", 3)),
        "lead_time_days": int(get_delivery_setting(conn, "lead_time_days", 1)),
        "horizon_days": int(get_delivery_setting(conn, "horizon_days", 14)),
    }

    horizon = settings["horizon_days"]
    lead_time = settings["lead_time_days"]
    default_capacity = settings["default_slot_capacity"]
    today = datetime.now().date()
    std_slots = ["Morning (08:00 - 12:00)", "Afternoon (13:00 - 17:00)"]

    avail_rows = conn.execute("SELECT * FROM delivery_availability").fetchall()
    avail_map = {(r["delivery_date"], r["time_slot"]): r for r in avail_rows}

    active_orders = conn.execute(
        """
        SELECT o.id, o.order_code, o.assigned_date, o.assigned_time_slot, o.order_status, o.total_amount,
               u.name AS customer_name,
               d.truck_id, d.driver_id, t.truck_code, t.plate_number, u_drv.name AS driver_name
        FROM orders o
        JOIN users u ON o.customer_id = u.id
        LEFT JOIN deliveries d ON o.id = d.order_id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u_drv ON d.driver_id = u_drv.id
        WHERE o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'pending_payment_verification', 'payment_verified', 'ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit')
          AND o.assigned_date IS NOT NULL
        ORDER BY o.assigned_date, o.assigned_time_slot
        """
    ).fetchall()

    bookings_map = {}
    for ord_row in active_orders:
        key = (ord_row["assigned_date"], ord_row["assigned_time_slot"])
        bookings_map.setdefault(key, []).append(ord_row)

    schedule_calendar = []
    total_slots = 0
    available_slots_count = 0
    booked_slots_count = 0
    blackout_slots_count = 0

    for i in range(0, horizon + lead_time):
        day_d = today + timedelta(days=i)
        day_str = day_d.strftime("%Y-%m-%d")
        day_name = day_d.strftime("%A")
        is_lead_day = (i < lead_time)

        for slot_name in std_slots:
            total_slots += 1
            cfg = avail_map.get((day_str, slot_name))
            if cfg:
                is_available = bool(cfg["is_available"])
                max_capacity = cfg["max_capacity"]
                notes = cfg["notes"] or ""
            else:
                is_available = False if day_d.weekday() == 6 else True
                max_capacity = default_capacity
                notes = "Sunday - Closed" if day_d.weekday() == 6 else ""

            bookings = bookings_map.get((day_str, slot_name), [])
            booked_count = len(bookings)
            remaining = max(0, max_capacity - booked_count)

            if not is_available:
                status = "unavailable"
                blackout_slots_count += 1
            elif booked_count >= max_capacity:
                status = "full"
                booked_slots_count += 1
            else:
                status = "available"
                available_slots_count += 1

            schedule_calendar.append({
                "date": day_str,
                "day_name": day_name,
                "time_slot": slot_name,
                "is_available": is_available,
                "max_capacity": max_capacity,
                "booked_count": booked_count,
                "remaining": remaining,
                "notes": notes,
                "status": status,
                "is_lead_day": is_lead_day,
                "bookings": bookings,
            })

    trucks = conn.execute("SELECT * FROM trucks ORDER BY truck_code").fetchall()
    drivers = conn.execute("SELECT * FROM users WHERE role = 'driver' ORDER BY name").fetchall()
    conn.close()

    metrics = {
        "horizon_days": horizon,
        "total_slots": total_slots,
        "available_slots": available_slots_count,
        "full_slots": booked_slots_count,
        "blackout_slots": blackout_slots_count,
        "active_trucks": sum(1 for t in trucks if t["status"] != "maintenance"),
        "total_trucks": len(trucks),
        "active_drivers": sum(1 for d in drivers if d["is_active"] == 1),
        "total_drivers": len(drivers),
    }

    return render_template(
        "admin/delivery_availability.html",
        settings=settings,
        metrics=metrics,
        calendar=schedule_calendar,
        trucks=trucks,
        drivers=drivers,
    )


@main_bp.route("/admin/customers")
@login_required("admin")
def admin_customers():
    conn = get_db()
    customers = conn.execute(
        """
        SELECT u.*, COUNT(o.id) AS total_orders, COUNT(o.id) AS order_count,
               COALESCE(SUM(o.total_amount), 0) AS total_spent
        FROM users u
        LEFT JOIN orders o ON u.id = o.customer_id
        WHERE u.role = 'customer'
        GROUP BY u.id
        ORDER BY u.id DESC
        """
    ).fetchall()
    conn.close()
    return render_template("admin/customers.html", customers=customers)


@main_bp.route("/admin/payments", methods=["GET", "POST"])
@login_required("admin")
def admin_payments():
    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action in ("confirm_payment", "verify_status"):
            payment_id = int(request.form.get("payment_id"))
            p_row = conn.execute("SELECT p.*, o.order_code FROM payments p JOIN orders o ON p.order_id = o.id WHERE p.id = ?", (payment_id,)).fetchone()
            if not p_row:
                conn.close()
                flash("Payment record not found.", "danger")
                return redirect(url_for("main.admin_payments"))

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if p_row["method"] == "cash":
                # Special COD Handling: approve arrangement without marking Paid before collection
                conn.execute(
                    """
                    UPDATE payments
                    SET payment_status = 'payment_due',
                        verified_by = ?,
                        verified_at = ?,
                        rejection_reason = NULL
                    WHERE id = ?
                    """,
                    (session["user_id"], now_str, payment_id),
                )
                conn.execute(
                    """
                    UPDATE orders
                    SET order_status = 'payment_verified',
                        rejection_reason = NULL
                    WHERE id = ?
                    """,
                    (p_row["order_id"],),
                )
                flash(f"Cash on Delivery arrangement for Order {clean_order_code(p_row['order_code'])} approved! Order is now eligible for delivery authorization.", "success")
            else:
                # Online QR: verify actual receipt of payment
                conn.execute(
                    """
                    UPDATE payments
                    SET payment_status = 'verified',
                        verified_by = ?,
                        verified_at = ?,
                        paid_at = ?,
                        rejection_reason = NULL
                    WHERE id = ?
                    """,
                    (session["user_id"], now_str, now_str, payment_id),
                )
                conn.execute(
                    """
                    UPDATE orders
                    SET order_status = 'payment_verified',
                        rejection_reason = NULL
                    WHERE id = ?
                    """,
                    (p_row["order_id"],),
                )
                flash(f"QR Payment for Order {clean_order_code(p_row['order_code'])} successfully verified and confirmed! Order is now eligible for delivery authorization.", "success")

            conn.commit()

        elif action == "reject_payment":
            payment_id = int(request.form.get("payment_id"))
            rejection_reason = request.form.get("rejection_reason", "").strip()
            if not rejection_reason:
                conn.close()
                flash("Please provide a rejection reason so the customer can correct the payment.", "warning")
                return redirect(url_for("main.admin_payments"))

            p_row = conn.execute("SELECT p.*, o.order_code FROM payments p JOIN orders o ON p.order_id = o.id WHERE p.id = ?", (payment_id,)).fetchone()
            if not p_row:
                conn.close()
                flash("Payment record not found.", "danger")
                return redirect(url_for("main.admin_payments"))

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                UPDATE payments
                SET payment_status = 'rejected',
                    rejection_reason = ?,
                    verified_by = ?,
                    verified_at = ?
                WHERE id = ?
                """,
                (rejection_reason, session["user_id"], now_str, payment_id),
            )
            conn.execute(
                """
                UPDATE orders
                SET order_status = 'payment_rejected',
                    rejection_reason = ?
                WHERE id = ?
                """,
                (rejection_reason, p_row["order_id"]),
            )
            conn.commit()
            flash(f"Payment for Order {clean_order_code(p_row['order_code'])} rejected with reason: '{rejection_reason}'. Customer notified to resubmit.", "info")

        elif action == "record_cod_collected":
            payment_id = int(request.form.get("payment_id"))
            p_row = conn.execute("SELECT p.*, o.order_code FROM payments p JOIN orders o ON p.order_id = o.id WHERE p.id = ?", (payment_id,)).fetchone()
            if p_row:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    UPDATE payments
                    SET payment_status = 'paid',
                        paid_at = ?,
                        collected_at = COALESCE(collected_at, ?),
                        collected_by = COALESCE(collected_by, ?),
                        verified_by = ?,
                        verified_at = ?
                    WHERE id = ?
                    """,
                    (now_str, now_str, session["user_id"], session["user_id"], now_str, payment_id),
                )
                conn.commit()
                flash(f"Cash on Delivery collection for Order {clean_order_code(p_row['order_code'])} officially confirmed as Paid.", "success")

        elif action == "record_payment":
            order_id = int(request.form.get("order_id"))
            method = request.form.get("method", "cash")
            ref = request.form.get("reference_number", "").strip() or request.form.get("reference_no", "").strip()
            status = request.form.get("status", "verified")
            notes = request.form.get("notes", "").strip()
            amount = float(request.form.get("amount", 0))

            existing = conn.execute("SELECT id FROM payments WHERE order_id = ?", (order_id,)).fetchone()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            collected_by = session["user_id"] if (method == "cash" and status == "paid") else None
            collected_at = now_str if (method == "cash" and status == "paid") else None
            if existing:
                conn.execute(
                    """
                    UPDATE payments
                    SET method=?, reference_number=?, payment_status=?, receipt_note=?, amount=?, verified_by=?, verified_at=?,
                        collected_by=COALESCE(collected_by, ?), collected_at=COALESCE(collected_at, ?)
                    WHERE order_id=?
                    """,
                    (method, ref, status, notes, amount, session["user_id"], now_str, collected_by, collected_at, order_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO payments (order_id, method, reference_number, payment_status, receipt_note, amount, verified_by, verified_at, collected_by, collected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, method, ref, status, notes, amount, session["user_id"], now_str, collected_by, collected_at),
                )

            if status in ("verified", "paid") or (method == "cash" and status == "payment_due"):
                conn.execute(
                    "UPDATE orders SET order_status = 'payment_verified', rejection_reason = NULL WHERE id = ?",
                    (order_id,),
                )
                ord_row = conn.execute("SELECT customer_id, order_code FROM orders WHERE id = ?", (order_id,)).fetchone()
                if ord_row:
                    create_notification(
                        conn,
                        ord_row["customer_id"],
                        "Payment Verified",
                        f"Payment for order {clean_order_code(ord_row['order_code'])} has been verified and confirmed. Ready for warehouse dispatch.",
                        "payment",
                        order_id,
                    )
            conn.commit()
            flash("Payment and receipt successfully recorded.", "success")

        conn.close()
        return redirect(url_for("main.admin_payments"))

    tab = request.args.get("tab", "awaiting")
    base_sql = """
        SELECT p.*, p.reference_number, o.order_code, o.recipient_name, o.phone AS customer_phone, o.delivery_address,
               o.total_amount AS order_total, o.order_status, o.assigned_date, o.assigned_time_slot,
               u_ver.name AS verified_by_name,
               d.id AS delivery_id, d.delivery_code, d.delivery_status,
               u_driver.name AS assigned_driver_name,
               u_col.name AS collected_by_driver_name,
               t.truck_code, t.plate_number
        FROM payments p
        JOIN orders o ON p.order_id = o.id
        LEFT JOIN deliveries d ON o.id = d.order_id
        LEFT JOIN users u_driver ON d.driver_id = u_driver.id
        LEFT JOIN users u_col ON p.collected_by = u_col.id
        LEFT JOIN users u_ver ON p.verified_by = u_ver.id
        LEFT JOIN trucks t ON d.truck_id = t.id
    """
    if tab == "awaiting":
        where = " WHERE (p.method = 'qr' AND p.payment_status IN ('pending', 'pending_verification')) OR (p.method = 'cash' AND p.payment_status != 'paid')"
    elif tab == "verified":
        where = " WHERE p.payment_status IN ('verified', 'paid')"
    elif tab == "rejected":
        where = " WHERE p.payment_status = 'rejected' OR o.order_status = 'payment_rejected'"
    else:
        where = ""

    payments = conn.execute(base_sql + where + " ORDER BY p.id DESC").fetchall()

    counts = {
        "awaiting": conn.execute(
            """
            SELECT COUNT(*) FROM payments p
            JOIN orders o ON p.order_id = o.id
            WHERE (p.method = 'qr' AND p.payment_status IN ('pending', 'pending_verification'))
               OR (p.method = 'cash' AND p.payment_status != 'paid')
            """
        ).fetchone()[0],
        "verified": conn.execute(
            """
            SELECT COUNT(*) FROM payments p
            JOIN orders o ON p.order_id = o.id
            WHERE p.payment_status IN ('verified', 'paid')
            """
        ).fetchone()[0],
        "rejected": conn.execute(
            """
            SELECT COUNT(*) FROM payments p
            JOIN orders o ON p.order_id = o.id
            WHERE p.payment_status = 'rejected' OR o.order_status = 'payment_rejected'
            """
        ).fetchone()[0],
        "all": conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
    }

    all_orders = conn.execute(
        """
        SELECT o.id, o.order_code, o.recipient_name, o.total_amount, p.payment_status
        FROM orders o
        LEFT JOIN payments p ON o.id = p.order_id
        ORDER BY o.id DESC
        """
    ).fetchall()

    conn.close()
    return render_template(
        "admin/payments.html",
        payments=payments,
        all_orders=all_orders,
        tab=tab,
        counts=counts,
    )


@main_bp.route("/admin/reports")
@login_required("admin")
def admin_reports():
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")

    conn = get_db()
    query = """
        SELECT o.*, u.name AS customer_name, p.method AS payment_method, p.payment_status
        FROM orders o
        JOIN users u ON o.customer_id = u.id
        LEFT JOIN payments p ON o.id = p.order_id
        WHERE 1=1
    """
    params = []
    if start_date:
        query += " AND date(o.created_at) >= date(?)"
        params.append(start_date)
    if end_date:
        query += " AND date(o.created_at) <= date(?)"
        params.append(end_date)
    query += " ORDER BY o.id DESC"

    orders = conn.execute(query, params).fetchall()
    total_sales = sum(o["total_amount"] for o in orders if o["order_status"] != "rejected")
    low_stock_items = conn.execute(
        "SELECT * FROM products WHERE stock_quantity <= low_stock_threshold"
    ).fetchall()

    # Sales by category for chart (filtered by date if provided)
    cat_query = """
        SELECT c.name, COALESCE(SUM(oi.subtotal), 0) AS revenue
        FROM categories c
        LEFT JOIN products p ON c.id = p.category_id
        LEFT JOIN (
            SELECT oi.product_id, oi.subtotal
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            WHERE o.order_status != 'rejected'
    """
    cat_params = []
    if start_date:
        cat_query += " AND date(o.created_at) >= date(?)"
        cat_params.append(start_date)
    if end_date:
        cat_query += " AND date(o.created_at) <= date(?)"
        cat_params.append(end_date)
    cat_query += """
        ) oi ON p.id = oi.product_id
        GROUP BY c.id
        ORDER BY revenue DESC
    """
    cat_sales = conn.execute(cat_query, cat_params).fetchall()

    # Daily orders for trend (filtered by date if provided, latest 7 days)
    daily_query = """
        SELECT date(created_at) AS order_date, COUNT(*) AS count,
               SUM(CASE WHEN order_status IN ('approved', 'in_transit', 'delivered') THEN 1 ELSE 0 END) AS approved_count
        FROM orders
        WHERE 1=1
    """
    daily_params = []
    if start_date:
        daily_query += " AND date(created_at) >= date(?)"
        daily_params.append(start_date)
    if end_date:
        daily_query += " AND date(created_at) <= date(?)"
        daily_params.append(end_date)
    daily_query += """
        GROUP BY date(created_at)
        ORDER BY order_date DESC
        LIMIT 7
    """
    daily_orders_raw = conn.execute(daily_query, daily_params).fetchall()
    daily_orders = list(reversed(daily_orders_raw))

    conn.close()

    max_cat_rev = max([float(c["revenue"] or 0) for c in cat_sales] or [1.0])
    if max_cat_rev <= 0:
        max_cat_rev = 1.0

    max_daily_count = max([int(d["count"] or 0) for d in daily_orders] or [1])
    if max_daily_count <= 0:
        max_daily_count = 1

    return render_template(
        "admin/reports.html",
        orders=orders,
        total_sales=total_sales,
        low_stock_items=low_stock_items,
        cat_sales=cat_sales,
        daily_orders=daily_orders,
        max_cat_rev=max_cat_rev,
        max_daily_count=max_daily_count,
        start_date=start_date,
        end_date=end_date,
    )


@main_bp.route("/admin/access", methods=["GET", "POST"])
@login_required("admin")
def admin_access():
    conn = get_db()
    current_admin_id = session.get("user_id")
    current_admin_name = session.get("user_name", "Admin")

    if request.method == "POST":
        action = request.form.get("action")
        target_tab = request.form.get("target_tab", "accounts")

        if action == "add_staff":
            name = request.form.get("name", "").strip()
            role = request.form.get("role", "driver")
            staff_id = request.form.get("staff_id", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            password = request.form.get("password", "rml12345")

            if role not in ("admin", "driver", "staff"):
                role = "driver"

            if not staff_id:
                prefix = "RML-ADM" if role == "admin" else "RML-DRV"
                staff_id = f"{prefix}-{random.randint(100, 999)}"

            # Ensure unique staff_id and email
            existing = conn.execute("SELECT id FROM users WHERE staff_id = ? OR LOWER(email) = ?", (staff_id, email)).fetchone()
            if existing:
                flash(f"Staff ID '{staff_id}' or Email '{email}' is already in use.", "danger")
            else:
                conn.execute(
                    "INSERT INTO users (staff_id, name, email, phone, password_hash, role, is_active, is_verified) VALUES (?, ?, ?, ?, ?, ?, 1, 1)",
                    (staff_id, name, email, phone, generate_password_hash(password), role),
                )
                conn.commit()
                new_u = conn.execute("SELECT * FROM users WHERE staff_id = ?", (staff_id,)).fetchone()
                log_access_event(
                    conn,
                    "Staff Account Created",
                    new_u,
                    current_admin_id,
                    current_admin_name,
                    f"Created {role.title()} account '{name}' ({staff_id}) with temporary password.",
                )
                conn.commit()
                flash(f"Created {role.title()} account for {name} ({staff_id}) successfully.", "success")
                target_tab = "accounts"

        elif action == "update_role":
            uid = int(request.form.get("user_id", 0))
            new_role = request.form.get("role", "driver")

            # Self-demotion protection: prevent admins from locking themselves out
            if uid == current_admin_id and new_role != "admin":
                flash("Protection active: Administrators cannot remove their own administrative privileges.", "danger")
                target_tab = "roles"
            elif new_role not in ("admin", "driver", "staff"):
                flash("Invalid role selection.", "danger")
                target_tab = "roles"
            else:
                u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                if u:
                    old_role = u["role"]
                    if old_role != new_role:
                        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, uid))
                        conn.commit()
                        log_access_event(
                            conn,
                            "Role Updated",
                            u,
                            current_admin_id,
                            current_admin_name,
                            f"Role changed from {old_role.title()} to {new_role.title()}.",
                        )
                        conn.commit()
                        flash(f"Role for {u['name']} successfully changed to {new_role.title()}.", "success")
                    else:
                        flash(f"{u['name']} is already assigned the {new_role.title()} role.", "info")
                else:
                    flash("User account not found.", "danger")
                target_tab = "roles"

        elif action == "toggle_active":
            uid = int(request.form.get("user_id", 0))
            explicit_status = request.form.get("status")  # Optional: 'active' or 'suspended'

            # Self-lockout protection: prevent administrator from suspending their own active session
            if uid == current_admin_id:
                flash("Protection active: You cannot suspend your own active administrator account.", "danger")
                target_tab = "accounts"
            else:
                u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                if u:
                    if explicit_status == "suspended":
                        new_status = 0
                    elif explicit_status == "active":
                        new_status = 1
                    else:
                        new_status = 0 if u["is_active"] == 1 else 1

                    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, uid))
                    conn.commit()

                    status_label = "Active" if new_status == 1 else "Suspended"
                    action_name = "Account Activated" if new_status == 1 else "Account Suspended"
                    log_access_event(
                        conn,
                        action_name,
                        u,
                        current_admin_id,
                        current_admin_name,
                        f"Account status set to {status_label} by administrator.",
                    )
                    conn.commit()
                    flash(f"Account for {u['name']} is now {status_label}.", "success" if new_status == 1 else "warning")
                else:
                    flash("User account not found.", "danger")
                target_tab = "accounts"

        conn.close()
        return redirect(url_for("main.admin_access", tab=target_tab))

    # GET Request: Fetch users and compute stats
    active_tab = request.args.get("tab", "accounts")
    search_q = request.args.get("search", "").strip()
    role_f = request.args.get("role_filter", "all").strip().lower()
    status_f = request.args.get("status_filter", "all").strip().lower()

    # Query enriched users with order and delivery metrics
    users_query = """
        SELECT u.*,
               (SELECT COUNT(*) FROM orders WHERE customer_id = u.id) AS order_count,
               (SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE customer_id = u.id AND order_status != 'rejected') AS total_spent,
               (SELECT COUNT(*) FROM deliveries WHERE driver_id = u.id) AS delivery_count,
               (SELECT COUNT(*) FROM deliveries WHERE driver_id = u.id AND delivery_status = 'delivered') AS completed_deliveries
        FROM users u
        ORDER BY 
            CASE WHEN u.role = 'admin' THEN 1 WHEN u.role = 'driver' THEN 2 ELSE 3 END,
            u.id ASC
    """
    all_users = conn.execute(users_query).fetchall()

    # Apply backend filters if requested
    filtered_users = []
    for u in all_users:
        # Search match
        if search_q:
            q_lower = search_q.lower()
            u_id_str = str(u["id"])
            staff_id_str = (u["staff_id"] or "").lower()
            name_str = (u["name"] or "").lower()
            email_str = (u["email"] or "").lower()
            phone_str = (u["phone"] or "").lower()
            if not (q_lower in name_str or q_lower in email_str or q_lower in phone_str or q_lower in staff_id_str or q_lower in u_id_str):
                continue

        # Role match
        if role_f and role_f != "all":
            if u["role"].lower() != role_f:
                continue

        # Status match
        if status_f and status_f != "all":
            if status_f == "active" and u["is_active"] != 1:
                continue
            if status_f == "suspended" and u["is_active"] != 0:
                continue

        filtered_users.append(u)

    # Calculate statistics across all accounts
    stats = {
        "total": len(all_users),
        "admin": sum(1 for u in all_users if u["role"] == "admin"),
        "driver": sum(1 for u in all_users if u["role"] == "driver"),
        "customer": sum(1 for u in all_users if u["role"] == "customer"),
        "active": sum(1 for u in all_users if u["is_active"] == 1),
        "suspended": sum(1 for u in all_users if u["is_active"] == 0),
    }

    staff_users = [u for u in all_users if u["role"] in ("admin", "driver", "staff")]

    # Fetch real access logs
    access_logs = conn.execute(
        """
        SELECT * FROM access_logs ORDER BY id DESC LIMIT 100
        """
    ).fetchall()

    conn.close()

    return render_template(
        "admin/access.html",
        users=filtered_users,
        all_users=all_users,
        staff_users=staff_users,
        access_logs=access_logs,
        stats=stats,
        active_tab=active_tab,
        search_query=search_q,
        role_filter=role_f,
        status_filter=status_f,
    )

