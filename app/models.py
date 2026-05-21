
#!/usr/bin/env python3
"""
Create PostgreSQL database and schema (v3) for Substation Environmental Monitoring.

Changes:
- Single organisation (singleton table).
- One room per site (room table removed; optional site.room_name).
- hvac_policy renamed to climate_policy.
- Default port 5432.
"""
import argparse
import datetime as dt
import os
import sys

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SCHEMA_SQL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'metric_type') THEN
    CREATE TYPE metric_type AS ENUM (
      'temperature_c','humidity_pct','co2_ppm','tvoc_ppb','pm2_5_ugm3','presence_bool','air_quality_index','battery_temp_c'
    );
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'alert_operator') THEN
    CREATE TYPE alert_operator AS ENUM ('above','below','outside_range','inside_range','anomaly');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'scope_level') THEN
    CREATE TYPE scope_level AS ENUM ('site','device','sensor');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notify_channel') THEN
    CREATE TYPE notify_channel AS ENUM ('email','sms','webhook');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'alert_status') THEN
    CREATE TYPE alert_status AS ENUM ('open','acknowledged','cleared');
  END IF;
END$$;

CREATE TABLE IF NOT EXISTS site (
  site_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL,
  room_name  TEXT,
  region     TEXT,
  address    TEXT,
  latitude   NUMERIC(9,6),
  longitude  NUMERIC(9,6),
  escalation_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  escalation_timeout_minutes INT NOT NULL DEFAULT 30,
  secondary_contact_user_id UUID
);

