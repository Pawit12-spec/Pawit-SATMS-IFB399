"""Flask application factory and bootstrap helpers for SATMS."""

import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask

from influxdb_client_3 import InfluxDBClient3
from .routes import start_escalation_worker, start_heartbeat_monitor

# Load .env from the project root (one level above the app package)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

class NoopInfluxClient:
    """Fallback client that safely drops writes when InfluxDB is unavailable."""
    def __init__(self, logger):
        self._logger = logger

    def write(self, *args, **kwargs):
        self._logger.warning("Dropping write because InfluxDB is unavailable.")

    def query(self, *args, **kwargs):
        self._logger.debug("NoopInfluxClient.query called; returning None.")
        return None

    def close(self):
        self._logger.debug("NoopInfluxClient.close called; nothing to close.")

def create_app(test_config=None):
    """Create and configure the Flask application instance.

    Args:
        test_config (dict | None): Overrides for ``app.config`` that should be
            applied after the default mapping is populated.

    Returns:
        flask.Flask: Fully configured application ready to serve requests.
    """
    app = Flask(__name__, static_folder="static", static_url_path="")

    default_upload_dir = (Path(app.root_path) / "uploads").resolve()
    
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        UPLOAD_DIR=str(Path(os.environ.get("UPLOAD_DIR", default_upload_dir)).resolve()),
        

        PG_HOST=os.environ.get("PGHOST", "127.0.0.1"),
        PG_PORT=int(os.environ.get("PGPORT", "5432")),
        PG_USER=os.environ.get("PGUSER", "postgres"),
        PG_PASSWORD=os.environ.get("PGPASSWORD", "pass"),
        PG_DATABASE=os.environ.get("PGDATABASE", "EQL SAM"),

        INFLUXDB3_HOST_URL = os.environ.get("INFLUXDB3_HOST_URL", "http://localhost:8181"),
        INFLUXDB3_AUTH_TOKEN = os.environ.get("INFLUXDB3_AUTH_TOKEN"),
        INFLUXDB3_DATABASE = os.environ.get("INFLUXDB3_DATABASE", "EQL_SATMS") 
    )

    if test_config is not None:
        app.config.update(test_config)

    
    Path(app.config["UPLOAD_DIR"]).resolve().mkdir(parents=True, exist_ok=True)

    if app.config.get("TESTING"):
        
        app.extensions["influx3"] = NoopInfluxClient(app.logger)
    else:
        try:
            app.extensions["influx3"] = InfluxDBClient3(
                host=app.config["INFLUXDB3_HOST_URL"],
                token=app.config["INFLUXDB3_AUTH_TOKEN"],
                database=app.config["INFLUXDB3_DATABASE"],
            )
        except Exception as exc:
            # Avoid crashing when the database is down during local dev.
            app.logger.warning("InfluxDB unavailable: %s; using no-op client.", exc)
            app.extensions["influx3"] = NoopInfluxClient(app.logger)

    from .routes import bp
    app.register_blueprint(bp)

    if not app.config.get("TESTING"):
        from .models import init_database
        result = init_database(
            host=app.config["PG_HOST"],
            port=app.config["PG_PORT"],
            user=app.config["PG_USER"],
            password=app.config["PG_PASSWORD"],
            dbname=app.config["PG_DATABASE"],
            owner=app.config["PG_USER"],
        )
        if result["status"] == "error":
            app.logger.warning("DB init failed: %s", result["message"])
        else:
            _ensure_default_admin(app)
            ensure_default_sites(app)

        start_escalation_worker(app)
        start_heartbeat_monitor(app)
    return app


def _ensure_default_admin(app):
    """Create a default admin user if the ``app_user`` table is empty.

    Args:
        app (flask.Flask): Application whose configuration and logger are used
            to connect to PostgreSQL and report status.
    """
    import psycopg2
    from werkzeug.security import generate_password_hash

    try:
        conn = psycopg2.connect(
            host=app.config["PG_HOST"],
            port=app.config["PG_PORT"],
            user=app.config["PG_USER"],
            password=app.config["PG_PASSWORD"],
            dbname=app.config["PG_DATABASE"],
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app_user LIMIT 1")
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO app_user (full_name, email, password_hash, role) "
                        "VALUES (%s, %s, %s, %s)",
                        ("Admin", "admin@eql.com",
                         generate_password_hash("admin"), "asset_owner"),
                    )
                    conn.commit()
                    app.logger.info("Default admin user created (admin@eql.com / admin).")
        finally:
            conn.close()
    except Exception as exc:
        app.logger.warning("Could not seed default admin: %s", exc)

DEFAULT_SITES = [
    ("Roma Street Central", "Substation Room", "Brisbane CBD", "Roma Street, Brisbane QLD 4000"),
    ("Fortitude Valley Sub", "Substation Room", "Inner North", "Wickham Street, Fortitude Valley QLD 4006"),
    ("South Brisbane Station", "Substation Room", "South Brisbane", "Grey Street, South Brisbane QLD 4101"),
    ("West End Substation", "Substation Room", "West End", "Boundary Street, West End QLD 4101"),
    ("Paddington Power Hub", "Substation Room", "Paddington", "Latrobe Terrace, Paddington QLD 4064"),
]


def ensure_default_sites(app):
    """Seed the 5 default substation sites, skipping any that already exist by name."""
    import psycopg2

    try:
        conn = psycopg2.connect(
            host=app.config["PG_HOST"],
            port=app.config["PG_PORT"],
            user=app.config["PG_USER"],
            password=app.config["PG_PASSWORD"],
            dbname=app.config["PG_DATABASE"],
        )
        try:
            inserted = 0
            with conn.cursor() as cur:
                for name, room_name, region, address in DEFAULT_SITES:
                    cur.execute("SELECT 1 FROM site WHERE name = %s", (name,))
                    if cur.fetchone() is None:
                        cur.execute(
                            "INSERT INTO site (name, room_name, region, address) "
                            "VALUES (%s, %s, %s, %s)",
                            (name, room_name, region, address),
                        )
                        inserted += 1
            conn.commit()
            if inserted:
                app.logger.info("Seeded %d default sites.", inserted)
        finally:
            conn.close()
    except Exception as exc:
        app.logger.warning("Could not seed default sites: %s", exc)
