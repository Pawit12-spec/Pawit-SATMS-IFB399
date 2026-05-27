"""
Replaces the Pi for demos. Sends sensor readings and thermal images to the app.

Modes:
  normal      readings + normal thermal images, no alerts
  hotspot     uploads hotspot images, CNN fires and sends email/SMS
  equipment   injects hot pixels into equipment zones, alert on 3rd upload
  anomaly     pushes sensor readings above thresholds, ML flags the site
  offline     sends for --offline-after seconds then stops, failsafe fires ~20s later

Usage:
  python3 demo_generator.py
  python3 demo_generator.py --mode hotspot
  python3 demo_generator.py --mode equipment
  python3 demo_generator.py --mode offline --offline-after 30
  python3 demo_generator.py --site fortitude-valley --mode hotspot
"""

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_URL      = "http://127.0.0.1:5050"
READINGS_URL  = f"{BASE_URL}/submit/readings"
CAMERA_URL    = f"{BASE_URL}/submit/camera"
INTERVAL      = 10
THERMAL_EVERY = 1
TIMEOUT       = 5

BRISBANE = ZoneInfo("Australia/Brisbane")

SCRIPT_DIR  = Path(__file__).parent
HOTSPOT_DIR = SCRIPT_DIR / "current-branch/tests/alert_test_images/irregular_hotspot"
NORMAL_DIR  = SCRIPT_DIR / "current-branch/tests/alert_test_images/normal"

HOTSPOT_IMAGES = sorted(HOTSPOT_DIR.glob("hotspot_*.png"))
NORMAL_IMAGES  = sorted(NORMAL_DIR.glob("normal_*.png"))

GRID_ROWS, GRID_COLS = 24, 32

ZONES = {
    "Battery":     {"startY": 5,  "endY": 13, "startX": 7,  "endX": 13},
    "Switchboard": {"startY": 10, "endY": 15, "startX": 20, "endX": 27},
}
THRESHOLDS = {"Battery": 41.5, "Switchboard": 41.0}

SITES = {
    "roma-street":      {"device_id": "pi-roma-street",      "base_temp": 27.0, "base_humidity": 55.0, "base_co2": 460},
    "fortitude-valley": {"device_id": "pi-fortitude-valley", "base_temp": 26.5, "base_humidity": 57.0, "base_co2": 459},
    "south-brisbane":   {"device_id": "pi-south-brisbane",   "base_temp": 27.5, "base_humidity": 53.0, "base_co2": 461},
    "west-end":         {"device_id": "pi-west-end",         "base_temp": 26.0, "base_humidity": 56.0, "base_co2": 462},
    "paddington":       {"device_id": "pi-paddington",       "base_temp": 27.2, "base_humidity": 54.0, "base_co2": 450},
}


def normal_temp_array(ambient=28.0):
    return [round(ambient + random.gauss(0, 1.5), 2) for _ in range(GRID_ROWS * GRID_COLS)]


def equipment_hot_temp_array(overheat_temp=50.0, ambient=28.0):
    arr = normal_temp_array(ambient)
    zone = ZONES["Battery"]
    for y in range(zone["startY"], zone["endY"]):
        for x in range(zone["startX"], zone["endX"]):
            arr[y * GRID_COLS + x] = round(overheat_temp + random.gauss(0, 0.5), 2)
    return arr


def normal_reading(site_cfg):
    return {
        "site_id":       site_cfg["site_id"],
        "device_id":     site_cfg["device_id"],
        "temperature_c": round(site_cfg["base_temp"] + random.gauss(0, 0.8), 2),
        "humidity_pct":  round(site_cfg["base_humidity"] + random.gauss(0, 2.0), 2),
        "co2_ppm":       int(site_cfg["base_co2"] + random.gauss(0, 10)),
        "aqi":           round(12.0 + random.gauss(0, 1.5), 1),
        "timestamp":     datetime.now(BRISBANE).isoformat(),
    }


def anomaly_reading(site_cfg):
    return {
        "site_id":       site_cfg["site_id"],
        "device_id":     site_cfg["device_id"],
        "temperature_c": round(42.0 + random.gauss(0, 0.5), 2),
        "humidity_pct":  round(85.0 + random.gauss(0, 1.0), 2),
        "co2_ppm":       int(1800 + random.gauss(0, 20)),
        "aqi":           round(75.0 + random.gauss(0, 2.0), 1),
        "timestamp":     datetime.now(BRISBANE).isoformat(),
    }


def send_reading(payload):
    try:
        r = requests.post(READINGS_URL, json=payload, timeout=TIMEOUT)
        print(
            f"  [reading] {payload['site_id']}  "
            f"temp={payload['temperature_c']}°C  "
            f"humidity={payload['humidity_pct']}%  "
            f"co2={payload['co2_ppm']}ppm  "
            f"aqi={payload['aqi']}  "
            f"→ {r.status_code}"
        )
    except requests.RequestException as e:
        print(f"  [reading] failed: {e}")


