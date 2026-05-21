import os
import sys
import uuid
from pathlib import Path
from functools import wraps
from PIL import Image
from datetime import *
from urllib3.exceptions import NewConnectionError, MaxRetryError
from collections import deque
import joblib
import pandas as pd
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


bp = Blueprint("main", __name__, template_folder="templates")
broker = SSEBroker()  # Handles Server-Sent Events

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


### SENSOR ML LOAD ###################################################################################

sensor_file         = joblib.load(os.path.join(os.path.dirname(__file__), "isolation_forest.joblib"))
sensor_model        = sensor_file  ["pipeline"]
# sensor_threshold    = sensor_file  ["threshold"]
sensor_threshold = -0.065


def score_reading(reading):
    """Return the Isolation Forest score and anomaly flag for one reading.

    Args:
        reading (Mapping): Parsed sensor payload containing temperature, humidity,
            CO₂, and AQI fields.

    Returns:
        tuple[float, bool]: The anomaly score and whether the reading is an outlier.
    """
    df = pd.DataFrame([{
        "temperature_c": reading["temperature_c"],
        "humidity_pct":  reading["humidity_pct"],
        "co2_ppm":       reading["co2_ppm"],
        "aqi":           reading["aqi"],
    }])
    score      = sensor_model.decision_function(df)[0]
    is_anomaly = score < sensor_threshold
    return score, bool(is_anomaly)


####################################################################################################

### average qs ##############################
temp_av_q = deque(maxlen=100)
humidity_av_q = deque(maxlen=100)
co2_av_q = deque(maxlen=100)
aqi_av_q = deque(maxlen=100)

def average(avq):
    """Compute the running average for a deque while handling empty queues.

    Args:
        avq (collections.deque): Rolling collection of numeric samples.

    Returns:
        float: Average of the deque contents, or 0.0 when empty.
    """
    return sum(avq) / len(avq) if avq else 0.0
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
                    "SELECT user_id, full_name, password_hash, role FROM app_user "
                    "WHERE email = %s AND is_active = TRUE",
                    (email,)
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row and check_password_hash(row[2], password):
            return {"user_id": str(row[0]), "full_name": row[1], "role": row[3]}
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
            return redirect(url_for("main.index"))
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


@bp.route("/")
@login_required
def index() -> str:
    """Render the main dashboard.

    Returns:
        str: Dashboard HTML template.
    """
    return render_template("index.html")

@bp.route("/temperature")
@login_required
def temperature() -> str:
    """Render the sensor page configured for temperature readings.

    Returns:
        str: Sensor template tailored to temperature metrics.
    """
    return render_template("sensor.html",
                           title="Temperature",
                           page_title="TEMPERATURE",
                           sensor_optimal_label="TEMPERATURE: 27°C ± 5%",
                           units="°C",
                           chart_title="Temperature",
                           table_heading="TEMPERATURE",
                           sensor_field="temperature_c",
                           sensor_color="#ef4444"
                           )
    
@bp.route("/humidity")
@login_required
def humidity() -> str:
    """Render the sensor page configured for humidity readings.

    Returns:
        str: Sensor template tailored to humidity metrics.
    """
    return render_template("sensor.html",
                           title="Humidity",
                           page_title="HUMIDITY",
                           sensor_optimal_label="HUMIDITY: 55% ± 5%",
                           units="%",
                           chart_title="Humidity",
                           table_heading="HUMIDITY",
                           sensor_field="humidity_pct",
                           sensor_color="#a855f7"
                           )


@bp.route("/co2")
@login_required
def co2() -> str:
    """Render the sensor page configured for CO₂ readings.

    Returns:
        str: Sensor template tailored to CO₂ metrics.
    """
    return render_template("sensor.html",
                           title="CO2",
                           page_title="CO2",
                           sensor_optimal_label="CO₂: 750 ppm ± 5%",
                           units="ppm",
                           chart_title="CO2",
                           table_heading="CO2",
                           sensor_field="co2_ppm",
                           sensor_color="#22d3ee"
                           )


@bp.route("/air-quality")
@login_required
def air_quality() -> str:
    """Render the sensor page configured for air quality index readings.

    Returns:
        str: Sensor template tailored to AQI metrics.
    """
    return render_template("sensor.html",
                           title="Air Quality",
                           page_title="AIR QUALITY",
                           sensor_optimal_label="AIR QUALITY: 750 μg/m³ ± 5%",
                           units="μg/m³",
                           chart_title="Air Quality",
                           table_heading="AIR QUALITY",
                           sensor_field="aqi",
                           sensor_color="#f59e0b"
                           )
    
@bp.route("/thermal")
@login_required
def thermal() -> str:
    """Render the thermal camera page with the latest image metadata.

    Returns:
        str: Thermal camera template populated with latest and recent images.
    """
    latest_image = get_latest_image()
    recent_images = get_recent_images(limit = 20)
    return render_template("thermal.html",
                           latest_image = latest_image,
                           recent_images = recent_images
    )

