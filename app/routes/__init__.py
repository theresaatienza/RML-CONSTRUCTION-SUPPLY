from flask import Blueprint
from app.utils.helpers import clean_order_code, normalize_image_url

main_bp = Blueprint("main", __name__)
verify_bp = Blueprint("verify", __name__)


@main_bp.app_template_filter("order_code")
def order_code_filter(code):
    return clean_order_code(code)


@main_bp.app_template_filter("product_img")
def product_img_filter(url):
    return normalize_image_url(url)


# Import route modules to attach handlers to main_bp and verify_bp
from app.routes import auth, customer, admin, driver  # noqa: E402, F401

__all__ = ["main_bp", "verify_bp"]
