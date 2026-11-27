import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv_file(dotenv_path=None):
    """Load key=value pairs from .env if present without overwriting existing environment variables."""
    target = dotenv_path or os.path.join(BASE_DIR, ".env")
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv_file()


class Config:
    """Centralized configuration for the RML Construction Supply Flask application."""

    BASE_DIR = BASE_DIR
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "rml-construction-supply-secret-key")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH",
        os.path.join(BASE_DIR, "database", "rml_database.db"),
    )
