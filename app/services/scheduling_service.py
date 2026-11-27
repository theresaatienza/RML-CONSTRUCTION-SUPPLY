from datetime import datetime, timedelta


def get_delivery_setting(conn, key, default=""):
    """Fetch setting value from delivery_settings table."""
    row = conn.execute("SELECT setting_value FROM delivery_settings WHERE setting_key = ?", (key,)).fetchone()
    return row["setting_value"] if row else default


def expire_stale_reservations(conn):
    """
    Checks for proposed delivery reservations past their expiration timestamp (e.g. 24 hours).
    Releases the reserved slot, truck, driver, and reverts order to pending_scheduling.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stale_orders = conn.execute(
        """
        SELECT o.id, o.order_code, o.assigned_date, o.assigned_time_slot
        FROM orders o
        WHERE o.order_status = 'awaiting_confirmation'
          AND o.reservation_expires_at IS NOT NULL
          AND o.reservation_expires_at <= ?
        """,
        (now_str,),
    ).fetchall()

    for o in stale_orders:
        conn.execute(
            """
            UPDATE orders
            SET order_status = 'pending_scheduling',
                schedule_status = 'expired',
                assigned_date = NULL,
                assigned_time_slot = NULL,
                reservation_expires_at = NULL,
                approval_notes = 'Schedule reservation expired after 24 hours without customer confirmation.'
            WHERE id = ?
            """,
            (o["id"],),
        )
        conn.execute(
            """
            UPDATE deliveries
            SET delivery_status = 'cancelled',
                driver_id = NULL,
                truck_id = NULL
            WHERE order_id = ?
            """,
            (o["id"],),
        )
    if stale_orders:
        conn.commit()
    return len(stale_orders)


def find_and_reserve_delivery_slot(conn, order_id=None):
    """
    Automatic delivery schedule proposal engine:
    1. Expires stale reservations first.
    2. Reads admin availability settings (lead time, horizon, slot capacity, timeout).
    3. Finds next available (date, time_slot) where:
       - Date & slot is configured and is_available == 1
       - Current active bookings < max_capacity
       - An active truck and driver are available (not double-booked in that slot)
    4. If found: returns dict with assigned_date, assigned_time_slot, truck_id, driver_id, expires_at.
       Else: returns None.
    """
    expire_stale_reservations(conn)

    try:
        lead_time = int(get_delivery_setting(conn, "lead_time_days", 1))
    except (ValueError, TypeError):
        lead_time = 1
    try:
        horizon = int(get_delivery_setting(conn, "horizon_days", 14))
    except (ValueError, TypeError):
        horizon = 14
    try:
        timeout_hours = int(get_delivery_setting(conn, "reservation_timeout_hours", 24))
    except (ValueError, TypeError):
        timeout_hours = 24
    try:
        default_capacity = int(get_delivery_setting(conn, "default_slot_capacity", 3))
    except (ValueError, TypeError):
        default_capacity = 3

    today = datetime.now().date()
    slots = ["Morning (08:00 - 12:00)", "Afternoon (13:00 - 17:00)"]

    active_trucks = conn.execute(
        "SELECT id, truck_code, plate_number FROM trucks WHERE status != 'maintenance' ORDER BY id"
    ).fetchall()
    active_drivers = conn.execute(
        "SELECT id, name FROM users WHERE role = 'driver' AND is_active = 1 ORDER BY id"
    ).fetchall()

    if not active_trucks or not active_drivers:
        return None

    # Search day by day starting from today + lead_time
    for i in range(lead_time, lead_time + horizon):
        day_date = today + timedelta(days=i)
        day_str = day_date.strftime("%Y-%m-%d")

        for slot in slots:
            avail = conn.execute(
                "SELECT max_capacity, is_available FROM delivery_availability WHERE delivery_date = ? AND time_slot = ?",
                (day_str, slot),
            ).fetchone()

            if avail:
                if not avail["is_available"]:
                    continue
                capacity = avail["max_capacity"]
            else:
                if day_date.weekday() == 6:  # Sunday closed by default if unconfigured
                    continue
                capacity = default_capacity

            # Count current active bookings for this date and slot
            exclude_clause = " AND o.id != ?" if order_id else ""
            params = [day_str, slot]
            if order_id:
                params.append(order_id)

            booked_count = conn.execute(
                f"""
                SELECT COUNT(*) FROM orders o
                WHERE o.assigned_date = ? AND o.assigned_time_slot = ?
                  AND o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'pending_payment_verification', 'payment_verified', 'ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit')
                  {exclude_clause}
                """,
                params,
            ).fetchone()[0]

            if booked_count >= capacity:
                continue

            deliv_params = [day_str, slot]
            if order_id:
                deliv_params.append(order_id)

            booked_trucks = {
                r[0] for r in conn.execute(
                    f"""
                    SELECT d.truck_id FROM deliveries d
                    JOIN orders o ON d.order_id = o.id
                    WHERE o.assigned_date = ? AND o.assigned_time_slot = ?
                      AND o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'pending_payment_verification', 'payment_verified', 'ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit')
                      AND d.truck_id IS NOT NULL
                      {exclude_clause}
                    """,
                    deliv_params,
                ).fetchall()
            }

            booked_drivers = {
                r[0] for r in conn.execute(
                    f"""
                    SELECT d.driver_id FROM deliveries d
                    JOIN orders o ON d.order_id = o.id
                    WHERE o.assigned_date = ? AND o.assigned_time_slot = ?
                      AND o.order_status IN ('awaiting_confirmation', 'schedule_confirmed', 'pending_payment_verification', 'payment_verified', 'ready_for_dispatch', 'scheduled', 'out_for_delivery', 'in_transit')
                      AND d.driver_id IS NOT NULL
                      {exclude_clause}
                    """,
                    deliv_params,
                ).fetchall()
            }

            available_truck = next((t for t in active_trucks if t["id"] not in booked_trucks), None)
            available_driver = next((d for d in active_drivers if d["id"] not in booked_drivers), None)

            if available_truck and available_driver:
                expires_at = (datetime.now() + timedelta(hours=timeout_hours)).strftime("%Y-%m-%d %H:%M:%S")
                return {
                    "assigned_date": day_str,
                    "assigned_time_slot": slot,
                    "truck_id": available_truck["id"],
                    "truck_code": available_truck["truck_code"],
                    "driver_id": available_driver["id"],
                    "driver_name": available_driver["name"],
                    "expires_at": expires_at,
                    "timeout_hours": timeout_hours,
                }

    return None