CREATE TABLE IF NOT EXISTS device (
  device_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id       UUID NOT NULL REFERENCES site(site_id) ON DELETE CASCADE,
  device_type   TEXT NOT NULL,
  model         TEXT,
  serial_number TEXT UNIQUE,
  firmware_ver  TEXT,
  ip_address    INET,
  installed_at  TIMESTAMPTZ,
  is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS sensor (
  sensor_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id   UUID NOT NULL REFERENCES device(device_id) ON DELETE CASCADE,
  metric      metric_type NOT NULL,
  unit        TEXT NOT NULL,
  label       TEXT,
  label_norm  TEXT GENERATED ALWAYS AS (COALESCE(label, '')) STORED,
  UNIQUE (device_id, metric, label_norm)
);

CREATE TABLE IF NOT EXISTS climate_policy (
  policy_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id         UUID NOT NULL REFERENCES site(site_id) ON DELETE CASCADE,
  effective_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
  target_temp_c   NUMERIC(5,2) NOT NULL DEFAULT 27.00,
  target_rh_pct   NUMERIC(5,2) NOT NULL DEFAULT 55.00,
  rh_tolerance_pct NUMERIC(5,2) NOT NULL DEFAULT 5.00,
  mode            TEXT NOT NULL DEFAULT 'Dry',
  notes           TEXT
);

CREATE TABLE IF NOT EXISTS measurement (
  ts         TIMESTAMPTZ NOT NULL,
  sensor_id  UUID NOT NULL REFERENCES sensor(sensor_id) ON DELETE CASCADE,
  metric     metric_type NOT NULL,
  value_num  DOUBLE PRECISION NOT NULL,
  quality_code SMALLINT DEFAULT 0,
  source     TEXT,
  PRIMARY KEY (sensor_id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS thermal_image (
  image_id    BIGINT GENERATED ALWAYS AS IDENTITY,
  ts          TIMESTAMPTZ NOT NULL,
  device_id   UUID NOT NULL REFERENCES device(device_id) ON DELETE CASCADE,
  storage_uri TEXT NOT NULL,
  width_px    INT,
  height_px   INT,
  min_temp_c  NUMERIC(6,2),
  max_temp_c  NUMERIC(6,2),
  avg_temp_c  NUMERIC(6,2),
  hotspots    JSONB,
  sha256      TEXT,
  PRIMARY KEY (device_id, ts),
  UNIQUE (image_id, ts)
) PARTITION BY RANGE (ts);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_measurement_5min AS
SELECT
  sensor_id,
  metric,
  date_trunc('minute', ts) - (EXTRACT(minute FROM ts)::int % 5) * INTERVAL '1 minute' AS bucket_start,
  COUNT(*)       AS n,
  AVG(value_num) AS avg_val,
  MIN(value_num) AS min_val,
  MAX(value_num) AS max_val
FROM measurement
GROUP BY sensor_id, metric, bucket_start
WITH NO DATA;

CREATE INDEX IF NOT EXISTS mv_meas_5min_sensor_bucket ON mv_measurement_5min (sensor_id, bucket_start DESC);

CREATE TABLE IF NOT EXISTS alert_rule (
  rule_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  scope         scope_level NOT NULL,
  site_id       UUID REFERENCES site(site_id) ON DELETE CASCADE,
  device_id     UUID REFERENCES device(device_id) ON DELETE CASCADE,
  sensor_id     UUID REFERENCES sensor(sensor_id) ON DELETE CASCADE,
  metric        metric_type,
  operator      alert_operator NOT NULL,
  threshold_low DOUBLE PRECISION,
  threshold_high DOUBLE PRECISION,
  hold_for      INTERVAL NOT NULL DEFAULT INTERVAL '60 seconds',
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  params        JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (operator IN ('above','below') AND threshold_low IS NOT NULL AND threshold_high IS NULL)
    OR (operator IN ('outside_range','inside_range') AND threshold_low IS NOT NULL AND threshold_high IS NOT NULL)
    OR (operator = 'anomaly')
  )
);

CREATE TABLE IF NOT EXISTS alert_event (
  event_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_id      UUID NOT NULL REFERENCES alert_rule(rule_id) ON DELETE CASCADE,
  opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at    TIMESTAMPTZ,
  status       alert_status NOT NULL DEFAULT 'open',
  site_id      UUID,
  device_id    UUID,
  sensor_id    UUID,
  metric       metric_type,
  sample_count INT,
  min_value    DOUBLE PRECISION,
  max_value    DOUBLE PRECISION,
  details      JSONB
);

CREATE TABLE IF NOT EXISTS app_user (
  user_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name  TEXT NOT NULL,
  email     TEXT UNIQUE,
  phone_e164 TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  preferences JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notify_target (
  target_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID REFERENCES app_user(user_id) ON DELETE SET NULL,
  channel    notify_channel NOT NULL,
  address    TEXT NOT NULL,
  UNIQUE (channel, address)
);

CREATE TABLE IF NOT EXISTS notification (
  notification_id BIGSERIAL PRIMARY KEY,
  event_id   UUID NOT NULL REFERENCES alert_event(event_id) ON DELETE CASCADE,
  target_id  UUID NOT NULL REFERENCES notify_target(target_id) ON DELETE CASCADE,
  sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  status     TEXT NOT NULL DEFAULT 'sent',
  provider_id TEXT,
  response_meta JSONB
);

CREATE TABLE IF NOT EXISTS nlq_query_log (
  query_id     BIGSERIAL PRIMARY KEY,
  user_id      UUID REFERENCES app_user(user_id) ON DELETE SET NULL,
  asked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  question     TEXT NOT NULL,
  filters      JSONB,
  execution_ms INT,
  result_rows  INT
);

/* 
AUDIT LOG
*/
CREATE TABLE IF NOT EXISTS audit_log (
  action_id           BIGSERIAL PRIMARY KEY,
  action_user_id      UUID REFERENCES app_user(user_id) ON DELETE SET NULL,
  action_user_name    TEXT,
  action_taken        TEXT NOT NULL,
  action_time         TIMESTAMPTZ NOT NULL DEFAULT now(),
  old_setting         JSONB,
  new_setting         JSONB
  
);

CREATE OR REPLACE FUNCTION no_update_or_delete() 
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
RAISE EXCEPTION 'Updates and deletes are not permitted';
END; 
$$;

DROP TRIGGER IF EXISTS trg_tamperproof_audit_log ON audit_log;


CREATE TRIGGER trg_tamperproof_audit_log
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION no_update_or_delete();

/* 
END AUDIT LOG
*/


CREATE OR REPLACE VIEW v_measurement_retention AS
SELECT * FROM measurement WHERE ts < now() - INTERVAL '90 days';

CREATE OR REPLACE VIEW v_thermal_image_retention AS
SELECT * FROM thermal_image WHERE ts < now() - INTERVAL '180 days';

CREATE TABLE IF NOT EXISTS user_assets (
  site_id UUID NOT NULL REFERENCES site(site_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,

  PRIMARY KEY (site_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_user_assets_user
  ON user_assets (user_id);

CREATE INDEX IF NOT EXISTS idx_user_assets_site
  ON user_assets (site_id);

CREATE TABLE IF NOT EXISTS anomaly_review (
  review_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id          UUID NOT NULL REFERENCES site(site_id) ON DELETE CASCADE,
  reviewed_by      TEXT NOT NULL,
  user_id          UUID REFERENCES app_user(user_id) ON DELETE SET NULL,
  reviewed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  status_at_review TEXT NOT NULL,
  comment          TEXT,
  timeout_minutes  INT NOT NULL DEFAULT 0,
  timeout_until    TIMESTAMPTZ,
  severity TEXT,
  escalated_at TIMESTAMPTZ
  );

CREATE INDEX IF NOT EXISTS idx_anomaly_review_site
  ON anomaly_review (site_id, reviewed_at DESC);
"""

INDEXES_SQL_TEMPLATE = r"""
CREATE INDEX IF NOT EXISTS measurement_{ym}_ts_brin  ON measurement_{ym} USING BRIN (ts);
CREATE INDEX IF NOT EXISTS measurement_{ym}_s_ts_idx ON measurement_{ym} (sensor_id, ts DESC);

CREATE INDEX IF NOT EXISTS ti_{ym}_ts_brin     ON thermal_image_{ym} USING BRIN (ts);
CREATE INDEX IF NOT EXISTS ti_{ym}_device_ts   ON thermal_image_{ym} (device_id, ts DESC);
"""

def ym_iter(start_date, months):
    """Yield sequential year/month pairs for partition creation.

    Args:
        start_date (datetime.date): Anchor date whose year/month start the
            sequence.
        months (int): Number of months to yield, including the start month.

    Yields:
        tuple[int, int]: Two-element tuple containing ``(year, month)``.
    """
    y = start_date.year
    m = start_date.month
    for i in range(months):
        yy = y + (m + i - 1) // 12
        mm = (m + i - 1) % 12 + 1
        yield yy, mm

def month_bounds_utc(year, month):
    """Return UTC timestamps that bound the provided month.

    Args:
        year (int): Four-digit year.
        month (int): Month number in the range 1-12.

    Returns:
        tuple[str, str]: ISO-like timestamp strings for the month start and the
        first instant of the next month.
    """
    start = f"{year:04d}-{month:02d}-01 00:00:00+00"
    if month == 12:
        next_y, next_m = year + 1, 1
    else:
        next_y, next_m = year, month + 1
    end = f"{next_y:04d}-{next_m:02d}-01 00:00:00+00"
    return start, end

def create_db_if_needed(conn_params, dbname, owner):
    """Create the target database if it does not already exist.

    Args:
        conn_params (dict): Base connection parameters excluding ``dbname``.
        dbname (str): Name of the database to ensure exists.
        owner (str): PostgreSQL role that should own the database.
    """
    tmp = conn_params.copy()
    tmp["dbname"] = "postgres"
    conn = psycopg2.connect(**tmp)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
            if cur.fetchone() is None:
                print(f"Creating database '{dbname}' (owner: {owner}) ...")
                cur.execute(f'CREATE DATABASE "{dbname}" WITH OWNER "{owner}"')
            else:
                print(f"Database '{dbname}' already exists.")
    finally:
        conn.close()

def run_sql(conn, sql):
    """Execute the supplied SQL and commit the transaction.

    Args:
        conn (psycopg2.connection): Active connection to execute against.
        sql (str): Statement(s) to run.
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()

def ensure_partitions(conn, months_ahead=6, months_back=0):
    """Ensure partition tables and indexes exist for a rolling window.

    Args:
        conn (psycopg2.connection): Connection bound to the target database.
        months_ahead (int): Number of future months to provision partitions for.
        months_back (int): Number of past months (inclusive) that must exist.

    Raises:
        RuntimeError: If the parent partitioned tables are missing.
    """
    today = dt.date.today().replace(day=1)
    total_months = months_back + months_ahead + 1
    m_idx = (today.year * 12 + (today.month - 1)) - months_back
    start_year = m_idx // 12
    start_month = m_idx % 12 + 1
    start_date = dt.date(start_year, start_month, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.measurement')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("Parent table 'measurement' does not exist.")
        cur.execute("SELECT to_regclass('public.thermal_image')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("Parent table 'thermal_image' does not exist.")

    for yy, mm in ym_iter(start_date, total_months):
        ym = f"{yy}_{mm:02d}"
        start, end = month_bounds_utc(yy, mm)
        run_sql(conn, f"""
            CREATE TABLE IF NOT EXISTS measurement_{ym}
            PARTITION OF measurement FOR VALUES FROM ('{start}') TO ('{end}');
        """)
        run_sql(conn, f"""
            CREATE TABLE IF NOT EXISTS thermal_image_{ym}
            PARTITION OF thermal_image FOR VALUES FROM ('{start}') TO ('{end}');
        """)
        run_sql(conn, INDEXES_SQL_TEMPLATE.format(ym=ym))
        
def parse_args():
    """Parse CLI arguments for the database bootstrap script.

    Returns:
        argparse.Namespace: Parsed argument values pulled from the environment
        defaults and CLI flags.
    """
    p = argparse.ArgumentParser(description="Create PostgreSQL database & schema (v3).")
    p.add_argument("--host", default=os.getenv("PGHOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", 5432)))
    p.add_argument("--user", default=os.getenv("PGUSER", "postgres"))
    p.add_argument("--password", default=os.getenv("PGPASSWORD", "pass"))
    p.add_argument("--dbname", default=os.getenv("PGDATABASE", "EQL SAM"))
    p.add_argument("--owner", default=os.getenv("PGOWNER", "postgres"))
    p.add_argument("--create-db", action="store_true")
    p.add_argument("--partitions-ahead", type=int, default=6)
    p.add_argument("--partitions-back", type=int, default=0)
    return p.parse_args()

def main():
    """Entry point for invoking the schema installer via the CLI."""
    args = parse_args()
    base = dict(host=args.host, port=args.port, user=args.user, password=args.password)
    if args.create_db:
        create_db_if_needed(base, args.dbname, args.owner)

    conn = psycopg2.connect(dbname=args.dbname, **base)
    try:
        print("Applying schema ...")
        run_sql(conn, SCHEMA_SQL)
        print("Ensuring partitions ...")
        ensure_partitions(conn, months_ahead=args.partitions_ahead, months_back=args.partitions_back)
        print("Done.")
    finally:
        conn.close()

def init_database(host="localhost", port=5432, user="postgres", password="postgres", 
                  dbname="EQL_SATMS", owner="postgres", partitions_ahead=6, partitions_back=0):
    """Initialize the PostgreSQL database for SATMS deployments.

    Args:
        host (str): PostgreSQL server host.
        port (int): TCP port exposed by PostgreSQL.
        user (str): Role used for provisioning objects.
        password (str): Password for ``user``.
        dbname (str): Database name to create or reuse.
        owner (str): Role that should own the database if created.
        partitions_ahead (int): Months ahead that should exist as partitions.
        partitions_back (int): Months in the past that should exist as partitions.

    Returns:
        dict: ``status`` and ``message`` describing the outcome.
    """
    try:
        base = dict(host=host, port=port, user=user, password=password)
        
        # Create database if needed
        create_db_if_needed(base, dbname, owner)
        
        # Connect to the new database and apply schema
        conn = psycopg2.connect(dbname=dbname, **base)
        try:
            print("Applying schema ...")
            run_sql(conn, SCHEMA_SQL)
            run_sql(conn, "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'")
            run_sql(conn, "ALTER TABLE site ADD COLUMN IF NOT EXISTS escalation_enabled BOOLEAN NOT NULL DEFAULT FALSE")
            run_sql(conn, "ALTER TABLE site ADD COLUMN IF NOT EXISTS escalation_timeout_minutes INT NOT NULL DEFAULT 30")
            run_sql(conn, "ALTER TABLE site ADD COLUMN IF NOT EXISTS secondary_contact_user_id UUID")
            run_sql(conn, """
                DO $$ BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'site_secondary_contact_fk'
                  ) THEN
                    ALTER TABLE site
                      ADD CONSTRAINT site_secondary_contact_fk
                      FOREIGN KEY (secondary_contact_user_id)
                      REFERENCES app_user(user_id) ON DELETE SET NULL;
                  END IF;
                END $$;
            """)
            run_sql(conn, "ALTER TABLE anomaly_review ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ")
            print("Ensuring partitions ...")
            ensure_partitions(conn, months_ahead=partitions_ahead, months_back=partitions_back)
            print("Done.")
            return {"status": "success", "message": f"Database '{dbname}' initialized successfully"}
        finally:
            conn.close()
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    main()
