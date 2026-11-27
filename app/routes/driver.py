from datetime import datetime
import math
import random
from flask import flash, jsonify, redirect, render_template, request, session, url_for

from app.models import get_db, create_notification
from app.routes import main_bp
from app.services import (
    RML_DEPOT_ADDRESS,
    RML_DEPOT_LAT,
    RML_DEPOT_LNG,
    RML_DEPOT_NAME,
    fetch_osrm_route,
    haversine_distance_km,
    is_gps_stale,
    resolve_delivery_coords,
)
from app.utils import clean_order_code, login_required


@main_bp.route("/api/gps/update", methods=["POST"])
@main_bp.route("/api/gps/update/<int:delivery_id>", methods=["POST"])
@login_required()
def api_gps_update(delivery_id=None):
    """Receive driver GPS coordinates and update active delivery location."""
    data = request.get_json(silent=True) or request.form or {}
    del_id = delivery_id or data.get("delivery_id")
    lat = data.get("lat")
    lng = data.get("lng")
    accuracy = data.get("accuracy")
    start_transit = data.get("start_transit")

    if not del_id or lat is None or lng is None:
        return jsonify({"ok": False, "error": "Missing coordinates or delivery ID"}), 400

    try:
        lat_f = float(lat)
        lng_f = float(lng)
        acc_f = float(accuracy) if accuracy is not None else 10.0
        del_id = int(del_id)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid coordinates or delivery ID format"}), 400

    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        return jsonify({"ok": False, "error": "Coordinates out of range"}), 400

    conn = get_db()
    deliv = conn.execute(
        """
        SELECT d.*, o.customer_id, o.order_code
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        WHERE d.id = ?
        """,
        (del_id,),
    ).fetchone()

    if not deliv:
        conn.close()
        return jsonify({"ok": False, "error": "Delivery not found"}), 404

    # Security: Driver can only update their assigned delivery
    current_user_id = session.get("user_id")
    user_role = session.get("role")
    if user_role != "admin" and deliv["driver_id"] != current_user_id:
        conn.close()
        return jsonify({"ok": False, "error": "Unauthorized: You are not assigned to this delivery"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_status = deliv["delivery_status"]

    # Auto-transition to en_route if start_transit requested or if authorized and starting movement
    if start_transit or (new_status in ("assigned", "authorized", "loading") and start_transit):
        new_status = "en_route"
        conn.execute("UPDATE deliveries SET delivery_status = 'en_route' WHERE id = ?", (del_id,))
        conn.execute("UPDATE orders SET order_status = 'out_for_delivery' WHERE id = ?", (deliv["order_id"],))
        create_notification(
            conn,
            deliv["customer_id"],
            "Out for Delivery",
            f"Delivery truck has departed from Lipa Yard with materials for order {clean_order_code(deliv['order_code'])}. Live GPS tracking is active.",
            "delivery",
            deliv["order_id"],
        )

    # Update delivery coordinates & tracking active flag
    conn.execute(
        """
        UPDATE deliveries
        SET current_lat = ?, current_lng = ?, last_gps_update = ?, tracking_active = 1
        WHERE id = ?
        """,
        (lat_f, lng_f, now_str, del_id),
    )

    # Insert historical breadcrumb in delivery_locations
    driver_id = deliv["driver_id"] or current_user_id or 1
    conn.execute(
        """
        INSERT INTO delivery_locations (delivery_id, driver_id, latitude, longitude, accuracy, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (del_id, driver_id, lat_f, lng_f, acc_f, now_str),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "delivery_id": del_id,
        "lat": lat_f,
        "lng": lng_f,
        "accuracy": acc_f,
        "delivery_status": new_status,
        "last_gps_update": now_str,
    })


@main_bp.route("/api/gps/stop-tracking/<int:delivery_id>", methods=["POST"])
@login_required()
def api_gps_stop_tracking(delivery_id):
    """Stop live GPS tracking session for a delivery."""
    conn = get_db()
    deliv = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    if not deliv:
        conn.close()
        return jsonify({"ok": False, "error": "Delivery not found"}), 404

    if session.get("role") != "admin" and deliv["driver_id"] != session.get("user_id"):
        conn.close()
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    conn.execute("UPDATE deliveries SET tracking_active = 0 WHERE id = ?", (delivery_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "delivery_id": delivery_id, "tracking_active": False})


@main_bp.route("/api/gps/track/<int:delivery_id>")
@login_required()
def api_gps_track(delivery_id):
    """Detailed live tracking payload for a delivery including route, metrics, and breadcrumbs."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT d.*,
               u.name AS driver_name, u.phone AS driver_phone,
               t.truck_code, t.plate_number, t.capacity AS truck_capacity,
               o.id AS order_id, o.order_code, o.order_status, o.recipient_name,
               o.phone AS recipient_phone, o.delivery_address, o.special_instructions,
               o.assigned_date, o.assigned_time_slot, o.customer_id
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        WHERE d.id = ?
        """,
        (delivery_id,),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Delivery not found"}), 404

    # Security check: Customer can only view their own order; Driver can view assigned; Admin can view all
    role = session.get("role")
    user_id = session.get("user_id")
    if role == "customer" and row["customer_id"] != user_id:
        conn.close()
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    # Resolve destination coordinates if not stored
    dest_lat = row["dest_lat"]
    dest_lng = row["dest_lng"]
    if not dest_lat or not dest_lng:
        dest_lat, dest_lng = resolve_delivery_coords(row["delivery_address"])
        conn.execute("UPDATE deliveries SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, delivery_id))
        conn.execute("UPDATE orders SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, row["order_id"]))
        conn.commit()

    cur_lat = row["current_lat"] or RML_DEPOT_LAT
    cur_lng = row["current_lng"] or RML_DEPOT_LNG

    # Fetch recent breadcrumbs from delivery_locations
    breadcrumbs_rows = conn.execute(
        """
        SELECT latitude, longitude, accuracy, recorded_at
        FROM delivery_locations
        WHERE delivery_id = ?
        ORDER BY id DESC
        LIMIT 40
        """,
        (delivery_id,),
    ).fetchall()
    conn.close()

    breadcrumbs = [
        {"lat": b["latitude"], "lng": b["longitude"], "accuracy": b["accuracy"], "recorded_at": b["recorded_at"]}
        for b in reversed(breadcrumbs_rows)
    ]

    stale = is_gps_stale(row["last_gps_update"], row["delivery_status"])

    return jsonify({
        "ok": True,
        "delivery": {
            "id": row["id"],
            "delivery_code": row["delivery_code"],
            "delivery_status": row["delivery_status"],
            "tracking_active": bool(row["tracking_active"]),
            "current_lat": cur_lat,
            "current_lng": cur_lng,
            "last_gps_update": row["last_gps_update"],
            "is_stale": stale,
            "dest_lat": dest_lat,
            "dest_lng": dest_lng,
            "depot_lat": RML_DEPOT_LAT,
            "depot_lng": RML_DEPOT_LNG,
            "depot_name": RML_DEPOT_NAME,
            "depot_address": RML_DEPOT_ADDRESS,
            "driver_name": row["driver_name"] or "Unassigned Driver",
            "driver_phone": row["driver_phone"] or "0917 555 0140",
            "truck_code": row["truck_code"] or "RML-07",
            "plate_number": row["plate_number"] or "DAE 4402",
            "order_id": row["order_id"],
            "order_code": clean_order_code(row["order_code"]),
            "order_status": row["order_status"],
            "recipient_name": row["recipient_name"],
            "recipient_phone": row["recipient_phone"] or "",
            "delivery_address": row["delivery_address"],
            "assigned_date": row["assigned_date"],
            "assigned_time_slot": row["assigned_time_slot"],
        },
        "breadcrumbs": breadcrumbs,
    })


@main_bp.route("/api/gps/location/<int:order_id>")
@login_required()
def api_gps_order_location(order_id):
    """Customer & driver polling endpoint for an order's delivery location."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT d.id, d.delivery_code, d.delivery_status, d.current_lat, d.current_lng, d.last_gps_update,
               d.dest_lat, d.dest_lng, d.tracking_active,
               u.name AS driver_name, u.phone AS driver_phone,
               t.truck_code, t.plate_number,
               o.id AS order_id, o.order_code, o.delivery_address, o.customer_id
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        WHERE d.order_id = ?
        """,
        (order_id,),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "No delivery registered for this order"}), 404

    # Security check: Customer can only view their own order
    if session.get("role") == "customer" and row["customer_id"] != session.get("user_id"):
        conn.close()
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    dest_lat = row["dest_lat"]
    dest_lng = row["dest_lng"]
    if not dest_lat or not dest_lng:
        dest_lat, dest_lng = resolve_delivery_coords(row["delivery_address"])
        conn.execute("UPDATE deliveries SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, row["id"]))
        conn.execute("UPDATE orders SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, order_id))
        conn.commit()

    conn.close()

    stale = is_gps_stale(row["last_gps_update"], row["delivery_status"])

    return jsonify({
        "ok": True,
        "delivery_id": row["id"],
        "delivery_code": row["delivery_code"],
        "delivery_status": row["delivery_status"],
        "lat": row["current_lat"] or RML_DEPOT_LAT,
        "lng": row["current_lng"] or RML_DEPOT_LNG,
        "last_gps_update": row["last_gps_update"],
        "is_stale": stale,
        "tracking_active": bool(row["tracking_active"]),
        "depot_lat": RML_DEPOT_LAT,
        "depot_lng": RML_DEPOT_LNG,
        "dest_lat": dest_lat,
        "dest_lng": dest_lng,
        "driver_name": row["driver_name"],
        "truck_code": row["truck_code"],
        "plate_number": row["plate_number"],
    })


@main_bp.route("/api/gps/delivery/<int:delivery_id>")
@login_required()
def api_gps_get(delivery_id):
    conn = get_db()
    row = conn.execute(
        """
        SELECT d.id, d.delivery_code, d.delivery_status, d.current_lat, d.current_lng, d.last_gps_update,
               d.dest_lat, d.dest_lng, d.tracking_active,
               u.name AS driver_name, t.truck_code, t.plate_number, o.order_code, o.delivery_address
        FROM deliveries d
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN orders o ON d.order_id = o.id
        WHERE d.id = ?
        """,
        (delivery_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify(dict(row))


@main_bp.route("/api/gps/fleet")
@login_required("admin")
def api_gps_fleet():
    """Admin endpoint to get live locations of all deliveries across the fleet."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT d.id, d.delivery_code, d.delivery_status, d.current_lat, d.current_lng, d.last_gps_update,
               d.dest_lat, d.dest_lng, d.tracking_active,
               u.name AS driver_name, u.phone AS driver_phone,
               t.truck_code, t.plate_number,
               o.id AS order_id, o.order_code, o.recipient_name, o.delivery_address
        FROM deliveries d
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN orders o ON d.order_id = o.id
        ORDER BY
            CASE
                WHEN d.delivery_status = 'en_route' THEN 1
                WHEN d.delivery_status = 'loading' THEN 2
                WHEN d.delivery_status = 'authorized' THEN 3
                WHEN d.delivery_status = 'assigned' THEN 4
                ELSE 5
            END,
            d.id DESC
        LIMIT 25
        """
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d["order_code"] = clean_order_code(d.get("order_code"))
        d["is_stale"] = is_gps_stale(d.get("last_gps_update"), d.get("delivery_status"))
        d["depot_lat"] = RML_DEPOT_LAT
        d["depot_lng"] = RML_DEPOT_LNG
        if not d.get("dest_lat") or not d.get("dest_lng"):
            clat, clng = resolve_delivery_coords(d.get("delivery_address"))
            d["dest_lat"] = clat
            d["dest_lng"] = clng
        result.append(d)

    return jsonify(result)


@main_bp.route("/api/gps/route-osrm", methods=["GET", "POST"])
@login_required()
def api_gps_route_osrm():
    """Calculate driving route and road geometry between two points using OSRM."""
    args = request.get_json(silent=True) or request.args or request.form or {}
    try:
        start_lat = float(args.get("start_lat", RML_DEPOT_LAT))
        start_lng = float(args.get("start_lng", RML_DEPOT_LNG))
        dest_lat = float(args.get("dest_lat", 13.9535))
        dest_lng = float(args.get("dest_lng", 121.1550))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid start or destination coordinates"}), 400

    route_data = fetch_osrm_route(start_lat, start_lng, dest_lat, dest_lng)
    return jsonify(route_data)


@main_bp.route("/api/gps/simulate/<int:delivery_id>", methods=["POST"])
@login_required()
def api_gps_simulate(delivery_id):
    """
    Simulate GPS vehicle advancement from current position towards delivery destination.
    Updates deliveries and records in delivery_locations history table.
    """
    conn = get_db()
    deliv = conn.execute(
        """
        SELECT d.*, o.delivery_address, o.order_code, o.customer_id
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        WHERE d.id = ?
        """,
        (delivery_id,),
    ).fetchone()

    if not deliv:
        conn.close()
        return jsonify({"ok": False, "error": "Delivery not found"}), 404

    # Security check: driver or admin
    if session.get("role") != "admin" and deliv["driver_id"] != session.get("user_id"):
        conn.close()
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    cur_lat = deliv["current_lat"] or RML_DEPOT_LAT
    cur_lng = deliv["current_lng"] or RML_DEPOT_LNG

    dest_lat = deliv["dest_lat"]
    dest_lng = deliv["dest_lng"]
    if not dest_lat or not dest_lng:
        dest_lat, dest_lng = resolve_delivery_coords(deliv["delivery_address"])
        conn.execute("UPDATE deliveries SET dest_lat = ?, dest_lng = ? WHERE id = ?", (dest_lat, dest_lng, delivery_id))

    # Vector movement step towards destination (approx 150m - 250m step)
    d_lat = dest_lat - cur_lat
    d_lng = dest_lng - cur_lng
    dist = math.sqrt(d_lat * d_lat + d_lng * d_lng)

    if dist < 0.0008:
        # Reached destination!
        next_lat = dest_lat
        next_lng = dest_lng
        arrived = True
    else:
        step_factor = min(0.12, 0.0018 / max(dist, 0.0001))
        # Add slight road jitter
        jitter_lat = random.uniform(-0.00015, 0.00015)
        jitter_lng = random.uniform(-0.00015, 0.00015)
        next_lat = round(cur_lat + (d_lat * step_factor) + jitter_lat, 5)
        next_lng = round(cur_lng + (d_lng * step_factor) + jitter_lng, 5)
        arrived = False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_status = deliv["delivery_status"]

    if new_status in ("assigned", "authorized", "loading"):
        new_status = "en_route"
        conn.execute("UPDATE deliveries SET delivery_status = 'en_route' WHERE id = ?", (delivery_id,))
        conn.execute("UPDATE orders SET order_status = 'out_for_delivery' WHERE id = ?", (deliv["order_id"],))
        create_notification(
            conn,
            deliv["customer_id"],
            "Out for Delivery",
            f"Delivery truck is now en route with your materials for order {clean_order_code(deliv['order_code'])}. Live GPS tracking is active.",
            "delivery",
            deliv["order_id"],
        )

    conn.execute(
        """
        UPDATE deliveries
        SET current_lat = ?, current_lng = ?, last_gps_update = ?, tracking_active = 1
        WHERE id = ?
        """,
        (next_lat, next_lng, now_str, delivery_id),
    )

    driver_id = deliv["driver_id"] or session.get("user_id") or 1
    conn.execute(
        """
        INSERT INTO delivery_locations (delivery_id, driver_id, latitude, longitude, accuracy, recorded_at)
        VALUES (?, ?, ?, ?, 8.5, ?)
        """,
        (delivery_id, driver_id, next_lat, next_lng, now_str),
    )
    conn.commit()
    conn.close()

    dist_km = haversine_distance_km(next_lat, next_lng, dest_lat, dest_lng)

    return jsonify({
        "ok": True,
        "lat": next_lat,
        "lng": next_lng,
        "delivery_status": new_status,
        "arrived": arrived,
        "distance_remaining_km": dist_km,
        "last_gps_update": now_str,
    })


@main_bp.route("/driver/schedule", endpoint="driver_schedule")
@main_bp.route("/driver/dashboard", endpoint="driver_dashboard")
@login_required("driver")
def driver_schedule():
    conn = get_db()
    uid = session["user_id"]

    query = """
        SELECT d.*, o.order_code, o.recipient_name, o.delivery_address, o.phone, o.order_status, o.total_amount,
               t.truck_code, t.plate_number,
               p.method AS payment_method, p.payment_status AS payment_status, p.amount AS payment_amount
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN payments p ON o.id = p.order_id
    """
    if session.get("role") == "admin":
        deliveries = conn.execute(query + " ORDER BY d.id DESC").fetchall()
    else:
        deliveries = conn.execute(query + " WHERE d.driver_id = ? ORDER BY d.id DESC", (uid,)).fetchall()

    delivered_count = sum(1 for d in deliveries if d["delivery_status"] == "delivered")
    total_count = len(deliveries)

    # Find the current / next active delivery
    active_delivery = next(
        (d for d in deliveries if d["delivery_status"] in ("en_route", "assigned", "authorized", "loading")),
        deliveries[0] if deliveries else None,
    )
    conn.close()

    return render_template(
        "driver/schedule.html",
        deliveries=deliveries,
        delivered_count=delivered_count,
        total_count=total_count,
        active_delivery=active_delivery,
    )


@main_bp.route("/driver/delivery/<int:delivery_id>", methods=["GET", "POST"])
@login_required("driver")
def driver_delivery_task(delivery_id):
    conn = get_db()

    delivery_check = conn.execute(
        """
        SELECT d.*, o.order_code, o.order_status, o.customer_id
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        WHERE d.id = ?
        """,
        (delivery_id,),
    ).fetchone()

    if not delivery_check:
        conn.close()
        flash("Delivery task not found.", "danger")
        return redirect(url_for("main.driver_schedule"))

    # Security check: driver can only access their assigned deliveries (unless admin)
    if session.get("role") != "admin" and delivery_check["driver_id"] != session["user_id"]:
        conn.close()
        flash("Unauthorized access to this delivery task.", "danger")
        return redirect(url_for("main.driver_schedule"))

    is_authorized = (
        delivery_check["order_status"] in ("ready_for_dispatch", "out_for_delivery", "delivered") or
        delivery_check["delivery_status"] in ("authorized", "loading", "en_route", "delivered")
    )

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_status":
            new_status = request.form.get("delivery_status", "en_route")

            # STRICT RESTRICTION: Driver CANNOT start delivery or load unless authorized by admin!
            if not is_authorized and new_status in ("loading", "en_route"):
                conn.close()
                flash("Dispatch Restricted: This delivery has NOT been authorized by the administrator. Driver cannot start delivery.", "danger")
                return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

            conn.execute("UPDATE deliveries SET delivery_status = ? WHERE id = ?", (new_status, delivery_id))
            if new_status == "en_route":
                conn.execute(
                    "UPDATE orders SET order_status = 'out_for_delivery' WHERE id = (SELECT order_id FROM deliveries WHERE id = ?)",
                    (delivery_id,),
                )
                ord_cust = conn.execute("SELECT customer_id, order_code, id FROM orders WHERE id = (SELECT order_id FROM deliveries WHERE id = ?)", (delivery_id,)).fetchone()
                if ord_cust:
                    create_notification(
                        conn,
                        ord_cust["customer_id"],
                        "Out for Delivery",
                        f"Delivery truck is now en route with your materials for order {clean_order_code(ord_cust['order_code'])}. Live GPS tracking is active.",
                        "delivery",
                        ord_cust["id"],
                    )
            conn.commit()
            flash(f"Delivery status updated to '{new_status.replace('_', ' ').title()}'.", "success")

        elif action == "confirm_delivered":
            if not is_authorized:
                conn.close()
                flash("Dispatch Restricted: Unauthorized delivery cannot be completed.", "danger")
                return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

            recipient = request.form.get("recipient_received_by", "").strip()
            role = request.form.get("recipient_role", "").strip()
            notes = request.form.get("delivery_notes", "").strip()
            qty_recv = request.form.get("quantity_received", "").strip()
            completion_time = request.form.get("completion_time", "").strip() or datetime.now().strftime("%I:%M %p")
            ack = 1 if request.form.get("recipient_acknowledged") else 1

            conn.execute(
                """
                UPDATE deliveries
                SET delivery_status='delivered',
                    recipient_received_by=?,
                    recipient_role=?,
                    delivery_notes=?,
                    quantity_received=?,
                    completion_time=?,
                    recipient_acknowledged=?,
                    delivered_at=datetime('now', 'localtime')
                WHERE id=?
                """,
                (recipient, role, notes, qty_recv, completion_time, ack, delivery_id),
            )
            conn.execute(
                "UPDATE orders SET order_status = 'delivered' WHERE id = (SELECT order_id FROM deliveries WHERE id = ?)",
                (delivery_id,),
            )
            ord_cust = conn.execute("SELECT customer_id, order_code, id FROM orders WHERE id = (SELECT order_id FROM deliveries WHERE id = ?)", (delivery_id,)).fetchone()
            if ord_cust:
                create_notification(
                    conn,
                    ord_cust["customer_id"],
                    "Materials Delivered",
                    f"Order {clean_order_code(ord_cust['order_code'])} has been successfully delivered and acknowledged on-site.",
                    "delivery",
                    ord_cust["id"],
                )

            # NOTE: Delivery status and payment status are maintained independently.
            # Completing delivery on-site does NOT automatically mark payment as paid.
            # Cash on Delivery collection is confirmed independently by the assigned driver.
            conn.commit()
            flash("Delivery confirmed and marked as Delivered!", "success")

        elif action == "confirm_cash_payment":
            # Security / RBAC check: only assigned driver or admin can confirm payment
            if session.get("role") != "admin" and delivery_check["driver_id"] != session["user_id"]:
                conn.close()
                flash("Unauthorized access: You can only confirm COD payments for deliveries assigned to your own account.", "danger")
                return redirect(url_for("main.driver_schedule"))

            if not is_authorized:
                conn.close()
                flash("Dispatch Restricted: Payment cannot be collected on an unauthorized delivery task.", "danger")
                return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

            payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (delivery_check["order_id"],)).fetchone()
            if not payment:
                conn.close()
                flash("No payment record found for this order.", "danger")
                return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

            # Strictly verify this is a cash / COD payment
            # Drivers must not have access to QR payment verification!
            if payment["method"] != "cash":
                conn.close()
                flash("Permission Denied: Delivery drivers cannot verify or approve QR code payments. Only Cash on Delivery payments can be collected.", "danger")
                return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

            if payment["payment_status"] == "paid":
                conn.close()
                flash("This Cash on Delivery payment has already been confirmed as Paid.", "info")
                return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            driver_notes = request.form.get("payment_notes", "").strip()
            collected_note = f" [Cash of ₱{payment['amount']:,.2f} collected on-site by driver {session.get('user_name', 'Driver')}]"
            if driver_notes:
                collected_note += f" Note: {driver_notes}"

            conn.execute(
                """
                UPDATE payments
                SET payment_status = 'paid',
                    paid_at = ?,
                    collected_at = ?,
                    collected_by = ?,
                    receipt_note = COALESCE(receipt_note, '') || ?
                WHERE id = ?
                """,
                (now_str, now_str, session["user_id"], collected_note, payment["id"]),
            )

            ord_cust = conn.execute("SELECT customer_id, order_code, id FROM orders WHERE id = ?", (delivery_check["order_id"],)).fetchone()
            if ord_cust:
                create_notification(
                    conn,
                    ord_cust["customer_id"],
                    "COD Payment Received",
                    f"Cash payment of ₱{payment['amount']:,.2f} for order {clean_order_code(ord_cust['order_code'])} was collected on-site by driver {session.get('user_name', 'Driver')}.",
                    "payment",
                    ord_cust["id"],
                )

            conn.commit()
            flash(f"Cash on Delivery payment of ₱{payment['amount']:,.2f} successfully confirmed and recorded as Paid!", "success")

        conn.close()
        return redirect(url_for("main.driver_delivery_task", delivery_id=delivery_id))

    delivery = conn.execute(
        """
        SELECT d.*, o.order_code, o.recipient_name, o.delivery_address, o.phone, o.phone AS recipient_phone,
               o.special_instructions, o.order_status, o.total_amount AS order_total,
               t.truck_code, t.plate_number, u.name AS driver_name,
               u_auth.name AS authorized_by_name
        FROM deliveries d
        JOIN orders o ON d.order_id = o.id
        LEFT JOIN trucks t ON d.truck_id = t.id
        LEFT JOIN users u ON d.driver_id = u.id
        LEFT JOIN users u_auth ON d.authorized_by = u_auth.id
        WHERE d.id = ?
        """,
        (delivery_id,),
    ).fetchone()

    if not delivery:
        conn.close()
        flash("Delivery task not found.", "danger")
        return redirect(url_for("main.driver_schedule"))

    # Security check: if driver, must be assigned to this delivery
    if session.get("role") != "admin" and delivery["driver_id"] != session["user_id"]:
        conn.close()
        flash("Unauthorized access to this delivery task.", "danger")
        return redirect(url_for("main.driver_schedule"))

    items = conn.execute(
        """
        SELECT oi.*, p.name, p.unit
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (delivery["order_id"],),
    ).fetchall()

    payment = conn.execute(
        """
        SELECT p.*, u_col.name AS collected_by_driver_name
        FROM payments p
        LEFT JOIN users u_col ON p.collected_by = u_col.id
        WHERE p.order_id = ?
        """,
        (delivery["order_id"],),
    ).fetchone()
    conn.close()

    return render_template(
        "driver/delivery_task.html",
        delivery=delivery,
        items=items,
        payment=payment,
        is_authorized=is_authorized,
    )

