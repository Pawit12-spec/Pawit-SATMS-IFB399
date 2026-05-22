import csv
import io
import os
import sys
import json
import uuid
from pathlib import Path
from functools import wraps
from PIL import Image
from datetime import *
from urllib3.exceptions import NewConnectionError, MaxRetryError
from collections import deque
import joblib
import pandas as pd
import numpy as np
from datetime import timezone, timedelta
from zoneinfo import ZoneInfo

import psycopg2
from werkzeug.security import check_password_hash, generate_password_hash
from flask import (Blueprint, render_template, jsonify, request, current_app,
                   Response, stream_with_context, session, redirect, url_for,
                   send_from_directory, abort)
from .models import init_database
from .influxdb import _escape_tag, parse_timestamp, to_ns, _escape_str_field
from .sse import SSEBroker, stream
import threading
import time
from .Alert_System import alert
from .thermal_model.model import analyse_image as thermal_analyse_image, thermal_analytics, localize_and_draw as thermal_localize
from .thermal_model.substation_configs import get_zone_temperatures, EQUIPMENT_THRESHOLDS, update_equipment_counters, fetch_zones_from_db
from .ml import score_reading, data_analytics

bp = Blueprint("main", __name__, template_folder="templates")
broker = SSEBroker()  # Handles Server-Sent Events

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
Brisbane_Time = timezone(timedelta(hours=10))

# Per-site rolling averages keyed by site_id
site_avg_queues = {}

def _get_site_queues(site_id):
    """Return the per-site rolling average deques, creating if needed."""
    if site_id not in site_avg_queues:
        site_avg_queues[site_id] = {
            "temp": deque(maxlen=100),
            "humidity": deque(maxlen=100),
            "co2": deque(maxlen=100),
            "aqi": deque(maxlen=100),
        }
    return site_avg_queues[site_id]

def average(avq):
    """
    Compute the running average for a deque while handling empty queues.
    
    Returns:
        float: Average of the deque contents, or 0.0 when empty.
    """
    return sum(avq) / len(avq) if avq else 0.0

# Per-site latest readings for overview status
site_latest = {}
site_cache = {}
_suppressed_alert_count = {}
site_name_cache = {}

equipment_fault_counters = {}  # device_id → {equipment_name: consecutive_breach_count}
_STATUS_PRIORITY = {"normal": 0, "warning": 1, "critical": 2}


_pending_alerts = {}
_pending_lock = threading.Lock()
ESCALATION_COOLDOWN_MINUTES = 60
_last_escalation_notify_at = {}

def _site_escalation_config(conn, site_id):
    """Fetch the escalation config + secondary-contact details for a site."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.escalation_enabled, s.escalation_timeout_minutes, "
            "       s.secondary_contact_user_id, u.full_name, u.email, u.phone_e164 "
            "FROM site s "
            "LEFT JOIN app_user u ON u.user_id = s.secondary_contact_user_id "
            "WHERE s.site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "enabled": bool(row[0]),
        "timeout_minutes": int(row[1] or 0),
        "contact_user_id": str(row[2]) if row[2] else None,
        "contact_name": row[3],
        "contact_email": row[4],
        "contact_phone": row[5],
    }


def _clear_pending_alert(site_id):
    """Remove any pending-alert tracking for site_id."""
    with _pending_lock:
        _pending_alerts.pop(site_id, None)

def _record_pending_alert(site_id, status, message):
    """Register or refresh a pending alert for site_id."""
    now = datetime.now(timezone.utc)
    with _pending_lock:
        existing = _pending_alerts.get(site_id)
        if existing is None:
            _pending_alerts[site_id] = {
                "opened_at": now,
                "status": status,
                "escalated": False,
                "escalated_at": None,
                "last_message": message,
            }
        else:
            if status == "critical":
                existing["status"] = "critical"
            existing["last_message"] = message

def get_user_allowed_site_ids():
    """Return the set of site UUIDs the current user is allowed to access."""
    if session.get("role") == "asset_owner":
        return None  # no restriction
    user_id = session.get("user_id")
    if not user_id:
        return set()
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT site_id FROM user_assets WHERE user_id = %s",
                    (user_id,),
                )
                return {str(r[0]) for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load user allowed sites: %s", e)
        return set()
    

def debug_site_display(site_id):
    if site_id in site_name_cache:
        return site_name_cache[site_id]
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM site WHERE site_id = %s", (site_id,))
                row = cur.fetchone()
                if row:
                    site_name_cache[site_id] = row[0]
                    return row[0]
        finally:
            conn.close()
    except Exception as e:
        print(f"Failed to load site name for {site_id}: {e}")
        return site_id 


def _resolve_site_id(slug):
    """Map a site slug (e.g. 'roma-street') to its DB UUID, caching results."""
    if slug in site_cache:
        return site_cache[slug]
    # Also allow passing a UUID directly
    try:
        uuid.UUID(slug)
        site_cache[slug] = slug
        return slug
    except ValueError:
        pass
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT site_id FROM site WHERE LOWER(REPLACE(REPLACE(name, ' ', '-'), '''', '')) LIKE %s",
                    (f"%{slug}%",)
                )
                row = cur.fetchone()
                if row:
                    site_cache[slug] = str(row[0])
                    return site_cache[slug]
        finally:
            conn.close()
    except Exception:
        pass
    return None
##############################################

def allowed_extension(filename: str) -> bool:
    """Return True if the filename has an allowed thermal upload extension.

    Args:
        filename (str): File name of the uploaded asset.

    Returns:
        bool: True when extension is allowed, otherwise False.
    """
    return Path(filename).suffix.lower() in ALLOWED_EXTS


def _get_pg_conn():
    """Create a new PostgreSQL connection using the Flask app configuration.

    Returns:
        psycopg2.extensions.connection: Open database connection.
    """
    return psycopg2.connect(
        host=current_app.config["PG_HOST"],
        port=current_app.config["PG_PORT"],
        user=current_app.config["PG_USER"],
        password=current_app.config["PG_PASSWORD"],
        dbname=current_app.config["PG_DATABASE"],
    )

##############################################
#               Login Helpers                #
##############################################
def login_required(f):
    """Redirect anonymous users to the login page when a view requires auth.

    Args:
        f (Callable): The Flask view to wrap.

    Returns:
        Callable: Wrapped view function enforcing authentication.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # Skip authentication when running tests
        if not current_app.config.get("TESTING") and not session.get("user_id"):
            return redirect(url_for("main.login"))

        return f(*args, **kwargs)

    return decorated