def get_latest_image() -> dict | None:
    """Fetch the newest thermal image metadata from InfluxDB.

    Returns:
        dict | None: Row containing filename and timestamp, or None when empty.
    """
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
    """Return the most recent thermal image records up to ``limit`` entries.

    Args:
        limit (int, optional): Maximum number of images to retrieve. Defaults to 20.

    Returns:
        list[dict]: Ordered list of filename/timestamp pairs.
    """
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
    """Serve a stored camera image from the configured upload directory.

    Args:
        filename (Path): Relative path segment of the image to retrieve.

    Returns:
        Response: Flask response streaming the requested file.
    """
    return send_from_directory(current_app.config["UPLOAD_DIR"], filename)


@bp.post("/submit/camera")
def upload_image():
    """Validate, persist, and index an uploaded thermal image.

    Returns:
        Response: JSON payload describing the stored image.
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
    now_dt = datetime.now(timezone.utc)
    now_ns = to_ns(now_dt)

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

    broker.publish(
        {
            "id": image_id,
            "filename": save_path.name,
            "size_bytes": save_path.stat().st_size,
            "content_type": f.mimetype,
            "camera_id": camera_id,
            "timestamp": now_dt.isoformat()
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
    ns = to_ns(ts)

    temp_av_q.append(temperature_c)
    humidity_av_q.append(humidity_pct)
    co2_av_q.append(co2_ppm)
    aqi_av_q.append(aqi)

    avg_temperature_c = average(temp_av_q)
    avg_humidity_pct = average(humidity_av_q)
    avg_co2_ppm = average(co2_av_q)
    avg_aqi = average(aqi_av_q)

    score, is_anomaly = score_reading(data)

    broker.publish(
        {
            "device_id": device_id,
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
        },
        event="reading",
        id=str(ns),
    )

    try:
        lp = (
            f"readings,device_id={_escape_tag(device_id)} "
            f"temperature_c={temperature_c},humidity_pct={humidity_pct},co2_ppm={co2_ppm},aqi={aqi} "
            f"{ns}"
        )
        current_app.extensions["influx3"].write(lp)
    except Exception as e:
        current_app.logger.warning("Influx write failed; skipping: %s", e)

    return jsonify(status="ok"), 201

@bp.route("/users")
@role_required("asset_owner")
def users() -> str:
    """Render the user administration page populated from PostgreSQL.

    Returns:
        str: Users template preloaded with account rows.
    """
    users_list = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, full_name, email, role, is_active "
                    "FROM app_user ORDER BY full_name"
                )
                users_list = [
                    {"user_id": str(r[0]), "full_name": r[1], "email": r[2], "role": r[3], "is_active": r[4]}
                    for r in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load users: %s", e)
    return render_template("users.html", users=users_list)

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
                    "FROM site_user su "
                    "JOIN site s ON s.site_id = su.site_id "
                    "WHERE su.user_id = %s",
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
                cur.execute(
                    "INSERT INTO site_user (site_id, user_id) VALUES (%s, %s) "
                    "ON CONFLICT (site_id, user_id) DO NOTHING",
                    (site_id, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update user assets: %s", e)
        return jsonify(error="Failed to update assets"), 500

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
                cur.execute(
                    "DELETE FROM site_user WHERE site_id = %s AND user_id = %s",
                    (site_id, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to remove user asset: %s", e)
        return jsonify(error="Failed to remove asset"), 500

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
    role = request.form.get("role", "user")
    
    if role not in ("user", "asset_owner"):
        role = "user"    
    
    if not full_name or not email:
        return redirect(url_for("main.users", error="All fields are required to be filled."))

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app_user SET full_name = %s, email = %s, role = %s WHERE user_id = %s",
                    (full_name, email, role, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except psycopg2.errors.UniqueViolation:
        return redirect(url_for("main.users", error="A user with that email already exists"))
    except Exception as e:
        current_app.logger.warning("Failed to update user: %s", e)
        return redirect(url_for("main.users", error="Failed to update user"))

    return redirect(url_for("main.users", success="User updated successfully"))
            

@bp.route("/users/create", methods=["POST"])
@role_required("asset_owner")
def create_user():
    """Create a new application user and persist it to PostgreSQL.

    Returns:
        Response: Redirect back to the users page with status feedback.
    """
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")

    if role not in ("user", "asset_owner"):
        role = "user"

    if not full_name or not email or not password:
        return redirect(url_for("main.users", error="All fields are required"))

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_user (full_name, email, password_hash, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (full_name, email, generate_password_hash(password), role)
                )
            conn.commit()
        finally:
            conn.close()
    except psycopg2.errors.UniqueViolation:
        return redirect(url_for("main.users", error="A user with that email already exists"))
    except Exception as e:
        current_app.logger.warning("Failed to create user: %s", e)
        return redirect(url_for("main.users", error="Failed to create user"))

    return redirect(url_for("main.users", success="User created successfully"))


@bp.route("/users/<user_id>/delete", methods=["POST"])
@role_required("asset_owner")
def delete_user(user_id):
    """Delete a user account.

    Args:
        user_id (str): Identifier of the user to delete.

    Returns:
        Response: Redirect back to the users page with status feedback.
    """
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_user WHERE user_id = %s", (user_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to delete user: %s", e)
        return redirect(url_for("main.users", error="Failed to delete user"))

    return redirect(url_for("main.users", success="User Deleted!"))


@bp.route("/users/<user_id>/role", methods=["POST"])
@role_required("asset_owner")
def update_role(user_id):
    """Update only the role for a user while validating allowed roles.

    Args:
        user_id (str): Identifier of the user to update.

    Returns:
        Response: Redirect back to the users page with status feedback.
    """

    # Make sure no one can just POST a different role lmao
    new_role = request.form.get("role", "user")
    if new_role not in ("user", "asset_owner"):
        new_role = "user"

    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app_user SET role = %s WHERE user_id = %s",
                    (new_role, user_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update role: %s", e)
        return redirect(url_for("main.users", error="Failed to update role"))

    return redirect(url_for("main.users", success="Role updated"))


@bp.route("/assets")
@role_required("asset_owner")
def assets():
    """Render the asset management page with site metadata from PostgreSQL.

    Returns:
        str: Assets template populated with current sites.
    """
    sites = []
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT site_id, name, room_name, region, address, latitude, longitude "
                    "FROM site ORDER BY name"
                )
                sites = [
                    {
                        "site_id": str(r[0]), "name": r[1], "room_name": r[2],
                        "region": r[3], "address": r[4],
                        "latitude": float(r[5]) if r[5] is not None else None,
                        "longitude": float(r[6]) if r[6] is not None else None,
                    }
                    for r in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to load sites: %s", e)
    return render_template("assets.html", sites=sites)


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
                    "INSERT INTO site (name, room_name, region, address, latitude, longitude) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (name, room_name, region, address, latitude, longitude),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to create site: %s", e)
        return redirect(url_for("main.assets", error="Failed to create site"))

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
                    "UPDATE site SET name=%s, room_name=%s, region=%s, address=%s, "
                    "latitude=%s, longitude=%s WHERE site_id=%s",
                    (name, room_name, region, address, latitude, longitude, site_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to update site: %s", e)
        return redirect(url_for("main.assets", error="Failed to update site"))

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
    try:
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM site WHERE site_id = %s", (site_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        current_app.logger.warning("Failed to delete site: %s", e)
        return redirect(url_for("main.assets", error="Failed to delete site"))

    return redirect(url_for("main.assets", success="Site deleted"))


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
####### Heartbeat monitoring for offline device detection and alerting (failsafe system) ##############
pi_alert_state = {} # device_id -> dict of alert milestones
pi_last_seen = {} # device_id -> timestamp of last heartbeat

def monitor_offline_devices(current_time):
    """The core logic, separated so Pytest can run it safely."""
    for device_id, last_heard in list(pi_last_seen.items()):
        
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
                    print(f"Failed to send alert: {e}")
            
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
                    print(f"Failed to send recovery alert: {e}")
                    
            # Reset all trackers so it is ready for the next failure
            pi_alert_state[device_id] = {
                "instant": False, "10m": False, "1h": False, 
                "last_daily": None, "is_offline": False
            }

# 1.THE BACKGROUND THREAD (Runs continuously)

def monitor_heartbeats():
    while True:
        monitor_offline_devices(time.time())
        time.sleep(10)

# Start the background monitor thread immediately
threading.Thread(target=monitor_heartbeats, daemon=True).start()


# THE FLASK ROUTE (Catches the ping from the Pi)

@bp.post("/submit/heartbeat")
def receive_heartbeat():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "pi_1"))
    
    # Update the tracker with the exact time the ping arrived.
    # We DO NOT reset the alert state here anymore so the background thread 
    # has a chance to see the transition and send the Recovery email!
    pi_last_seen[device_id] = time.time()
    
    return jsonify(status="ok"), 200
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
    """Return downsampled historical readings from InfluxDB.

    Query params:
        period (str): One of day, week, month, year.
        field (str): One of temperature_c, humidity_pct, co2_ppm, aqi.

    Returns:
        Response: JSON with chart (time/avg) and table (time/avg/min/max) arrays.
    """
    period = request.args.get("period", "day")
    field = request.args.get("field", "temperature_c")

    if period not in PERIOD_CFG:
        return jsonify(error="Invalid period"), 400
    if field not in ALLOWED_FIELDS:
        return jsonify(error="Invalid field"), 400

    cfg = PERIOD_CFG[period]
    time_filter = cfg["filter"]
    bucket = cfg["bucket"]

    try:
        table = current_app.extensions["influx3"].query(
            f"SELECT "
            f"  DATE_BIN(INTERVAL '{bucket}', time) AS bucket, "
            f"  AVG({field})   AS avg_val, "
            f"  MIN({field})   AS min_val, "
            f"  MAX({field})   AS max_val, "
            f"  COUNT({field}) AS sample_count "
            f"FROM readings "
            f"WHERE {time_filter} "
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

    try:
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

        # Reverse so oldest first (for chart rendering)
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
