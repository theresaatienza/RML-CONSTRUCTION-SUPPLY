# RML Construction Supply

A web-based construction materials ordering, inventory, payment verification, and live GPS delivery management system built for **RML Construction Supply** (Lipa City, Batangas).

---

## Project Overview

The system provides three role-isolated operational portals within a single Flask web application:

- **Customer Portal:** Public landing page, customer registration & login (credentials, Google OAuth 2.0, and optional 3-step ID/facial recognition verification), live product & variation catalog, shopping cart, checkout, automatic delivery slot reservation, online QR and Cash on Delivery (COD) payment workflows, live GPS order tracking, notifications, and printable invoices.
- **Administrator Console:** KPI operations dashboard, order review & manual/automatic schedule assignment, conflict detection, QR payment verification & COD approval, pre-dispatch delivery authorization, admin-controlled order cancellation with automatic stock restoration, inventory & variation management, fleet live GPS console, automatic sales/order reports, and role/access audit logging.
- **Driver Portal:** Assigned daily schedule, dispatch authorization enforcement, live mobile GPS sharing & OSRM road route preview, independent on-site COD cash collection confirmation, and site delivery acknowledgment.

---

## Technologies Used

- **Backend:** Python 3, Flask, Werkzeug Security, Google Auth (`google-auth`)
- **Database:** SQLite3 (`database/rml_database.db` active runtime store; reference MySQL GPS DDL in `database/schema_gps_tracking.sql`)
- **Frontend:** HTML5 (Jinja2 Templates), CSS3, Bootstrap 5.3, Bootstrap Icons, Vanilla JavaScript
- **Mapping & Routing:** OpenStreetMap, Leaflet.js, Browser Geolocation API, Open Source Routing Machine (OSRM)
- **Image Processing:** Pillow

---

## Project Directory Structure

```text
RML_Construction_Supply/
├── app/
│   ├── __init__.py                 # Application factory (create_app) & context processor
│   ├── models/
│   │   ├── __init__.py
│   │   ├── database.py             # Database connection, schema migrations, catalog seeding
│   │   └── catalog_data.py         # Official RML product & variation catalog definitions
│   ├── routes/
│   │   ├── __init__.py             # Blueprint definitions (main_bp, verify_bp) & Jinja filters
│   │   ├── auth.py                 # Public landing, authentication, Google OAuth, 3-step verification
│   │   ├── customer.py             # Customer catalog, cart, checkout, orders, payments, tracking
│   │   ├── admin.py                # Admin dashboard, orders, inventory, deliveries, payments, reports, access
│   │   └── driver.py               # Driver schedule, delivery confirmation, COD collection, GPS APIs
│   ├── services/
│   │   ├── __init__.py
│   │   ├── cart_service.py         # Cart calculation, stock validation, and legacy session migration
│   │   ├── scheduling_service.py   # Automatic delivery slot proposal & stale reservation expiration
│   │   └── gps_service.py          # OSRM road routing, Batangas geocoding, and signal staleness checks
│   └── utils/
│       ├── __init__.py
│       ├── security.py             # Role-based access decorators, login rate limiting, audit logging
│       └── helpers.py              # Input validators, order code formatting, image URL normalization
├── templates/                      # Jinja2 HTML templates (admin/, auth/, customer/, driver/)
├── static/                         # Static assets (css/, js/, images/, images/products/)
├── database/                       # Runtime database (gitignored) & SQL schema reference
├── tests/                          # Automated unit and integration test suites
├── scripts/                        # Maintenance utilities (schema inspection, catalog check, lock reset)
├── docs/design_mockups/            # High-fidelity UI reference mockups
├── gps_tracking/                   # Standalone PHP/MySQL GPS reference scripts
├── config.py                       # Centralized environment and Flask configuration
├── run.py                          # Application entry point
├── run_app.bat                     # Windows startup helper script
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment configuration template
├── .gitignore                      # Git ignore rules
└── README.md                       # Project documentation
```

---

## Installation Requirements

- **Python:** 3.10 or newer
- **pip:** Python package installer

Create and activate a virtual environment from the project root, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

*(On macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`)*

---

## Environment Configuration

Copy `.env.example` to `.env` in the project root and configure your environment values:

```powershell
Copy-Item .env.example .env
```

Available settings in `.env`:
- `FLASK_SECRET_KEY` — Secret key used to sign Flask session cookies (set a strong random string in production).
- `DATABASE_PATH` — Optional custom path to the SQLite database file (defaults to `database/rml_database.db`).
- `GOOGLE_CLIENT_ID` — Optional Google OAuth 2.0 Web Client ID to enable *Continue with Google* on customer sign-in.

---

## Database Setup Instructions

The application initializes and migrates its database automatically on startup via `init_db()` in `app/models/database.py`:

1. Ensure the `database/` directory is writable.
2. When `run.py` starts, missing tables, columns, delivery settings, and the official 57-product RML hardware catalog (`app/models/catalog_data.py`) are created and synchronized automatically while preserving existing orders, users, and stock records.
3. Optional maintenance scripts can be run from the project root:
   - Inspect database tables and row counts: `python scripts/inspect_schema.py`
   - Verify database catalog alignment: `python scripts/verify_catalog.py`
   - Reset temporary login rate-limit locks: `python scripts/reset_account_locks.py`

---

## Running the Flask Application

From the project root directory:

```powershell
python run.py
```

*(On Windows, you can also double-click or run `.\run_app.bat`.)*

The server starts on `http://127.0.0.1:5000`:
- **Customer Portal / Landing:** `http://127.0.0.1:5000/`
- **Admin Console Sign-In:** `http://127.0.0.1:5000/admin/login`
- **Driver Portal Sign-In:** `http://127.0.0.1:5000/driver/login`

---

## Running Automated Tests

Run `unittest` discovery across the `tests/` suite:

```powershell
python -m unittest discover -s tests -v
```

Individual workflow integration suites can also be executed directly:

```powershell
python tests/test_application_architecture.py
python tests/test_availability_workflow.py
python tests/test_admin_cancel_order.py
python tests/test_access_roles.py
python tests/test_driver_delivery.py
```
