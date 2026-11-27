from flask import session
from app.utils.helpers import normalize_image_url


def get_customer_cart_details(conn, user_id):
    """
    Retrieve customer's active cart items with real-time product prices and stocks from database.
    Recalculates subtotals, VAT, delivery fee, and cart totals securely.
    """
    rows = conn.execute(
        """
        SELECT ci.id AS cart_item_id, ci.user_id, ci.product_id, ci.variation_id, ci.quantity,
               p.name AS product_name, p.unit AS product_unit, p.price AS product_base_price,
               p.stock_quantity AS product_stock, p.image_url, p.is_active,
               pv.variation_name, pv.price AS variation_price, pv.unit AS variation_unit,
               pv.stock_quantity AS variation_stock, pv.image_url AS variation_image_url, pv.is_available AS variation_available
        FROM cart_items ci
        JOIN products p ON ci.product_id = p.id
        LEFT JOIN product_variations pv ON ci.variation_id = pv.id
        WHERE ci.user_id = ? AND p.is_active = 1
        ORDER BY ci.id ASC
        """,
        (user_id,),
    ).fetchall()

    # Migration fallback: if database cart is empty, check legacy session['cart'] and migrate
    legacy_cart = session.get("cart", {})
    if not rows and legacy_cart:
        for c_key, c_qty in legacy_cart.items():
            try:
                c_qty = int(c_qty)
                if c_qty <= 0:
                    continue
                parts = str(c_key).split(":")
                p_id = int(parts[0]) if parts[0].isdigit() else 0
                v_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                if p_id > 0:
                    conn.execute(
                        "INSERT INTO cart_items (user_id, product_id, variation_id, quantity) VALUES (?, ?, ?, ?)",
                        (user_id, p_id, v_id, c_qty)
                    )
            except Exception:
                pass
        conn.commit()
        rows = conn.execute(
            """
            SELECT ci.id AS cart_item_id, ci.user_id, ci.product_id, ci.variation_id, ci.quantity,
                   p.name AS product_name, p.unit AS product_unit, p.price AS product_base_price,
                   p.stock_quantity AS product_stock, p.image_url, p.is_active,
                   pv.variation_name, pv.price AS variation_price, pv.unit AS variation_unit,
                   pv.stock_quantity AS variation_stock, pv.image_url AS variation_image_url, pv.is_available AS variation_available
            FROM cart_items ci
            JOIN products p ON ci.product_id = p.id
            LEFT JOIN product_variations pv ON ci.variation_id = pv.id
            WHERE ci.user_id = ? AND p.is_active = 1
            ORDER BY ci.id ASC
            """,
            (user_id,),
        ).fetchall()

    cart_items = []
    subtotal = 0.0
    total_qty = 0

    for r in rows:
        var_id = r["variation_id"]
        # Determine actual price and unit from DB
        if var_id and r["variation_price"] is not None:
            unit_price = float(r["variation_price"])
            unit_label = r["variation_unit"] or r["product_unit"]
            display_name = f"{r['product_name']} ({r['variation_name']})"
            available_stock = int(r["variation_stock"] or 0)
        else:
            unit_price = float(r["product_base_price"])
            unit_label = r["product_unit"]
            display_name = r["product_name"]
            available_stock = int(r["product_stock"] or 0)

        qty = int(r["quantity"])
        if available_stock > 0 and qty > available_stock:
            qty = available_stock
            conn.execute(
                "UPDATE cart_items SET quantity = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                (qty, r["cart_item_id"]),
            )
            conn.commit()

        line_total = round(qty * unit_price, 2)
        subtotal += line_total
        total_qty += qty

        cart_items.append({
            "cart_item_id": r["cart_item_id"],
            "product_id": r["product_id"],
            "variation_id": var_id,
            "product_name": r["product_name"],
            "variation_name": r["variation_name"],
            "display_name": display_name,
            "unit": unit_label,
            "price": unit_price,
            "stock": available_stock,
            "quantity": qty,
            "line_total": line_total,
            "image_url": normalize_image_url((r["variation_image_url"] if var_id and r["variation_image_url"] else None) or r["image_url"]),
            "is_in_stock": available_stock > 0,
            "product": {
                "id": r["product_id"],
                "name": r["product_name"],
                "unit": r["product_unit"],
                "price": r["product_base_price"],
                "stock_quantity": r["product_stock"],
                "image_url": r["image_url"],
            },
            "variation": {
                "id": var_id,
                "variation_name": r["variation_name"],
                "price": r["variation_price"],
                "unit": r["variation_unit"],
                "stock_quantity": r["variation_stock"],
            } if var_id else None,
            "cart_key": f"{r['product_id']}:{var_id}" if var_id else str(r['product_id']),
        })

    subtotal = round(subtotal, 2)
    delivery_fee = 1500.00 if cart_items else 0.00
    vat_amount = round(subtotal * 0.12, 2)
    total_amount = round(subtotal + delivery_fee + vat_amount, 2)

    return {
        "items": cart_items,
        "subtotal": subtotal,
        "delivery_fee": delivery_fee,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "total_items_count": total_qty,
    }

