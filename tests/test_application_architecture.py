"""Architecture, blueprint, static asset, and cross-portal route verification tests."""
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import app, create_app
from app.models import get_db, OFFICIAL_CATALOG


class TestApplicationArchitecture(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_01_factory_and_blueprints(self):
        instance = create_app()
        self.assertIn("main", instance.blueprints)
        self.assertIn("verify", instance.blueprints)
        self.assertIn("order_code", instance.jinja_env.filters)
        self.assertIn("product_img", instance.jinja_env.filters)

    def test_02_database_and_catalog_connectivity(self):
        conn = get_db()
        active_count = conn.execute("SELECT COUNT(*) FROM products WHERE is_active = 1").fetchone()[0]
        conn.close()
        self.assertEqual(active_count, len(OFFICIAL_CATALOG))

    def test_03_static_assets_and_product_images(self):
        for asset in [
            "/static/css/style.css",
            "/static/js/main.js",
            "/static/images/products/kb-denatured-alcohol.jpg",
            "/static/images/products/cement-fortune.jpg",
        ]:
            resp = self.client.get(asset)
            self.assertEqual(resp.status_code, 200, f"Failed serving static asset {asset}")
            self.assertGreater(len(resp.data), 500)
            resp.close()

    def test_04_public_and_auth_endpoints(self):
        for route in ["/", "/login", "/register", "/forgot-password", "/admin/login", "/driver/login"]:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"Public/Auth route {route} failed")

    def test_05_customer_admin_driver_portals_and_rbac(self):
        # Unauthenticated customer route redirects to login
        r_unauth = self.client.get("/customer/catalog")
        self.assertEqual(r_unauth.status_code, 302)

        conn = get_db()
        cust = conn.execute("SELECT id, name FROM users WHERE role='customer' AND is_active=1 LIMIT 1").fetchone()
        adm = conn.execute("SELECT id, name FROM users WHERE role='admin' AND is_active=1 LIMIT 1").fetchone()
        drv = conn.execute("SELECT id, name FROM users WHERE role='driver' AND is_active=1 LIMIT 1").fetchone()
        conn.close()

        # Customer Portal + Verification Wizard
        with self.client.session_transaction() as sess:
            sess["user_id"] = cust["id"]
            sess["role"] = "customer"
            sess["user_name"] = cust["name"]
        for route in [
            "/customer/dashboard",
            "/customer/catalog",
            "/customer/cart",
            "/customer/orders",
            "/customer/notifications",
            "/customer/profile",
            "/customer/verification",
            "/customer/payment-history",
            "/customer/support",
        ]:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"Customer route {route} returned {resp.status_code}")

        # Customer blocked from Admin Portal
        r_cross = self.client.get("/admin/dashboard")
        self.assertEqual(r_cross.status_code, 302)

        # Admin Portal + GPS Fleet API
        with self.client.session_transaction() as sess:
            sess["user_id"] = adm["id"]
            sess["role"] = "admin"
            sess["user_name"] = adm["name"]
        for route in [
            "/admin/dashboard",
            "/admin/orders",
            "/admin/inventory",
            "/admin/deliveries",
            "/admin/delivery-availability",
            "/admin/customers",
            "/admin/payments",
            "/admin/reports",
            "/admin/access",
            "/api/gps/fleet",
        ]:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"Admin route {route} returned {resp.status_code}")

        # Driver Portal
        with self.client.session_transaction() as sess:
            sess["user_id"] = drv["id"]
            sess["role"] = "driver"
            sess["user_name"] = drv["name"]
        for route in ["/driver/dashboard", "/driver/schedule"]:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"Driver route {route} returned {resp.status_code}")


if __name__ == "__main__":
    unittest.main()
