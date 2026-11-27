import os
from flask import Flask, session
from config import Config
from app.models import init_db, get_db
from app.routes import main_bp, verify_bp


def create_app(config_class=Config):
    """Application factory creating and configuring the RML Construction Supply Flask instance."""
    base_dir = config_class.BASE_DIR
    flask_app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
        static_url_path="/static",
    )
    flask_app.secret_key = config_class.SECRET_KEY
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = config_class.SESSION_COOKIE_HTTPONLY
    flask_app.config["SESSION_COOKIE_SAMESITE"] = config_class.SESSION_COOKIE_SAMESITE
    flask_app.config["GOOGLE_CLIENT_ID"] = config_class.GOOGLE_CLIENT_ID

    os.makedirs(os.path.dirname(config_class.DATABASE_PATH), exist_ok=True)
    init_db()

    flask_app.register_blueprint(main_bp)
    flask_app.register_blueprint(verify_bp)

    @flask_app.context_processor
    def inject_globals():
        """Inject current user, cart item count, unread notification count, and Google Client ID into templates."""
        cart_count = 0
        unread_notifications_count = 0
        user = None
        if "user_id" in session:
            conn = get_db()
            user = conn.execute(
                "SELECT id, staff_id, name, email, phone, address, role, created_at, avatar_url FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            if user and user["role"] == "customer":
                try:
                    c_row = conn.execute(
                        "SELECT COALESCE(SUM(quantity), 0) FROM cart_items WHERE user_id = ?",
                        (session["user_id"],),
                    ).fetchone()
                    cart_count = int(c_row[0]) if c_row and c_row[0] else 0
                except Exception:
                    cart_count = 0
                try:
                    unread_row = conn.execute(
                        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
                        (session["user_id"],),
                    ).fetchone()
                    unread_notifications_count = unread_row[0] if unread_row else 0
                except Exception:
                    unread_notifications_count = 0
            conn.close()
        return dict(
            current_user=user,
            cart_count=cart_count,
            unread_notifications_count=unread_notifications_count,
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        )

    return flask_app


app = create_app()

__all__ = ["app", "create_app"]