def send_thermal(image_path, camera_id, temp_array):
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                CAMERA_URL,
                files={"image": (image_path.name, f, "image/png")},
                data={"camera_id": camera_id, "temp_data": json.dumps(temp_array)},
                timeout=TIMEOUT,
            )
        print(f"  [thermal] {image_path.name} → {r.status_code}")
        if r.status_code == 201:
            print(f"           saved as {r.json().get('filename')}")
    except requests.RequestException as e:
        print(f"  [thermal] failed: {e}")


def run_normal(site_cfg, camera_id):
    print("normal mode — readings + normal thermal images. ctrl+c to stop.\n")
    tick = 0
    while True:
        send_reading(normal_reading(site_cfg))
        if tick % THERMAL_EVERY == 0 and NORMAL_IMAGES:
            send_thermal(random.choice(NORMAL_IMAGES), camera_id, normal_temp_array())
        tick += 1
        time.sleep(INTERVAL)


def run_hotspot(site_cfg, camera_id):
    print("hotspot mode — uploading hotspot images, alert fires each upload.\n")
    if not HOTSPOT_IMAGES:
        print("no hotspot images found in", HOTSPOT_DIR)
        return
    tick = 0
    img_cycle = 0
    while True:
        send_reading(normal_reading(site_cfg))
        if tick % THERMAL_EVERY == 0:
            img = HOTSPOT_IMAGES[img_cycle % len(HOTSPOT_IMAGES)]
            send_thermal(img, camera_id, normal_temp_array())
            img_cycle += 1
        tick += 1
        time.sleep(INTERVAL)


def run_equipment(site_cfg, camera_id):
    print(f"equipment mode — injecting Battery zone temp ~50°C (threshold {THRESHOLDS['Battery']}°C), alert fires on 3rd upload.\n")
    if not NORMAL_IMAGES:
        print("no normal images found in", NORMAL_DIR)
        return
    tick = 0
    upload_count = 0
    while True:
        send_reading(normal_reading(site_cfg))
        if tick % THERMAL_EVERY == 0:
            arr = equipment_hot_temp_array()
            send_thermal(random.choice(NORMAL_IMAGES), camera_id, arr)
            upload_count += 1
            battery_zone = ZONES["Battery"]
            battery_temp = arr[battery_zone["startY"] * GRID_COLS + battery_zone["startX"]]
            status = "alert sent" if upload_count >= 3 else f"{3 - upload_count} more to trigger alert"
            print(f"  [equipment] upload #{upload_count} — Battery ~{battery_temp:.1f}°C (threshold {THRESHOLDS['Battery']}°C) — {status}")
        tick += 1
        time.sleep(INTERVAL)


def run_anomaly(site_cfg, camera_id):
    print("anomaly mode — sensor readings above thresholds, ML will flag the site.\n")
    tick = 0
    while True:
        send_reading(anomaly_reading(site_cfg))
        if tick % THERMAL_EVERY == 0 and NORMAL_IMAGES:
            send_thermal(random.choice(NORMAL_IMAGES), camera_id, normal_temp_array())
        tick += 1
        time.sleep(INTERVAL)


def run_offline(site_cfg, camera_id, offline_after):
    print(f"offline mode — sending for {offline_after}s then stopping. failsafe fires ~20s after.\n")
    start = time.time()
    tick = 0
    while time.time() - start < offline_after:
        send_reading(normal_reading(site_cfg))
        if tick % THERMAL_EVERY == 0 and NORMAL_IMAGES:
            send_thermal(random.choice(NORMAL_IMAGES), camera_id, normal_temp_array())
        tick += 1
        time.sleep(INTERVAL)
    print(f"\nstopped. device '{site_cfg['device_id']}' is now silent — check email/SMS shortly.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal",
                        choices=["normal", "hotspot", "equipment", "anomaly", "offline"])
    parser.add_argument("--site", default="roma-street", choices=list(SITES.keys()))
    parser.add_argument("--camera-id", default="Substation_Alpha_Cam1")
    parser.add_argument("--offline-after", type=int, default=30)
    args = parser.parse_args()

    site_cfg = {**SITES[args.site], "site_id": args.site}

    print(f"site: {args.site}  device: {site_cfg['device_id']}  camera: {args.camera_id}\n")

    try:
        if   args.mode == "normal":    run_normal(site_cfg, args.camera_id)
        elif args.mode == "hotspot":   run_hotspot(site_cfg, args.camera_id)
        elif args.mode == "equipment": run_equipment(site_cfg, args.camera_id)
        elif args.mode == "anomaly":   run_anomaly(site_cfg, args.camera_id)
        elif args.mode == "offline":   run_offline(site_cfg, args.camera_id, args.offline_after)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