def role_required(role):
    """Ensure the current user has the specified role before entering a view.

    Args:
        role (str): Role name required to access the endpoint.

    Returns:
        Callable: Decorator that enforces the role requirement.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_app.config.get("TESTING"):
                if not session.get("user_id"):
                    return redirect(url_for("main.login"))
                if session.get("role") != role:
                    return redirect(url_for("main.index"))

            return f(*args, **kwargs)
        return wrapped
    return decorator

def authenticate_user(email, password):
    """Validate credentials against PostgreSQL and return session metadata.

    Args:
        email (str): Submitted email address.
        password (str): Plain-text password to verify.

    Returns:
        dict | None: Session payload of the authenticated user, else None.
    """
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, full_name, password_hash, role, preferences FROM app_user "
                    "WHERE email = %s AND is_active = TRUE",
                    (email,)
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row and check_password_hash(row[2], password):
            return {"user_id": str(row[0]), "full_name": row[1], "role": row[3], "preferences": row[4] or {}}
    except Exception as e:
        current_app.logger.warning("Auth DB error: %s", e)
    return None

@bp.route("/login", methods=["GET", "POST"])
def login() -> str | Response:
    """Render the login form and authenticate submitted credentials.

    Returns:
        str | Response: Login page HTML on GET/failed auth, redirect on success.
    """
    if session.get("user_id"):
        return redirect(url_for("main.index"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = authenticate_user(email, password)
        if user:
            session["user_id"] = user["user_id"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session["preferences"] = user["preferences"]
            default_page = user["preferences"].get("default_page", "index")
            return redirect(url_for(f"main.{default_page}"))
        error = "Invalid email or password"
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout() -> Response:
    """Clear the current session and send the user back to the login page.

    Returns:
        Response: Redirect to the login view.
    """

    session.clear()
    return redirect(url_for("main.login"))


@bp.post("/api/profile")
@login_required
def update_profile() -> Response:
    """Update the current user's display name.

    Returns:
        Response: JSON result indicating success or failure.
    """
    user_id = session.get("user_id")
    data = request.get_json(silent=True) or {}
    full_name = data.get("full_name", "").strip()
    if not full_name:
        return jsonify(error="Name cannot be empty"), 400
    if len(full_name) > 100:
        return jsonify(error="Name too long"), 400
    
    old_user_setting = None
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute( 
                    "SELECT full_name FROM app_user WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                old_user_setting = {"Full Name": row[0]} 
                cur.execute(
                    "UPDATE app_user SET full_name = %s WHERE user_id = %s",
                    (full_name, user_id)
                )
            conn.commit()
        finally:
            conn.close()
        session["full_name"] = full_name
        write_audit_log(
            action="profile.update",
            old_setting=old_user_setting,
            new_setting={"Full Name": full_name},
        )
        return jsonify(success=True, full_name=full_name)
    except Exception as e:
        current_app.logger.warning("Failed to update profile: %s", e)
        return jsonify(error="Failed to update profile"), 500


@bp.post("/api/preferences")
@login_required
def save_preferences() -> Response:
    """Persist a partial set of user preferences to PostgreSQL and the session."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(error="User not authenticated"), 401

    data = request.get_json(silent=True) or {}
    preferences = data.get("preferences")

    if not isinstance(preferences, dict):
        return jsonify(error="Invalid preferences"), 400

    allowed_values = {
        "theme": {"light", "dark"},
        "default_page": {
            "index", "temperature", "humidity",
            "co2", "air_quality", "thermal"
        },
        "data_delay_threshold": {60, 120, 300, 600},
        "colorblind": {"off", "on"},
    }

    int_fields = {"data_delay_threshold"}
    sanitised = {}

    for key, value in preferences.items():
        if key not in allowed_values:
            continue

        if key in int_fields:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue

        if value in allowed_values[key]:
            sanitised[key] = value

    if not sanitised:
        return jsonify(error="No valid preferences provided"), 400

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app_user
                    SET preferences = preferences || %s::jsonb
                    WHERE user_id = %s
                    """,
                    (json.dumps(sanitised), user_id),
                )
            conn.commit()
        finally:
            conn.close()

        session["preferences"] = {
            **session.get("preferences", {}),
            **sanitised,
        }
        return jsonify(success=True)

    except Exception as e:
        current_app.logger.warning("Failed to save preferences: %s", e)
        return jsonify(error="Failed to save preferences"), 500

### AUDIT LOG   ######################################################################################

def write_audit_log(action, old_setting=None, new_setting=None):
    action_user_ID = session.get("user_id")
    action_user_name = session.get("full_name")
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                "INSERT INTO audit_log (action_user_id, action_user_name, action_taken, old_setting, new_setting) "
                "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)",
                (action_user_ID, action_user_name, action,
                 json.dumps(old_setting) if old_setting is not None else None,
                 json.dumps(new_setting) if new_setting is not None else None,),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to write audit log: %s", e)

@bp.route("/audit-log")
@login_required
def audit_log() -> str:
    rows = []
    try: 
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action_id, action_user_name, action_taken, old_setting, new_setting, action_time "
                    "FROM audit_log ORDER BY action_time DESC"
                    )
                rows = []
                for r in cur.fetchall():
                    old_setting = r[3]
                    new_setting = r[4]
                    action_time = r[5].astimezone(Brisbane_Time)
                    changed_setting = set()
                    if old_setting and new_setting:
                        changed_setting = {key for key in (old_setting.keys()) & (new_setting.keys())
                                           if old_setting[key] != new_setting[key]}
                    rows.append({
                        "action_id": str(r[0]),
                        "action_user_name": r[1],
                        "action_taken": r[2],
                        "old_setting": old_setting,
                        "new_setting": new_setting,
                        "action_time": action_time,
                        "changed_setting": changed_setting,
                    })
                
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load audit log: %s", e)
    return render_template("audit_log.html", rows=rows)

### MAIN ROUTES  ######################################################################################

@bp.route("/")
@login_required
def index() -> str:
    """Render the overview page with all sites and their latest status."""
    sites = []
    allowed = get_user_allowed_site_ids()
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                if allowed is None:
                    cur.execute(
                        "SELECT site_id, name, room_name, region, address "
                        "FROM site ORDER BY name"
                    )
                else:
                    if not allowed:
                        return render_template("overview.html", sites=[])
                    cur.execute(
                        "SELECT site_id, name, room_name, region, address "
                        "FROM site WHERE site_id = ANY(%s::uuid[]) ORDER BY name",
                        (list(allowed),),
                    )
                for r in cur.fetchall():
                    sid = str(r[0])
                    latest = site_latest.get(sid, {})
                    with _pending_lock:
                        pending = _pending_alerts.get(sid)
                    is_escalated = bool(pending and pending.get("escalated"))
                    status = latest.get("status", "normal")
                    if is_escalated:
                        status = "escalated"
                    sites.append({
                        "site_id": sid,
                        "name": r[1],
                        "room_name": r[2],
                        "region": r[3],
                        "address": r[4] or "Substation Room",
                        "status": status,
                        "last_reading": latest.get("last_reading", "--"),
                        "temperature_c": latest.get("temperature_c"),
                        "humidity_pct": latest.get("humidity_pct"),
                        "co2_ppm": latest.get("co2_ppm"),
                        "aqi": latest.get("aqi"),
                        "suppressed_occurrence_count": latest.get("suppressed_occurrence_count", 0),
                        "escalated": is_escalated,
                    })
                # Fetch latest review per site
                site_id_list = [s["site_id"] for s in sites]
                reviews_map = _get_latest_reviews(conn, site_id_list)
                for s in sites:
                    rev = reviews_map.get(s["site_id"])
                    s["suppressed"] = rev is not None
                    s["last_reviewed_by"] = rev["reviewed_by"] if rev else None
                    s["last_reviewed_at"] = rev["reviewed_at"] if rev else None
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load sites for overview: %s", e)
    return render_template("overview.html", sites=sites)

@bp.route("/dashboard")
@bp.route("/dashboard/<site_id>")
@login_required
def dashboard(site_id=None) -> str:
    """Render the dashboard with sensor cards and charts, optionally for a specific site."""
    site_info = None
    if site_id:
        allowed = get_user_allowed_site_ids()
        if allowed is not None and site_id not in allowed:
            abort(403)
        try:
            conn = _get_pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT site_id, name, room_name, region, address "
                        "FROM site WHERE site_id = %s", (site_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        site_info = {
                            "site_id": str(row[0]),
                            "name": row[1],
                            "room_name": row[2],
                            "region": row[3],
                            "address": row[4],
                        }
            finally:
                conn.close()
        except Exception as e:
            current_app.logger.warning("Failed to load site info: %s", e)
    return render_template("index.html", site_info=site_info)

@bp.route("/temperature")
@bp.route("/temperature/<site_id>")
@login_required
def temperature(site_id=None) -> str:
    """Render the sensor page configured for temperature readings."""
    site_info = _load_site_info(site_id)
    return render_template("sensor.html",
                           title="Temperature",
                           page_title="TEMPERATURE",
                           sensor_optimal_label="TEMPERATURE: 27°C ± 5%",
                           units="°C",
                           chart_title="Temperature",
                           table_heading="TEMPERATURE",
                           sensor_field="temperature_c",
                           sensor_color="#ef4444",
                           site_info=site_info,
                           base_path="/temperature",
                           )
    
@bp.route("/humidity")
@bp.route("/humidity/<site_id>")
@login_required
def humidity(site_id=None) -> str:
    """Render the sensor page configured for humidity readings."""
    site_info = _load_site_info(site_id)
    return render_template("sensor.html",
                           title="Humidity",
                           page_title="HUMIDITY",
                           sensor_optimal_label="HUMIDITY: 55% ± 5%",
                           units="%",
                           chart_title="Humidity",
                           table_heading="HUMIDITY",
                           sensor_field="humidity_pct",
                           sensor_color="#a855f7",
                           site_info=site_info,
                           base_path="/humidity",
                           )


@bp.route("/co2")
@bp.route("/co2/<site_id>")
@login_required
def co2(site_id=None) -> str:
    """Render the sensor page configured for CO₂ readings."""
    site_info = _load_site_info(site_id)
    return render_template("sensor.html",
                           title="CO2",
                           page_title="CO2",
                           sensor_optimal_label="CO₂: 750 ppm ± 5%",
                           units="ppm",
                           chart_title="CO2",
                           table_heading="CO2",
                           sensor_field="co2_ppm",
                           sensor_color="#22d3ee",
                           site_info=site_info,
                           base_path="/co2",
                           )


@bp.route("/air-quality")
@bp.route("/air-quality/<site_id>")
@login_required
def air_quality(site_id=None) -> str:
    """Render the sensor page configured for air quality index readings.

    Returns:
        str: Sensor template tailored to AQI metrics.
    """
    site_info = _load_site_info(site_id)
    return render_template("sensor.html",
                           title="Air Quality",
                           page_title="AIR QUALITY",
                           sensor_optimal_label="AIR QUALITY: 750 μg/m³ ± 5%",
                           units="μg/m³",
                           chart_title="Air Quality",
                           table_heading="AIR QUALITY",
                           sensor_field="aqi",
                           sensor_color="#f59e0b",
                           site_info=site_info,
                           base_path="/air-quality",
                           )

def _load_site_info(site_id):
    """Load site metadata from PostgreSQL for optional site-scoped views."""
    if not site_id:
        return None
    allowed = get_user_allowed_site_ids()
    if allowed is not None and site_id not in allowed:
        abort(403)
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT site_id, name, room_name, region, address "
                    "FROM site WHERE site_id = %s", (site_id,)
                )
                row = cur.fetchone()
                if row:
                    return {
                        "site_id": str(row[0]),
                        "name": row[1],
                        "room_name": row[2],
                        "region": row[3],
                        "address": row[4],
                    }
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load site info: %s", e)
    return None
    
@bp.route("/thermal")
@bp.route("/thermal/<site_id>")
@login_required
def thermal(site_id=None) -> str:
    """Render the thermal camera page with the latest image metadata."""
    site_info = _load_site_info(site_id)
    latest_image = get_latest_image()
    recent_images = get_recent_images(limit = 20)
    return render_template("thermal.html",
                           site_info = site_info,
                           base_path = "/thermal",
                           latest_image = latest_image,
                           recent_images = recent_images
    )

def get_latest_image() -> dict | None:
    """Fetch the newest thermal image metadata from InfluxDB."""
    try:
        table = current_app.extensions["influx3"].query("""
            SELECT filename, time
            FROM camera_images
            ORDER BY time DESC
            LIMIT 1
        """)
        df = table.to_pandas()
        if df.empty:
            return None
        return df.iloc[0].to_dict()
    except Exception as exc:
        current_app.logger.warning("Failed to get latest image: %s", exc)
        return None


def get_recent_images(limit: int = 20) -> list:
    """Return the most recent thermal image records up to limit entries."""
    try:
        table = current_app.extensions["influx3"].query(f"""
            SELECT filename, time
            FROM camera_images
            ORDER BY time DESC
            LIMIT {limit}
        """)
        df = table.to_pandas()

        if df.empty:
            return []

        rows = []
        for _, row in df.iterrows():
            rows.append({
                "filename": row["filename"],
                "time": row["time"],
            })

        return rows

    except Exception as exc:
        current_app.logger.warning("Failed to get recent images: %s", exc)
        return []

# This is possibly unsafe but it works for now :joy:
@bp.get("/uploads/<path:filename>")
def uploaded_file(filename: Path) -> Response:
    """Serve a stored camera image from the configured upload directory."""
    return send_from_directory(current_app.config["UPLOAD_DIR"], filename)


@bp.post("/submit/camera")
def upload_image():
    """Accept a thermal image from a Pi, classify it, and fire alerts if needed.

    camera_zones starts as None so that get_zone_temperatures and localize_and_draw
    fall back to substation_zones.json when the DB is unavailable (None sentinel,
    not an empty list — see fetch_zones_from_db).
    Alert recipients are resolved from the camera's site escalation config; env-var
    fallback applies when no site contact is configured.
    equipment_fault_counters is module-level, keyed by site_id (or camera_id when the
    device has no site) — counters persist across requests so the 3-consecutive-breach
    rule works correctly across separate uploads.
    """
    if "image" not in request.files:
        return jsonify(error='Missing file field "image"'), 400

    f = request.files["image"]
    if not f.filename:
        return jsonify(error="Empty filename"), 400

    if not allowed_extension(f.filename):
        return jsonify(error="Unsupported file type"), 400

    try:
        img = Image.open(f.stream)
        img.verify()
    except Exception:
        return jsonify(error="Invalid image data"), 400

    f.stream.seek(0)

    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    ext = Path(f.filename).suffix.lower()
    image_id = str(uuid.uuid4())
    save_path = upload_dir / f"{image_id}{ext}"
    f.save(save_path)

    camera_id = request.form.get("camera_id", "unknown")
    now_dt = datetime.now(Brisbane_Time)
    now_ns = to_ns(now_dt)

    camera_zones = None
    try:
        _zconn = _get_pg_conn()
        try:
            camera_zones = fetch_zones_from_db(camera_id, _zconn)
        finally:
            _zconn.close()
    except Exception as e:
        current_app.logger.warning("Zone fetch failed, falling back to config file: %s", e)

    thermal_result   = thermal_analyse_image(str(save_path))
    thermal_analysis = thermal_analytics(thermal_result)
    if thermal_analysis["analysis"]:
        print(f"[THERMAL ANALYTICS] {thermal_analysis['analysis']}")

    # Resolve recipients for this camera
    recipients_email, recipients_phone = [], []
    cam_site_id = None
    try:
        _conn = _get_pg_conn()
        with _conn.cursor() as cur:
            cur.execute(
                "SELECT site_id FROM device WHERE serial_number = %s OR device_id::text = %s",
                (camera_id, camera_id),
            )
            row = cur.fetchone()
        cam_site_id = str(row[0]) if row else None
        if cam_site_id:
            cfg = _site_escalation_config(_conn, cam_site_id)
            if cfg and cfg["contact_email"]:
                recipients_email = [cfg["contact_email"]]
                recipients_phone = [cfg["contact_phone"]] if cfg["contact_phone"] else []
        _conn.close()
    except Exception:
        pass

    # Hotspot alert + HSV localisation
    if thermal_result["is_anomaly"]:
        try:
            _, detections = thermal_localize(str(save_path), camera_id=camera_id, zones=camera_zones)
            current_app.logger.info(
                "Hotspot localised — %d blob(s) found [%s]", len(detections), camera_id
            )
        except Exception as e:
            current_app.logger.warning("Hotspot localisation failed: %s", e)
        try:
            a = alert.HighPriorityAlert(
                f"Irregular hotspot detected — camera {camera_id}. "
                f"Please ensure equipment is behaving correctly.\nTimestamp: {now_dt.isoformat()}",
                camera_id, now_dt.isoformat(),
                recipients_email=recipients_email,
                recipients_phone=recipients_phone,
            )
            a.trigger()
        except Exception as e:
            current_app.logger.warning("Hotspot alert failed: %s", e)

    # Equipment zone temperature thresholds from raw MLX90640 data (24×32 = 768 floats)
    equipment_readings = {}
    temp_data_raw = request.form.get("temp_data")
    if temp_data_raw:
        try:
            raw = json.loads(temp_data_raw)
            if len(raw) == 768:
                zone_temps = get_zone_temperatures(camera_id, raw, zones=camera_zones)
                device_key = cam_site_id or camera_id
                counters   = equipment_fault_counters.setdefault(
                    device_key, {name: 0 for name in EQUIPMENT_THRESHOLDS}
                )
                equipment_readings = update_equipment_counters(zone_temps, counters)
                for name, reading in equipment_readings.items():
                    count = counters[name]
                    if count == 1:
                        print(f"[LOW] {name} [{device_key}]: 1st overheating reading. Temp: {reading['temp']}°C")
                    elif count == 2:
                        print(f"[MEDIUM] {name} [{device_key}]: 2nd consecutive overheating. Temp: {reading['temp']}°C")
                    elif count >= 3:
                        try:
                            a = alert.HighPriorityAlert(
                                f"{name} overheating — {count} consecutive readings above {reading['threshold']}°C.\n"
                                f"Last reading: {reading['temp']}°C. Immediate attention required!\nTimestamp: {now_dt.isoformat()}",
                                f"{name} [{camera_id}]", now_dt.isoformat(),
                                recipients_email=recipients_email,
                                recipients_phone=recipients_phone,
                            )
                            a.trigger()
                        except Exception as e:
                            current_app.logger.warning("Equipment alert failed: %s", e)
        except Exception as e:
            current_app.logger.warning("Equipment temp extraction failed: %s", e)

    # Determine overall thermal status for this site
    thermal_status = "critical" if thermal_result["is_anomaly"] else "normal"
    for r in equipment_readings.values():
        if _STATUS_PRIORITY.get(r["status"], 0) > _STATUS_PRIORITY.get(thermal_status, 0):
            thermal_status = r["status"]

    # Update site_latest — merge with existing sensor data, only escalate status never downgrade
    if cam_site_id:
        existing = site_latest.get(cam_site_id, {})
        if _STATUS_PRIORITY.get(thermal_status, 0) >= _STATUS_PRIORITY.get(existing.get("status", "normal"), 0):
            existing["status"] = thermal_status
        existing["thermal_anomaly"]    = thermal_result["is_anomaly"]
        existing["thermal_confidence"] = thermal_result["confidence"]
        existing["equipment_readings"] = equipment_readings
        site_latest[cam_site_id] = existing

    # Register pending alert so thermal anomalies feed the escalation system
    if cam_site_id and thermal_status in ("warning", "critical"):
        msg = f"{thermal_status.upper()} thermal event at site {cam_site_id} — camera {camera_id}"
        if thermal_result["is_anomaly"]:
            msg += f", hotspot detected (confidence: {thermal_result['confidence']}%)"
        _record_pending_alert(cam_site_id, thermal_status, msg)

    try:
        lp = (
            f"camera_images,camera_id={_escape_tag(camera_id)} "
            f'image_id="{_escape_str_field(image_id)}",'
            f'filename="{_escape_str_field(save_path.name)}",'
            f"size_bytes={int(save_path.stat().st_size)},"
            f'content_type="{_escape_str_field(f.mimetype or "")}" '
            f"{now_ns}"
        )
        current_app.extensions["influx3"].write(lp)
    except Exception as e:
        current_app.logger.warning("Influx write failed; skipping: %s", e)

    if equipment_readings:
        try:
            lines = [
                f"equipment_temperatures,camera_id={_escape_tag(camera_id)},equipment={_escape_tag(name)} "
                f"temp={reading['temp']},threshold={reading['threshold']} "
                f"{now_ns}"
                for name, reading in equipment_readings.items()
            ]
            current_app.extensions["influx3"].write("\n".join(lines))
        except Exception as e:
            current_app.logger.warning("Equipment readings Influx write failed: %s", e)

    broker.publish(
        {
            "id": image_id,
            "filename": save_path.name,
            "size_bytes": save_path.stat().st_size,
            "content_type": f.mimetype,
            "camera_id": camera_id,
            "timestamp": now_dt.isoformat(),
            "is_anomaly": thermal_result["is_anomaly"],
            "confidence": thermal_result["confidence"],
            "analysis": thermal_analysis["analysis"],
            "status": thermal_status,
            "equipment_readings": equipment_readings,
        },
        event="thermal_image",
        id=str(now_ns),
    )

    return jsonify(
        id=image_id,
        filename=save_path.name,
        size_bytes=save_path.stat().st_size,
        content_type=f.mimetype,
    ), 201


@bp.get("/events")
def events():
    """Stream server-sent events to listeners and clean up on disconnect.

    Returns:
        Response: Streaming response that yields SSE payloads.
    """
    q = broker.subscribe()

    def gen():
        try:
            yield from stream(q)
        finally:
            broker.unsubscribe(q)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
    }

    return Response(stream_with_context(gen()), headers=headers)


@bp.post("/submit/readings")
def consume_reading():
    """Ingest a live sensor reading, broadcast it, and store it in InfluxDB.

    Returns:
        Response: JSON acknowledgement of the ingested reading.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify(error="Invalid or missing JSON"), 400

    try:
        temperature_c = float(data["temperature_c"])
        humidity_pct = float(data["humidity_pct"])
        ts = parse_timestamp(data["timestamp"])
        co2_ppm = float(data["co2_ppm"])
        aqi = float(data["aqi"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify(error=f"Bad payload: {e}"), 400

    device_id = str(data.get("device_id", "unknown"))
    site_id = str(data.get("site_id", "unknown"))
    ns = to_ns(ts)
    
    with pi_lock:
        pi_last_seen[device_id] = time.time()

    # Resolve slug to DB UUID early so all downstream uses are consistent
    db_site_id = _resolve_site_id(site_id) or site_id

    # Per-site rolling averages (keyed by DB UUID)
    queues = _get_site_queues(db_site_id)
    queues["temp"].append(temperature_c)
    queues["humidity"].append(humidity_pct)
    queues["co2"].append(co2_ppm)
    queues["aqi"].append(aqi)

    avg_temperature_c = average(queues["temp"])
    avg_humidity_pct = average(queues["humidity"])
    avg_co2_ppm = average(queues["co2"])
    avg_aqi = average(queues["aqi"])

    scores, anomaly_run, is_anomaly = score_reading(data, db_site_id, display_name=debug_site_display(db_site_id))
    score = scores
    
    # Determine site status for overview
    status = "normal"
    if is_anomaly and not is_site_suppressed(db_site_id):
        if temperature_c > 40 or co2_ppm > 1500 or humidity_pct > 80:
            status = "critical"
        else:
            status = "warning"
    elif is_anomaly and is_site_suppressed(db_site_id):
        _suppressed_alert_count[db_site_id] = _suppressed_alert_count.get(db_site_id, 0) + 1

    if status in ("warning", "critical"):
        msg = (f"{status.upper()} reading at site {db_site_id}: "
               f"temp={temperature_c}°C humidity={humidity_pct}% "
               f"co2={co2_ppm}ppm aqi={aqi}")
        _record_pending_alert(db_site_id, status, msg)
        with _pending_lock:
            entry = _pending_alerts.get(db_site_id)
        if entry and entry.get("escalated"):
            status = "escalated"
    else:
        _clear_pending_alert(db_site_id)


    # Update latest reading cache for overview
    from datetime import datetime as dt_cls
    elapsed = datetime.now(timezone.utc) - ts.astimezone(timezone.utc) if ts.tzinfo else datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)
    mins = max(1, int(elapsed.total_seconds() / 60))
    last_str = f"{mins} MIN AGO" if mins < 60 else f"{mins // 60} HR AGO"


    analytics = data_analytics(data, db_site_id, anomaly_run)
    if analytics["analysis"]:
        print(f"[{debug_site_display(db_site_id)} ANALYTICS] {analytics['analysis']}")


    # Merge sensor data into site_latest, preserving thermal fields from upload_image.
    # Only escalate status — never let a normal sensor reading downgrade a thermal critical.
    existing = site_latest.get(db_site_id, {})
    merged_status = status if _STATUS_PRIORITY.get(status, 0) >= _STATUS_PRIORITY.get(existing.get("status", "normal"), 0) else existing.get("status", "normal")
    existing.update({
        "status": merged_status,
        "last_reading": last_str,
        "temperature_c": temperature_c,
        "humidity_pct": humidity_pct,
        "co2_ppm": co2_ppm,
        "aqi": aqi,
        "is_anomaly": is_anomaly,
        "suppressed_occurrence_count": _suppressed_alert_count.get(db_site_id, 0),
    })
    site_latest[db_site_id] = existing

    broker.publish(
        {
            "device_id": device_id,
            "site_id": db_site_id,
            "temperature_c": temperature_c,
            "humidity_pct": humidity_pct,
            "co2_ppm": co2_ppm,
            "aqi": aqi,
            "timestamp": ts.isoformat(),
            "avg_temperature_c": avg_temperature_c,
            "avg_humidity_pct": avg_humidity_pct,
            "avg_co2_ppm": avg_co2_ppm,
            "avg_aqi": avg_aqi,
            "anomaly_score": score,
            "is_anomaly": is_anomaly,
            "sensor_anomalies": anomaly_run,
            "status": status,
            "analysis": analytics["analysis"],
        },
        event="reading",
        id=str(ns),
    )

    try:
        lp = (
            f"readings,device_id={_escape_tag(device_id)},site_id={_escape_tag(db_site_id)} "
            f"temperature_c={temperature_c},humidity_pct={humidity_pct},co2_ppm={co2_ppm},aqi={aqi} "
            f"{ns}"
        )
        current_app.extensions["influx3"].write(lp)
    except Exception as e:
        current_app.logger.warning("Influx write failed; skipping: %s", e)

    return jsonify(status="ok"), 201


def check_phonenumber(raw: str):
    if not raw:
        return None, None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None, "Phone number must contain digits"
    if len(digits) < 8 or len(digits) > 15:
        return None, "Phone number must be 8-15 digits in the proper format"
    return digits, None


def _load_manage_context():
    """Load users, sites, contacts, and camera zones for the unified manage page."""
    users_list = []
    sites = []
    contacts = []
    zones = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, full_name, email, phone_e164, role, is_active "
                    "FROM app_user ORDER BY full_name"
                )
                users_list = [
                    {
                        "user_id": str(r[0]),
                        "full_name": r[1],
                        "email": r[2],
                        "phone_e164": r[3],
                        "role": r[4],
                        "is_active": r[5],
                    }
                    for r in cur.fetchall()
                ]
                cur.execute(
                    "SELECT s.site_id, s.name, s.room_name, s.region, s.address, "
                    "       s.latitude, s.longitude, "
                    "       s.escalation_enabled, s.escalation_timeout_minutes, "
                    "       s.secondary_contact_user_id, u.full_name "
                    "FROM site s "
                    "LEFT JOIN app_user u ON u.user_id = s.secondary_contact_user_id "
                    "ORDER BY s.name"
                )
                sites = [
                    {
                        "site_id": str(r[0]), "name": r[1], "room_name": r[2],
                        "region": r[3], "address": r[4],
                        "latitude": float(r[5]) if r[5] is not None else None,
                        "longitude": float(r[6]) if r[6] is not None else None,
                        "escalation_enabled": bool(r[7]),
                        "escalation_timeout_minutes": int(r[8] or 0),
                        "secondary_contact_user_id": str(r[9]) if r[9] else "",
                        "secondary_contact_name": r[10] or "",
                    }
                    for r in cur.fetchall()
                ]
                cur.execute(
                    "SELECT user_id, full_name, email FROM app_user "
                    "WHERE is_active = TRUE ORDER BY full_name"
                )
                contacts = [
                    {"user_id": str(r[0]), "full_name": r[1], "email": r[2] or ""}
                    for r in cur.fetchall()
                ]
                cur.execute(
                    "SELECT zone_id, camera_id, name, sort_order, start_y, end_y, start_x, end_x "
                    "FROM camera_zone ORDER BY camera_id, sort_order, name"
                )
                zones = [
                    {
                        "zone_id":    str(r[0]),
                        "camera_id":  r[1],
                        "name":       r[2],
                        "sort_order": r[3],
                        "start_y":    r[4],
                        "end_y":      r[5],
                        "start_x":    r[6],
                        "end_x":      r[7],
                    }
                    for r in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load manage context: %s", e)
    return users_list, sites, contacts, zones


@bp.get("/users/<user_id>/assets")
@role_required("asset_owner")
def get_user_assets(user_id) -> Response:
    """Return the list of assets assigned to a specific user as JSON.

    Args:
        user_id (str): Identifier of the user being queried.

    Returns:
        Response: JSON payload describing the user's assets.
    """
    user_asset_list = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s.name, s.site_id "
                    "FROM user_assets ua "
                    "JOIN site s ON s.site_id = ua.site_id "
                    "WHERE ua.user_id = %s",
                    (user_id,)
                )
                user_asset_list = [
                    {"asset_name": str(r[0]), "asset_id": str(r[1])}
                    for r in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load user assets: %s", e)
        return jsonify(error="Failed to load assets"), 500

    return jsonify(assets=user_asset_list)


@bp.get("/user/assets")
@role_required("asset_owner")
def get_all_assets() -> Response:
    """Return all available substation sites so they can be assigned to users.

    Returns:
        Response: JSON payload with the list of site IDs and names.
    """
    assets = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT site_id, name FROM site ORDER BY name")
                assets = [
                    {"asset_id": str(row[0]), "asset_name": row[1]}
                    for row in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load assets: %s", e)
        return jsonify(error="Failed to load assets"), 500

    return jsonify(assets=assets)


@bp.route("/users/<user_id>/assets/add", methods=["POST"])
@role_required("asset_owner")
def update_user_assets(user_id):
    """Allocate a substation site to the specified user.

    Args:
        user_id (str): Identifier of the user receiving the site.

    Returns:
        Response: JSON result indicating success or failure.
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        site_id = (payload.get("asset_id") or payload.get("site_id") or "").strip()
    else:
        site_id = (request.form.get("asset_id", "") or request.form.get("site_id", "")).strip()

    if not site_id:
        return jsonify(error="asset_id is required"), 400

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM site where site_id = %s", (site_id,))
                site_row = cur.fetchone()
                cur.execute("SELECT full_name FROM app_user where user_id = %s", (user_id,))
                user_row = cur.fetchone()
                cur.execute(
                    "INSERT INTO user_assets (site_id, user_id) VALUES (%s, %s) "
                    "ON CONFLICT (site_id, user_id) DO NOTHING",
                    (site_id, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update user assets: %s", e)
        return jsonify(error="Failed to update assets"), 500
    new_asset_setting = {"Site" : site_row[0], "User": user_row[0]}
    write_audit_log(
        action="assets.add",
        old_setting=None,
        new_setting=new_asset_setting
    )

    return jsonify(success=True)

@bp.route("/users/<user_id>/assets/remove", methods=["POST"])
@role_required("asset_owner")
def remove_user_asset(user_id):
    """Remove a substation assignment from the specified user.

    Args:
        user_id (str): Identifier of the user losing the site.

    Returns:
        Response: JSON result indicating success or failure.
    """
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        site_id = (payload.get("asset_id") or payload.get("site_id") or "").strip()
    else:
        site_id = (request.form.get("asset_id", "") or request.form.get("site_id", "")).strip()

    if not site_id:
        return jsonify(error="asset_id is required"), 400

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM site where site_id = %s", (site_id,))
                site_row = cur.fetchone()
                cur.execute("SELECT full_name FROM app_user where user_id = %s", (user_id,))
                user_row = cur.fetchone()
                cur.execute(
                    "DELETE FROM user_assets WHERE site_id = %s AND user_id = %s",
                    (site_id, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to remove user asset: %s", e)
        return jsonify(error="Failed to remove asset"), 500
    old_asset_setting = {"Site" : site_row[0], "User": user_row[0]}
    write_audit_log(
        action="assets.remove",
        old_setting=old_asset_setting,
        new_setting=None,
    )

    return jsonify(success=True)
    
@bp.route("/users/<user_id>/update", methods=["POST"])
@role_required("asset_owner")
def update_user(user_id) -> Response:
    """Update an existing user's profile, role, and email.

    Args:
        user_id (str): Identifier of the user to update.

    Returns:
        Response: Redirect back to the users page with flash parameters.
    """
    
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    phone_raw = request.form.get("phone_e164", "").strip()
    role = request.form.get("role", "user")

    if role not in ("user", "asset_owner"):
        role = "user"
    if not full_name or not email:
        return redirect(url_for("main.assets", tab="users", error="All fields are required to be filled."))

    phone_e164, phone_error = check_phonenumber(phone_raw)
    if phone_error:
        return redirect(url_for("main.assets", tab="users", error=phone_error))
    ## new var for audit log
    old_user_setting = None 
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT full_name, email, phone_e164, role FROM app_user WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    old_user_setting = {"Full Name": row[0], "Email": row[1], "Phone": row[2], "Role": row[3]}
                cur.execute(
                    "UPDATE app_user SET full_name = %s, email = %s, phone_e164 = %s, role = %s WHERE user_id = %s",
                    (full_name, email, phone_e164, role, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except psycopg2.errors.UniqueViolation as e:
        msg = str(e).lower()
        if "phone" in msg:
            return redirect(url_for("main.assets", tab="users", error="A user with that phone number already exists"))
        return redirect(url_for("main.assets", tab="users", error="A user with that email already exists"))
    except Exception as e:
        current_app.logger.warning("Failed to update user: %s", e)
        return redirect(url_for("main.assets", tab="users", error="Failed to update user"))
    new_user_setting = {"Full Name": full_name, "Email": email, "Phone": phone_e164, "Role": role}
    write_audit_log(
        action="users.update",
        old_setting=old_user_setting,
        new_setting=new_user_setting,)
    return redirect(url_for("main.assets", tab="users", success="User updated successfully"))
            

@bp.route("/users/create", methods=["POST"])
@role_required("asset_owner")
def create_user():
    """Create a new application user and persist it to PostgreSQL.

    Returns:
        Response: Redirect back to the users page with status feedback.
    """
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    phone_raw = request.form.get("phone_e164", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    site_ids = [s for s in request.form.getlist("site_ids") if s]

    if role not in ("user", "asset_owner"):
        role = "user"

    if not full_name or not email or not password:
        return redirect(url_for("main.assets", tab="users", error="All fields are required"))

    phone_e164, phone_error = check_phonenumber(phone_raw)
    if phone_error:
        return redirect(url_for("main.assets", tab="users", error=phone_error))

    assigned_site_names = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_user (full_name, email, phone_e164, password_hash, role) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
                    (full_name, email, phone_e164, generate_password_hash(password), role)
                )
                new_user_id = cur.fetchone()[0]
                if site_ids:
                    cur.execute(
                        "SELECT site_id, name FROM site WHERE site_id = ANY(%s::uuid[])",
                        (site_ids,),
                    )
                    valid_sites = cur.fetchall()
                    for site_id, site_name in valid_sites:
                        cur.execute(
                            "INSERT INTO user_assets (site_id, user_id) VALUES (%s, %s) "
                            "ON CONFLICT (site_id, user_id) DO NOTHING",
                            (site_id, new_user_id),
                        )
                        assigned_site_names.append(site_name)
            conn.commit()
        finally:
            conn.close()
    except psycopg2.errors.UniqueViolation as e:
        msg = str(e).lower()
        if "phone" in msg:
            return redirect(url_for("main.assets", tab="users", error="A user with that phone number already exists"))
        return redirect(url_for("main.assets", tab="users", error="A user with that email already exists"))
    except Exception as e:
        current_app.logger.warning("Failed to create user: %s", e)
        return redirect(url_for("main.assets", tab="users", error="Failed to create user"))
    new_created_user = {"Full Name": full_name, "Email": email, "Phone": phone_e164, "Role": role}
    if assigned_site_names:
        new_created_user["Assigned Sites"] = ", ".join(assigned_site_names)
    write_audit_log(
        action="users.create",
        old_setting=None,
        new_setting=new_created_user,)

    return redirect(url_for("main.assets", tab="users", success="User created successfully"))


@bp.route("/users/<user_id>/delete", methods=["POST"])
@role_required("asset_owner")
def delete_user(user_id):
    """Delete a user account.

    Args:
        user_id (str): Identifier of the user to delete.

    Returns:
        Response: Redirect back to the users page with status feedback.
    """
    deleted_user = None
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT full_name, email, role FROM app_user WHERE user_id = %s", (user_id,)),
                row = cur.fetchone()
                if row:
                    deleted_user = {"Full Name": row[0], "Email": row[1], "Role": row[2]}
                cur.execute("DELETE FROM app_user WHERE user_id = %s", (user_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to delete user: %s", e)
        return redirect(url_for("main.assets", tab="users", error="Failed to delete user"))
    write_audit_log(
        action="users.delete",
        old_setting=deleted_user,
        new_setting=None,)

    return redirect(url_for("main.assets", tab="users", success="User Deleted!"))


@bp.route("/users/<user_id>/role", methods=["POST"])
@role_required("asset_owner")
def update_role(user_id):
    """Update only the role for a user while validating allowed roles.

    Args:
        user_id (str): Identifier of the user to update.

    Returns:
        Response: Redirect back to the users page with status feedback.
    """
    old_role = None
    user_name = None

    # Make sure no one can just POST a different role lmao
    new_role = request.form.get("role", "user")
    if new_role not in ("user", "asset_owner"):
        new_role = "user"

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT full_name, role FROM app_user WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    old_role = row[1]
                    user_name = row[0]
                cur.execute(
                    "UPDATE app_user SET role = %s WHERE user_id = %s",
                    (new_role, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update role: %s", e)
        return redirect(url_for("main.assets", tab="users", error="Failed to update role"))
    write_audit_log(
        action="users.update_role",
        old_setting={"Full Name": user_name, "Role": old_role},
        new_setting={"Full Name": user_name, "Role": new_role},)

    return redirect(url_for("main.assets", tab="users", success="Role updated"))


def _parse_escalation_form():
    """Pull escalation policy fields."""
    enabled = request.form.get("escalation_enabled", "").lower() in ("1", "on", "true", "yes")
    try:
        timeout = int(request.form.get("escalation_timeout_minutes", "30") or 30)
    except (TypeError, ValueError):
        timeout = 30
    timeout = max(1, min(timeout, 1440))
    contact = (request.form.get("secondary_contact_user_id", "") or "").strip()
    if not contact:
        contact = None
    else:
        try:
            uuid.UUID(contact)
        except ValueError:
            contact = None
    return enabled, timeout, contact


@bp.route("/assets")
@role_required("asset_owner")
def assets():
    """Render the unified manage (Settings) page."""
    users_list, sites, contacts, zones = _load_manage_context()
    tab = request.args.get("tab", "users")
    if tab not in ("users", "sites", "zones"):
        tab = "users"
    return render_template(
        "manage.html",
        users=users_list, sites=sites, contacts=contacts, zones=zones,
        active_tab=tab,
    )


@bp.route("/assets/create", methods=["POST"])
@role_required("asset_owner")
def create_asset():
    """Create a new substation site record.

    Returns:
        Response: Redirect back to the assets page with status feedback.
    """
    name = request.form.get("name", "").strip()
    room_name = request.form.get("room_name", "").strip() or None
    region = request.form.get("region", "").strip() or None
    address = request.form.get("address", "").strip() or None
    latitude = request.form.get("latitude", "").strip() or None
    longitude = request.form.get("longitude", "").strip() or None
    esc_enabled, esc_timeout, sec_contact = _parse_escalation_form()

    if not name:
        return redirect(url_for("main.assets", error="Site name is required"))

    try:
        if latitude is not None:
            latitude = float(latitude)
        if longitude is not None:
            longitude = float(longitude)
    except ValueError:
        return redirect(url_for("main.assets", error="Latitude/longitude must be numbers"))

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO site "
                    "(name, room_name, region, address, latitude, longitude, "
                    " escalation_enabled, escalation_timeout_minutes, secondary_contact_user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (name, room_name, region, address, latitude, longitude,
                     esc_enabled, esc_timeout, sec_contact),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to create site: %s", e)
        return redirect(url_for("main.assets", error="Failed to create site"))
    write_audit_log(
        action="assets.create",
        old_setting=None,
        new_setting={
            "Name": name, "Room Name": room_name, "Region": region,
            "Address": address, "Latitude": latitude, "Longitude": longitude,
            "Escalation Enabled": esc_enabled,
            "Escalation Timeout (min)": esc_timeout,
            "Secondary Contact": sec_contact,
        },)
    return redirect(url_for("main.assets", success="Site created successfully"))


@bp.route("/assets/<site_id>/edit", methods=["POST"])
@role_required("asset_owner")
def edit_asset(site_id):
    """Update an existing substation site's metadata.

    Args:
        site_id (str): Identifier of the site being updated.

    Returns:
        Response: Redirect back to the assets page with status feedback.
    """
    name = request.form.get("name", "").strip()
    room_name = request.form.get("room_name", "").strip() or None
    region = request.form.get("region", "").strip() or None
    address = request.form.get("address", "").strip() or None
    latitude = request.form.get("latitude", "").strip() or None
    longitude = request.form.get("longitude", "").strip() or None
    esc_enabled, esc_timeout, sec_contact = _parse_escalation_form()

    if not name:
        return redirect(url_for("main.assets", error="Site name is required"))
    old_site_setting = None
    try:
        if latitude is not None:
            latitude = float(latitude)
        if longitude is not None:
            longitude = float(longitude)
    except ValueError:
        return redirect(url_for("main.assets", error="Latitude/longitude must be numbers"))

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, room_name, region, address, latitude, longitude, "
                    "       escalation_enabled, escalation_timeout_minutes, secondary_contact_user_id "
                    "FROM site WHERE site_id = %s", (site_id,)
                )
                row = cur.fetchone()
                if row:
                    old_site_setting = {
                        "Name": row[0], "Room Name": row[1], "Region": row[2],
                        "Address": row[3],
                        "Latitude": float(row[4]) if row[4] is not None else None,
                        "Longitude": float(row[5]) if row[5] is not None else None,
                        "Escalation Enabled": bool(row[6]),
                        "Escalation Timeout (min)": int(row[7] or 0),
                        "Secondary Contact": str(row[8]) if row[8] else None,
                    }
                cur.execute(
                    "UPDATE site SET name=%s, room_name=%s, region=%s, address=%s, "
                    "latitude=%s, longitude=%s, "
                    "escalation_enabled=%s, escalation_timeout_minutes=%s, "
                    "secondary_contact_user_id=%s "
                    "WHERE site_id=%s",
                    (name, room_name, region, address, latitude, longitude,
                     esc_enabled, esc_timeout, sec_contact, site_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update site: %s", e)
        return redirect(url_for("main.assets", error="Failed to update site"))
    new_site_setting = {
        "Name": name, "Room Name": room_name, "Region": region,
        "Address": address, "Latitude": latitude, "Longitude": longitude,
        "Escalation Enabled": esc_enabled,
        "Escalation Timeout (min)": esc_timeout,
        "Secondary Contact": sec_contact,
    }
    write_audit_log(
        action="assets.update",
        old_setting=old_site_setting,
        new_setting=new_site_setting,)
    
    return redirect(url_for("main.assets", success="Site updated successfully"))


@bp.route("/assets/<site_id>/delete", methods=["POST"])
@role_required("asset_owner")
def delete_asset(site_id):
    """Delete a substation site record.

    Args:
        site_id (str): Identifier of the site to delete.

    Returns:
        Response: Redirect back to the assets page with status feedback.
    """
    old_site_setting = None
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, room_name, region, address, latitude, longitude "
                    "FROM site WHERE site_id = %s", (site_id,)
                )
                row = cur.fetchone()
                if row:
                    old_site_setting = {
                        "Name": row[0], "Room Name": row[1], "Region": row[2],
                        "Address": row[3], "Latitude": float(row[4]) if row[4] is not None else None,
                        "Longitude": float(row[5]) if row[5] is not None else None,
                    }
                cur.execute("DELETE FROM site WHERE site_id = %s", (site_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to delete site: %s", e)
        return redirect(url_for("main.assets", error="Failed to delete site"))
    write_audit_log(
        action="assets.delete",
        old_setting=old_site_setting,
        new_setting=None,)

    return redirect(url_for("main.assets", success="Site deleted"))


####### Camera zone CRUD ###############################################################

def _parse_zone_form():
    """Pull and validate zone coordinate fields from request.form."""
    camera_id = request.form.get("camera_id", "").strip()
    name      = request.form.get("name", "").strip()
    try:
        sort_order = int(request.form.get("sort_order", 0))
        start_y    = int(request.form["start_y"])
        end_y      = int(request.form["end_y"])
        start_x    = int(request.form["start_x"])
        end_x      = int(request.form["end_x"])
    except (KeyError, ValueError):
        return None, "All coordinate fields are required and must be integers"
    if not camera_id or not name:
        return None, "Camera ID and zone name are required"
    if sort_order < 0:
        return None, "Sort order must be non-negative"
    if start_y < 0 or start_x < 0:
        return None, "Coordinates must be non-negative"
    if end_y <= start_y:
        return None, "End row must be greater than start row"
    if end_x <= start_x:
        return None, "End column must be greater than start column"
    return {"camera_id": camera_id, "name": name, "sort_order": sort_order,
            "start_y": start_y, "end_y": end_y,
            "start_x": start_x, "end_x": end_x}, None


@bp.route("/zones/create", methods=["POST"])
@role_required("asset_owner")
def create_zone():
    """POST /zones/create — insert a new equipment zone for a camera.

    sort_order controls which EQUIPMENT_THRESHOLDS index this zone maps to
    in update_equipment_counters. camera_id + name must be unique.
    """
    data, err = _parse_zone_form()
    if err:
        return redirect(url_for("main.assets", tab="zones", error=err))
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO camera_zone (camera_id, name, sort_order, start_y, end_y, start_x, end_x) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (data["camera_id"], data["name"], data["sort_order"],
                     data["start_y"], data["end_y"], data["start_x"], data["end_x"]),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to create zone: %s", e)
        return redirect(url_for("main.assets", tab="zones", error="Failed to create zone — check that camera ID and name are unique"))
    write_audit_log(action="zone.create", old_setting=None, new_setting=data)
    return redirect(url_for("main.assets", tab="zones", success="Zone created"))


@bp.route("/zones/<zone_id>/edit", methods=["POST"])
@role_required("asset_owner")
def edit_zone(zone_id):
    """POST /zones/<zone_id>/edit — update an existing equipment zone.

    Reads the existing row before updating so the old values can be written to
    the audit log.
    """
    data, err = _parse_zone_form()
    if err:
        return redirect(url_for("main.assets", tab="zones", error=err))
    old_zone = None
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT camera_id, name, sort_order, start_y, end_y, start_x, end_x "
                    "FROM camera_zone WHERE zone_id = %s", (zone_id,)
                )
                row = cur.fetchone()
                if row:
                    old_zone = {"camera_id": row[0], "name": row[1], "sort_order": row[2],
                                "start_y": row[3], "end_y": row[4],
                                "start_x": row[5], "end_x": row[6]}
                cur.execute(
                    "UPDATE camera_zone SET camera_id=%s, name=%s, sort_order=%s, "
                    "start_y=%s, end_y=%s, start_x=%s, end_x=%s "
                    "WHERE zone_id=%s",
                    (data["camera_id"], data["name"], data["sort_order"],
                     data["start_y"], data["end_y"], data["start_x"], data["end_x"],
                     zone_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update zone: %s", e)
        return redirect(url_for("main.assets", tab="zones", error="Failed to update zone"))
    write_audit_log(action="zone.update", old_setting=old_zone, new_setting=data)
    return redirect(url_for("main.assets", tab="zones", success="Zone updated"))


@bp.route("/zones/<zone_id>/delete", methods=["POST"])
@role_required("asset_owner")
def delete_zone(zone_id):
    """POST /zones/<zone_id>/delete — remove an equipment zone and write an audit entry."""
    old_zone = None
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT camera_id, name FROM camera_zone WHERE zone_id = %s", (zone_id,)
                )
                row = cur.fetchone()
                if row:
                    old_zone = {"camera_id": row[0], "name": row[1]}
                cur.execute("DELETE FROM camera_zone WHERE zone_id = %s", (zone_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to delete zone: %s", e)
        return redirect(url_for("main.assets", tab="zones", error="Failed to delete zone"))
    write_audit_log(action="zone.delete", old_setting=old_zone, new_setting=None)
    return redirect(url_for("main.assets", tab="zones", success="Zone deleted"))


@bp.route("/init-db")
def init_db():
    """Initialise the PostgreSQL database using app config."""
    result = init_database(
        host=current_app.config["PG_HOST"],
        port=current_app.config["PG_PORT"],
        user=current_app.config["PG_USER"],
        password=current_app.config["PG_PASSWORD"],
        dbname=current_app.config["PG_DATABASE"],
        owner=current_app.config["PG_USER"],
        partitions_ahead=6,
        partitions_back=0
    )
    return jsonify(result)

####### Heartbeat monitoring for offline device detection and alerting (failsafe system) ##############
pi_lock = threading.Lock()
device_rolling_avg = {} 
pi_alert_state = {} # device_id -> dict of alert milestones
pi_last_seen = {} # device_id -> timestamp of last heartbeat

def monitor_offline_devices(current_time):
    """The core logic, separated so Pytest can run it safely."""
    
    with pi_lock:
        seen_devices = list(pi_last_seen.items())
    for device_id, last_heard in seen_devices:
        
        # 1. Initialize the state dictionary for new devices
        if device_id not in pi_alert_state:
            pi_alert_state[device_id] = {
                "instant": False, "10m": False, "1h": False, 
                "last_daily": None, "is_offline": False
            }
            
        state = pi_alert_state[device_id]
        offline_duration = int(current_time - last_heard)
        
        # CONTINUOUS PRINT MESSAGE FOR DEBUGGING PURPOSES TO SEE THE DURATION IN REAL-TIME
        print(f"[Failsafe Check] {device_id} | Offline Duration: {offline_duration}s")
            
        if offline_duration > 20:
            state["is_offline"] = True
            should_alert = False
            
            if offline_duration >= 3600:
                time_str = f"{offline_duration // 3600} HOURS, {(offline_duration % 3600) // 60} MINUTES"
            elif offline_duration >= 60:
                time_str = f"{offline_duration // 60}:{offline_duration % 60:02d} MINUTES"
            else:
                time_str = f"{offline_duration} SECONDS"
            
            # 2. Check Milestones
            if not state["instant"]:
                should_alert = True
                state["instant"] = True
            elif offline_duration >= 600 and not state["10m"]:
                should_alert = True
                state["10m"] = True
            elif offline_duration >= 3600 and not state["1h"]:
                should_alert = True
                state["1h"] = True
            else:
                # 3. Check Daily 7 AM and 5 PM, can be configured to be any time but these are good for testing
                now_local = datetime.now(ZoneInfo("Australia/Brisbane"))
                current_date = now_local.strftime("%Y-%m-%d")
                
                if now_local.hour == 7:
                    period = f"{current_date}-7AM"
                    if state["last_daily"] != period:
                        should_alert = True
                        state["last_daily"] = period
                elif now_local.hour == 17:
                    period = f"{current_date}-5PM"
                    if state["last_daily"] != period:
                        should_alert = True
                        state["last_daily"] = period

            if should_alert:
                message = f"CRITICAL: {device_id} IS OFFLINE. TOTAL DOWNTIME: {time_str}."
                print(message)
                try:
                    offline_alert = alert.HighPriorityAlert(
                        message=message,
                        source=device_id,
                        timestamp=datetime.now(timezone.utc),
                    )
                    offline_alert.trigger()
                except Exception as e:
                    current_app.logger.warning("Failed to send offline alert: %s", e)

        else:
            # 4. Device is ONLINE (duration < 20s)
            if state["is_offline"]:
                message = f"RECOVERY: {device_id} IS BACK ONLINE! Connection restored."
                print(message)
                try:
                    recovery_alert = alert.HighPriorityAlert(
                        message=message,
                        source=device_id,
                        timestamp=datetime.now(timezone.utc),
                    )
                    recovery_alert.trigger()
                except Exception as e:
                    current_app.logger.warning("Failed to send recovery alert: %s", e)
                    
            # Reset all trackers so it is ready for the next failure
            pi_alert_state[device_id] = {
                "instant": False, "10m": False, "1h": False, 
                "last_daily": None, "is_offline": False
            }

def monitor_heartbeats(app):
    """Daemon thread loop: poll monitor_offline_devices every 10 seconds with an app context.

    Separated from the core logic so pytest can call monitor_offline_devices directly
    with a fake timestamp, without needing to manage the thread lifecycle.
    """
    while True:
        try:
            with app.app_context():
                monitor_offline_devices(time.time())
        except Exception as e:
            app.logger.warning("Heartbeat monitor error: %s", e)
        time.sleep(10)


def start_heartbeat_monitor(app):
    """Spawn the heartbeat monitor daemon. Skipped during testing."""
    if app.config.get("TESTING"):
        return
    if getattr(app, "_heartbeat_monitor_started", False):
        return
    app._heartbeat_monitor_started = True
    threading.Thread(target=monitor_heartbeats, args=(app,), daemon=True).start()

def _trigger_escalation(site_id, entry):
    """Escalate an unacknowledged alert: notify the site's secondary contact,
    persist a review row, and write an audit-log entry."""
    try:
        conn = _get_pg_conn()
    except Exception as e:
        current_app.logger.warning("Escalation: DB connect failed: %s", e)
        return

    try:
        cfg = _site_escalation_config(conn, site_id)
        if not cfg or not cfg["enabled"] or cfg["timeout_minutes"] <= 0:
            return

        # Look up the site name for nicer notification text.
        site_name = site_id
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM site WHERE site_id = %s", (site_id,))
                row = cur.fetchone()
                if row:
                    site_name = row[0]
        except Exception:
            pass

        opened_at = entry["opened_at"]
        waited_min = int((datetime.now(timezone.utc) - opened_at).total_seconds() // 60)
        message = (
            f"ESCALATION: Alert on site '{site_name}' was not acknowledged "
            f"within {cfg['timeout_minutes']} minutes (open for ~{waited_min} min). "
        )

        # 1. Notify secondary contact (email + SMS), respecting per-site cooldown.
        if cfg["contact_email"] or cfg["contact_phone"]:
            now = datetime.now(timezone.utc)
            with _pending_lock:
                last_at = _last_escalation_notify_at.get(site_id)
                cooldown_active = (
                    last_at is not None
                    and ESCALATION_COOLDOWN_MINUTES > 0
                    and (now - last_at).total_seconds() / 60.0 < ESCALATION_COOLDOWN_MINUTES
                )
            if cooldown_active:
                current_app.logger.info(
                    "Escalation for %s suppressed by cooldown (last sent %s)",
                    site_id, last_at.isoformat() if last_at else "never",
                )
            else:
                try:
                    esc_alert = alert.HighPriorityAlert(
                        message=message,
                        source=site_name,
                        timestamp=datetime.now(timezone.utc),
                    )
                    esc_alert.trigger(
                        recipient_email=cfg["contact_email"],
                        recipient_phone=cfg["contact_phone"],
                    )
                    with _pending_lock:
                        _last_escalation_notify_at[site_id] = now
                except Exception as e:
                    current_app.logger.warning("Escalation notify failed for %s: %s", site_id, e)
        else:
            current_app.logger.warning(
                "Escalation for %s skipped notify: no secondary contact configured", site_id)

        # 2. Persist an anomaly_review row that records the escalation.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO anomaly_review "
                    "(site_id, reviewed_by, user_id, status_at_review, comment, "
                    " timeout_minutes, timeout_until, severity, "
                    " escalated_at) "
                    "VALUES (%s, %s, %s, 'escalated', %s, 0, NULL, 'high', "
                    "        now())",
                    (site_id, "SYSTEM (escalation)", cfg["contact_user_id"], message),
                )
            conn.commit()
        except Exception as e:
            current_app.logger.warning("Escalation review insert failed for %s: %s", site_id, e)

        # 3. Audit-log the escalation action.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log "
                    "(action_user_id, action_user_name, action_taken, old_setting, new_setting) "
                    "VALUES (NULL, %s, %s, %s::jsonb, %s::jsonb)",
                    (
                        "SYSTEM",
                        "alert.escalated",
                        json.dumps({"status": entry.get("status"), "site_id": site_id}),
                        json.dumps({
                            "site_id": site_id,
                            "site_name": site_name,
                            "escalated_to_name": cfg["contact_name"],
                            "minutes_open": waited_min,
                            "configured_timeout_minutes": cfg["timeout_minutes"],
                        }),
                    ),
                )
            conn.commit()
        except Exception as e:
            current_app.logger.warning("Escalation audit-log failed for %s: %s", site_id, e)

        # 4. Update in-memory state so the overview reflects "escalated".
        with _pending_lock:
            cur_entry = _pending_alerts.get(site_id)
            if cur_entry is not None:
                cur_entry["escalated"] = True
                cur_entry["escalated_at"] = datetime.now(timezone.utc)
        latest = site_latest.get(site_id)
        if latest is not None:
            latest["status"] = "escalated"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _escalation_scan():
    """One pass of the escalation worker. Separated so tests can call directly."""
    now = datetime.now(timezone.utc)
    # Snapshot pending entries that aren't yet escalated.
    with _pending_lock:
        candidates = [
            (sid, dict(e)) for sid, e in _pending_alerts.items()
            if not e.get("escalated")
        ]
    if not candidates:
        return

    # Group by site to avoid one DB connect per item; fetch configs lazily.
    try:
        conn = _get_pg_conn()
    except Exception as e:
        # Can't read configs this round; try again next tick.
        try:
            current_app.logger.warning("Escalation scan: DB connect failed: %s", e)
        except Exception:
            pass
        return
    try:
        for site_id, entry in candidates:
            cfg = _site_escalation_config(conn, site_id)
            if not cfg or not cfg["enabled"] or cfg["timeout_minutes"] <= 0:
                continue
            # Don't escalate while the site is in a review-suppression window.
            if is_site_suppressed(site_id):
                continue
            age_min = (now - entry["opened_at"]).total_seconds() / 60.0
            if age_min >= cfg["timeout_minutes"]:
                _trigger_escalation(site_id, entry)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def monitor_escalations(app):
    """Background loop: periodically check for unacknowledged alerts that
    have aged past their site's escalation timeout and escalate them.

    Args:
        app (flask.Flask): Application instance used to push a request-free
            app context (needed for ``current_app.config`` in
            :func:`_get_pg_conn`).
    """
    while True:
        try:
            with app.app_context():
                _escalation_scan()
        except Exception as e:
            print(f"Escalation scan error: {e}")
        time.sleep(30)


def start_escalation_worker(app):
    """Spawn the escalation background daemon. Idempotent per process.

    Skipped automatically when ``app.config['TESTING']`` is true so unit
    tests never accidentally trigger real escalations / outbound notifications.
    """
    if app.config.get("TESTING"):
        return
    if getattr(app, "_escalation_worker_started", False):
        return
    app._escalation_worker_started = True
    threading.Thread(
        target=monitor_escalations, args=(app,), daemon=True
    ).start()

#########################################################################################
PERIOD_CFG = {
    "day":   {"filter": "time >= now() - interval '1 day'",    "bucket": "5 minutes"},
    "week":  {"filter": "time >= now() - interval '7 days'",   "bucket": "30 minutes"},
    "month": {"filter": "time >= now() - interval '30 days'",  "bucket": "3 hours"},
    "year":  {"filter": "time >= now() - interval '365 days'", "bucket": "1 day"},
}

ALLOWED_FIELDS = {"temperature_c", "humidity_pct", "co2_ppm", "aqi"}

@bp.get("/api/readings")
@login_required
def api_readings():
    """Return downsampled historical readings from InfluxDB, optionally filtered by site."""
    period = request.args.get("period", "day")
    field = request.args.get("field", "temperature_c")
    site_id = request.args.get("site_id", "")

    allowed = get_user_allowed_site_ids()
    if site_id and allowed is not None and site_id not in allowed:
        return jsonify(error="Access denied"), 403

    if period not in PERIOD_CFG:
        return jsonify(error="Invalid period"), 400
    if field not in ALLOWED_FIELDS:
        return jsonify(error="Invalid field"), 400

    cfg = PERIOD_CFG[period]
    time_filter = cfg["filter"]
    bucket = cfg["bucket"]

    site_filter = ""
    if site_id:
        site_filter = f" AND site_id = '{_escape_str_field(site_id)}'"
    elif allowed is not None:
        if not allowed:
            return jsonify(readings=[], table=[], total=0)
        escaped = ",".join(f"'{_escape_str_field(s)}'" for s in allowed)
        site_filter = f" AND site_id IN ({escaped})"

    try:
        table = current_app.extensions["influx3"].query(
            f"SELECT "
            f"  DATE_BIN(INTERVAL '{bucket}', time) AS bucket, "
            f"  AVG({field})   AS avg_val, "
            f"  MIN({field})   AS min_val, "
            f"  MAX({field})   AS max_val, "
            f"  COUNT({field}) AS sample_count "
            f"FROM readings "
            f"WHERE {time_filter}{site_filter} "
            f"GROUP BY 1 ORDER BY 1 ASC"
        )
        if table is None:
            return jsonify(readings=[], table=[], total=0)

        df = table.to_pandas()
        if df.empty:
            return jsonify(readings=[], table=[], total=0)

        chart_rows = []
        table_rows = []
        for _, row in df.iterrows():
            t = str(row["bucket"])
            avg_v = float(row["avg_val"])
            min_v = float(row["min_val"])
            max_v = float(row["max_val"])
            cnt   = int(row["sample_count"])
            chart_rows.append({"time": t, "value": avg_v})
            table_rows.append({
                "time": t, "avg": avg_v, "min": min_v,
                "max": max_v, "samples": cnt,
            })

        return jsonify(readings=chart_rows, table=table_rows, total=len(chart_rows))

    except Exception as exc:
        current_app.logger.warning("Failed to query readings: %s", exc)
        return jsonify(readings=[], table=[], total=0)


@bp.get("/api/overview")
@login_required
def api_overview():
    """Return the last N readings (all sensors) for the dashboard mini-charts.

    Query params:
        n (int): Number of recent points to return (default 20, max 100).

    Returns:
        Response: JSON with readings array and latest averages.
    """
    n = min(int(request.args.get("n", 20)), 100)
    site_id = request.args.get("site_id", "")

    allowed = get_user_allowed_site_ids()
    if site_id and allowed is not None and site_id not in allowed:
        return jsonify(error="Access denied"), 403

    try:
        if site_id:
            table = current_app.extensions["influx3"].query(
                f"SELECT time, temperature_c, humidity_pct, co2_ppm, aqi "
                f"FROM readings "
                f"WHERE time >= now() - interval '2 hours' AND site_id = '{_escape_str_field(site_id)}' "
                f"ORDER BY time DESC "
                f"LIMIT {n}"
            )
        elif allowed is not None:
            if not allowed:
                return jsonify(readings=[], averages={})
            escaped = ",".join(f"'{_escape_str_field(s)}'" for s in allowed)
            table = current_app.extensions["influx3"].query(
                f"SELECT time, temperature_c, humidity_pct, co2_ppm, aqi "
                f"FROM readings "
                f"WHERE time >= now() - interval '2 hours' AND site_id IN ({escaped}) "
                f"ORDER BY time DESC "
                f"LIMIT {n}"
            )
        else:
            table = current_app.extensions["influx3"].query(
                f"SELECT time, temperature_c, humidity_pct, co2_ppm, aqi "
                f"FROM readings "
                f"WHERE time >= now() - interval '2 hours' "
                f"ORDER BY time DESC "
                f"LIMIT {n}"
            )
        if table is None:
            return jsonify(readings=[], averages={})

        df = table.to_pandas()
        if df.empty:
            return jsonify(readings=[], averages={})

        df = df.iloc[::-1].reset_index(drop=True)

        rows = []
        for _, row in df.iterrows():
            rows.append({
                "time": str(row["time"]),
                "temperature_c": float(row["temperature_c"]),
                "humidity_pct": float(row["humidity_pct"]),
                "co2_ppm": float(row["co2_ppm"]),
                "aqi": float(row["aqi"]),
            })

        averages = {
            "avg_temperature_c": float(df["temperature_c"].mean()),
            "avg_humidity_pct": float(df["humidity_pct"].mean()),
            "avg_co2_ppm": float(df["co2_ppm"].mean()),
            "avg_aqi": float(df["aqi"].mean()),
        }

        return jsonify(readings=rows, averages=averages)

    except Exception as exc:
        current_app.logger.warning("Failed to query overview: %s", exc)
        return jsonify(readings=[], averages={})

@bp.get("/api/sites")
@login_required
def api_sites():
    """Return all sites with their latest live status for the overview page."""
    sites = []
    allowed = get_user_allowed_site_ids()
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                if allowed is None:
                    cur.execute(
                        "SELECT site_id, name, room_name, region, address "
                        "FROM site ORDER BY name"
                    )
                else:
                    if not allowed:
                        return jsonify(sites=[])
                    cur.execute(
                        "SELECT site_id, name, room_name, region, address "
                        "FROM site WHERE site_id = ANY(%s::uuid[]) ORDER BY name",
                        (list(allowed),),
                    )
                for r in cur.fetchall():
                    sid = str(r[0])
                    latest = site_latest.get(sid, {})
                    with _pending_lock:
                        pending = _pending_alerts.get(sid)
                    is_escalated = bool(pending and pending.get("escalated"))
                    status = latest.get("status", "normal")
                    if is_escalated:
                        status = "escalated"
                    sites.append({
                        "site_id": sid,
                        "name": r[1],
                        "room_name": r[2],
                        "region": r[3],
                        "address": r[4] or "Substation Room",
                        "status": status,
                        "last_reading": latest.get("last_reading", "--"),
                        "temperature_c": latest.get("temperature_c"),
                        "humidity_pct": latest.get("humidity_pct"),
                        "co2_ppm": latest.get("co2_ppm"),
                        "aqi": latest.get("aqi"),
                        "is_anomaly": latest.get("is_anomaly", False),
                        "suppressed_occurrence_count": latest.get("suppressed_occurrence_count", 0),
                        "escalated": is_escalated,
                        "escalated_at": (pending["escalated_at"].isoformat()
                                         if pending and pending.get("escalated_at") else None),
                    })
                # Fetch latest review per site (only active suppressions)
                site_id_list = [s["site_id"] for s in sites]
                reviews_map = _get_latest_reviews(conn, site_id_list)
                for s in sites:
                    rev = reviews_map.get(s["site_id"])
                    s["suppressed"] = rev is not None
                    s["last_reviewed_by"] = rev["reviewed_by"] if rev else None
                    s["last_reviewed_at"] = rev["reviewed_at"] if rev else None
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load sites: %s", e)
    return jsonify(sites=sites)

_alert_suppression = {}


def is_site_suppressed(site_id):
    """Return True if the site is currently within a review-suppression window."""
    until = _alert_suppression.get(site_id)
    if until and datetime.now(timezone.utc) < until:
        return True
    _alert_suppression.pop(site_id, None)
    _suppressed_alert_count.pop(site_id, None)
    return False


def _get_latest_reviews(conn, site_ids):
    """Return dict mapping site_id -> latest review row only if still suppressed."""
    if not site_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (site_id) site_id, reviewed_by, reviewed_at, timeout_until "
            "FROM anomaly_review WHERE site_id = ANY(%s::uuid[]) "
            "  AND timeout_until IS NOT NULL AND timeout_until > now() "
            "ORDER BY site_id, reviewed_at DESC",
            (list(site_ids),),
        )
        return {
            str(r[0]): {
                "reviewed_by": r[1],
                "reviewed_at": r[2].isoformat(),
                "timeout_until": r[3].isoformat() if r[3] else None,
            }
            for r in cur.fetchall()
        }


@bp.post("/api/reviews")
@login_required
def submit_review():
    """Submit an anomaly review for a site, storing the reviewer's notes."""
    data = request.get_json(silent=True) or {}
    site_id = (data.get("site_id") or "").strip()
    comment = (data.get("comment") or "").strip()
    timeout_minutes = data.get("timeout_minutes", 0)
    severity = (data.get("severity") or "").strip()
    status_at_review = (data.get("status_at_review") or "unknown").strip()

    if not site_id:
        return jsonify(error="site_id is required"), 400

    try:
        timeout_minutes = max(0, min(int(timeout_minutes), 1440))  # cap at 24h
    except (TypeError, ValueError):
        timeout_minutes = 0

    reviewer_name = session.get("full_name", "Unknown")
    user_id = session.get("user_id")

    timeout_until = None
    if timeout_minutes > 0:
        timeout_until = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)
        _alert_suppression[site_id] = timeout_until
        _suppressed_alert_count[site_id] = 0

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO anomaly_review "
                    "(site_id, reviewed_by, user_id, status_at_review, comment, "
                    " timeout_minutes, timeout_until, severity) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING review_id, reviewed_at",
                    (site_id, reviewer_name, user_id, status_at_review,
                     comment or None, timeout_minutes,
                     timeout_until.isoformat() if timeout_until else None,
                     severity or None),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()

        _clear_pending_alert(site_id)
        write_audit_log(
            action="alert.acknowledged",
            old_setting={"status": status_at_review},
            new_setting={
                "site_id": site_id,
                "severity": severity or None,
                "timeout_minutes": timeout_minutes,
            },
        )

        return jsonify(
            success=True,
            review_id=str(row[0]),
            reviewed_at=row[1].isoformat(),
            reviewed_by=reviewer_name,
        ), 201

    except Exception as e:
        current_app.logger.warning("Failed to submit review: %s", e)
        return jsonify(error="Failed to submit review"), 500


@bp.get("/api/reviews/<site_id>")
@login_required
def get_reviews(site_id):
    """Return the review log for a specific site, newest first."""
    limit = min(int(request.args.get("limit", 50)), 200)
    reviews = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT review_id, reviewed_by, reviewed_at, "
                    "       status_at_review, comment, timeout_minutes, "
                    "       timeout_until, severity "
                    "FROM anomaly_review "
                    "WHERE site_id = %s "
                    "ORDER BY reviewed_at DESC LIMIT %s",
                    (site_id, limit),
                )
                for r in cur.fetchall():
                    reviews.append({
                        "review_id": str(r[0]),
                        "reviewed_by": r[1],
                        "reviewed_at": r[2].isoformat(),
                        "status_at_review": r[3],
                        "comment": r[4] or "",
                        "timeout_minutes": r[5],
                        "timeout_until": r[6].isoformat() if r[6] else None,
                        "severity": r[7] or "",
                    })
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load reviews for %s: %s", site_id, e)
        return jsonify(error="Failed to load reviews"), 500

    return jsonify(reviews=reviews)


@bp.post("/api/reviews/<site_id>/cancel")
@login_required
@role_required("asset_owner")
def cancel_suppression(site_id):
    """Admin-only: cancel an active suppression for a site and log it."""
    _alert_suppression.pop(site_id, None)
    _suppressed_alert_count.pop(site_id, None)

    reviewer_name = session.get("full_name", "Unknown")
    user_id = session.get("user_id")

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE anomaly_review SET timeout_until = now() "
                    "WHERE site_id = %s AND timeout_until IS NOT NULL AND timeout_until > now()",
                    (site_id,),
                )
                cur.execute(
                    "INSERT INTO anomaly_review "
                    "(site_id, reviewed_by, user_id, status_at_review, comment, "
                    " timeout_minutes, timeout_until, severity) "
                    "VALUES (%s, %s, %s, 'suppression_cancelled', "
                    " 'Suppression cancelled by admin', 0, NULL, NULL) "
                    "RETURNING review_id, reviewed_at",
                    (site_id, reviewer_name, user_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to cancel suppression for %s: %s", site_id, e)
        return jsonify(error="Failed to cancel suppression"), 500

    _clear_pending_alert(site_id)
    write_audit_log(
        action="alert.suppression_cancelled",
        old_setting=None,
        new_setting={"site_id": site_id},
    )

    return jsonify(success=True)


def _export_csv(table_name, select_cols, col_labels, filename_prefix):
    """Shared helper for CSV exports."""
    start_str = request.args.get("start", "").strip()
    end_str = request.args.get("end", "").strip()

    if not start_str or not end_str:
        return jsonify(error="start and end query parameters are required"), 400

    try:
        start_dt = parse_timestamp(start_str)
        end_dt = parse_timestamp(end_str)
    except Exception:
        return jsonify(error="Invalid date format."), 400

    if end_dt <= start_dt:
        return jsonify(error="end must be after start"), 400

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    sql = (
        f"SELECT {select_cols} "
        f"FROM {table_name} "
        f"WHERE time >= '{start_iso}' AND time <= '{end_iso}' "
        f"ORDER BY time ASC"
    )

    try:
        result = current_app.extensions["influx3"].query(sql)
        if result is None:
            return jsonify(error="No data found for the selected range"), 404

        df = result.to_pandas()
        if df.empty:
            return jsonify(error="No data found for the selected range"), 404

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")

        df = df.rename(columns={k: v for k, v in col_labels.items() if k in df.columns})

        output = io.StringIO()
        df.to_csv(output, index=False)
        csv_bytes = output.getvalue().encode("utf-8")

        filename = f"{filename_prefix}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.csv"
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as exc:
        current_app.logger.warning("CSV export failed: %s", exc)
        return jsonify(error="Failed to export data"), 500


@bp.get("/api/export/sensors")
@login_required
def export_sensors_csv():
    """Export sensor readings within a time range as a downloadable CSV."""
    field = request.args.get("field", "").strip()
    if field and field not in ALLOWED_FIELDS:
        return jsonify(error="Invalid field"), 400

    cols = f"time, device_id, {field}" if field else "time, device_id, temperature_c, humidity_pct, co2_ppm, aqi"
    labels = {
        "time": "Timestamp (UTC)", "device_id": "Device ID",
        "temperature_c": "Temperature (°C)", "humidity_pct": "Humidity (%)",
        "co2_ppm": "CO2 (ppm)", "aqi": "Air Quality Index",
    }
    return _export_csv("readings", cols, labels, "sensor_readings")


@bp.get("/api/export/thermal")
@login_required
def export_thermal_csv():
    """Export thermal image metadata within a time range as a downloadable CSV."""
    labels = {
        "time": "Timestamp (UTC)", "camera_id": "Camera ID",
        "filename": "Filename", "size_bytes": "File Size (bytes)",
        "content_type": "Content Type",
    }
    return _export_csv(
        "camera_images",
        "time, camera_id, filename, size_bytes, content_type",
        labels, "thermal_images",
    )
    
@bp.get("/api/get/")
@login_required
def get_sensor_values():
    """Return raw sensor readings in a time window, including site names.

    Query params:
        start (str, optional): ISO8601 or epoch start timestamp.
        end (str, optional): ISO8601 or epoch end timestamp.
        scope (int, optional): Minutes to look back when ``start`` is omitted.
            If ``end`` is also omitted, the current UTC time is used.
        site_name (str, optional): Site name to restrict results.
        limit (int, optional): Max rows to return, default 5000, max 10000.

    Returns:
        Response: JSON payload with site metadata and matching sensor rows.
    """
    start_str = request.args.get("start", "").strip()
    end_str = request.args.get("end", "").strip()
    scope_arg = request.args.get("scope", "").strip()
    site_name_arg = request.args.get("site_name", "").strip()
    limit_arg = request.args.get("limit", "5000").strip()

    if not start_str and not scope_arg:
        return jsonify(error="start or scope query parameter is required"), 400

    if end_str:
        try:
            end_dt = parse_timestamp(end_str)
        except Exception:
            return jsonify(error="Invalid date format."), 400
    else:
        end_dt = datetime.now(timezone.utc)

    if start_str:
        try:
            start_dt = parse_timestamp(start_str)
        except Exception:
            return jsonify(error="Invalid start date format."), 400
    else:
        try:
            scope_minutes = int(scope_arg)
        except (TypeError, ValueError):
            return jsonify(error="scope must be an integer number of minutes"), 400
        if scope_minutes <= 0:
            return jsonify(error="scope must be greater than 0"), 400
        start_dt = end_dt - timedelta(minutes=scope_minutes)

    if end_dt <= start_dt:
        return jsonify(error="end must be after start"), 400

    try:
        limit = max(1, min(int(limit_arg), 10000))
    except (TypeError, ValueError):
        return jsonify(error="limit must be an integer"), 400

    allowed = get_user_allowed_site_ids()

    start_iso = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    site_rows = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                if allowed is None:
                    cur.execute("SELECT site_id, name FROM site ORDER BY name")
                else:
                    if not allowed:
                        return jsonify(
                            site_id=None,
                            site_name=None,
                            start=start_iso,
                            end=end_iso,
                            readings=[],
                            total=0,
                        )
                    cur.execute(
                        "SELECT site_id, name FROM site "
                        "WHERE site_id = ANY(%s::uuid[]) ORDER BY name",
                        (list(allowed),),
                    )
                site_rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        current_app.logger.warning("Failed to load site names for sensor query: %s", exc)
        return jsonify(error="Failed to load site metadata"), 500

    site_map = {str(row[0]): row[1] for row in site_rows}
    selected_site_id = None
    selected_site_name = None

    if site_name_arg:
        site_name_arg_lower = site_name_arg.lower()
        for row in site_rows:
            row_site_id = str(row[0])
            row_site_name = row[1]
            if row_site_name and row_site_name.lower() == site_name_arg_lower:
                selected_site_id = row_site_id
                selected_site_name = row_site_name
                break
        if not selected_site_id:
            return jsonify(error="Unknown site_name"), 404

    site_filter = ""
    if selected_site_id:
        site_filter = f" AND site_id = '{_escape_str_field(selected_site_id)}'"
    elif allowed is not None:
        escaped = ",".join(f"'{_escape_str_field(s)}'" for s in site_map)
        if not escaped:
            return jsonify(
                site_id=None,
                site_name=None,
                start=start_iso,
                end=end_iso,
                readings=[],
                total=0,
            )
        site_filter = f" AND site_id IN ({escaped})"

    sql = (
        "SELECT time, site_id, device_id, temperature_c, humidity_pct, co2_ppm, aqi "
        "FROM readings "
        f"WHERE time >= '{start_iso}' AND time <= '{end_iso}'{site_filter} "
        "ORDER BY time ASC "
        f"LIMIT {limit}"
    )

    try:
        table = current_app.extensions["influx3"].query(sql)
        if table is None:
            return jsonify(
                site_id=selected_site_id,
                site_name=selected_site_name,
                start=start_iso,
                end=end_iso,
                readings=[],
                total=0,
            )

        df = table.to_pandas()
        if df.empty:
            return jsonify(
                site_id=selected_site_id,
                site_name=selected_site_name,
                start=start_iso,
                end=end_iso,
                readings=[],
                total=0,
            )

        readings = []
        for _, row in df.iterrows():
            row_site_id = str(row["site_id"]) if pd.notna(row["site_id"]) else None
            device_id = str(row["device_id"]) if pd.notna(row["device_id"]) else None
            reading_time = pd.to_datetime(row["time"], utc=True).isoformat().replace("+00:00", "Z")
            readings.append({
                "time": reading_time,
                "site_id": row_site_id,
                "site_name": site_map.get(row_site_id, row_site_id),
                "device_id": device_id,
                "temperature_c": float(row["temperature_c"]),
                "humidity_pct": float(row["humidity_pct"]),
                "co2_ppm": float(row["co2_ppm"]),
                "aqi": float(row["aqi"]),
            })

        response = {
            "site_id": selected_site_id,
            "site_name": selected_site_name,
            "start": start_iso,
            "end": end_iso,
            "limit": limit,
            "readings": readings,
            "total": len(readings),
        }
        if not selected_site_id:
            response["sites"] = [
                {"site_id": sid, "site_name": name}
                for sid, name in site_map.items()
            ]

        return jsonify(response)

    except Exception as exc:
        current_app.logger.warning("Failed to query sensor values: %s", exc)
        return jsonify(error="Failed to query sensor values"), 500