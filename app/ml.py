import numpy as np
import pandas as pd
import joblib
import os
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo



SENSORS = ["temperature_c", "humidity_pct", "co2_ppm", "aqi"]
TICKS = 5

MONTHLY_HUMIDITY = {
    1: 58, 2: 60, 3: 58, 4: 54, 5: 50, 6: 47,
    7: 45, 8: 44, 9: 47, 10: 51, 11: 55, 12: 57,
}

sensor_files        = {sensor: joblib.load(os.path.join(os.path.dirname(__file__), f"models/isolation_forest_{sensor}_sensor.joblib")) for sensor in SENSORS}
zscore_windows = {}
anomaly_event_counts = {}

def per_site_ml(site_id):
    if site_id not in zscore_windows or site_id not in anomaly_event_counts:
        zscore_windows[site_id] = {sensor: deque(maxlen=720) for sensor in SENSORS}
        anomaly_event_counts[site_id] = {sensor: 0 for sensor in SENSORS}
        current_month = datetime.now(ZoneInfo("Australia/Brisbane")).month

        baseline_zscore_fill = {
            "temperature_c": (24.0, 1.5), 
            "humidity_pct":  (MONTHLY_HUMIDITY[current_month], 2.0),
            "co2_ppm":       (460.0, 8.0),
            "aqi":           (12.0, 2.0),
        }
        for sensor, (mean, std) in baseline_zscore_fill.items():
            for _ in range(720):
                zscore_windows[site_id][sensor].append(mean + np.random.normal(0, std))
    return zscore_windows[site_id], anomaly_event_counts[site_id]

def get_zscore(site_id, sensor, value):
    window = zscore_windows[site_id][sensor]
    if len(window) < 2:
        window.append(value)
        return 0.0
    mean = np.mean(window)
    std  = np.std(window) or 1.0
    zscore = (value - mean) / std
    window.append(value)
    return zscore

def get_residual(sensor, value, hour, month):
    baselines = sensor_files[sensor]["baselines"]
    expected = baselines.loc[(hour, month), sensor]
    return value - expected

def get_features(sensor):
    return [sensor, "hour", "month", f"{sensor}_zscore", f"{sensor}_residual"]


def score_reading(reading, site_id, display_name=None):
    per_site_ml(site_id)

    now_local = datetime.now(ZoneInfo("Australia/Brisbane"))
    hour = now_local.hour
    month = now_local.month

    scores = {}
    is_anomaly = {}

    for sensor in SENSORS:
        value = float(reading[sensor])
        zscore = get_zscore(site_id, sensor, value)
        residual = get_residual(sensor, value, hour, month)
        
        df = pd.DataFrame([{
            sensor: value,
            "hour": hour,
            "month": month,
            f"{sensor}_residual": residual,
            f"{sensor}_zscore": zscore,
        }])

        model = sensor_files[sensor]["model"]
        threshold = sensor_files[sensor]["threshold"]
        score = model.decision_function(df[get_features(sensor)])[0]

        if display_name == "Roma Street Central":
            print(f"[{display_name} REG READING]  {sensor} {value} score={score:.4f} threshold={threshold:.4f} zscore={zscore:.2f} residual={residual:.2f}")

        scores[sensor] = score
        is_anomaly[sensor] = bool(score < threshold)

        if display_name == "Roma Street Central":
            if is_anomaly[sensor]:
                print(f"[{display_name} !DETECT!]  {sensor} Anomaly detected: {is_anomaly[sensor]} (score={score:.4f} < threshold={threshold:.4f})")

    any_anomaly = False
    anomaly_run = {}
    event_counts = anomaly_event_counts[site_id]

    for sensor in SENSORS: 
        if is_anomaly[sensor]: 
            event_counts[sensor] += 1
        else: 
            event_counts[sensor] = 0

        if event_counts[sensor] >= TICKS:
            anomaly_run[sensor] = True
            any_anomaly = True
        else: 
            anomaly_run[sensor] = False
    
    #print anomaly run status 
    if display_name == "Roma Street Central":
        for sensor in SENSORS:
            print(f"[{display_name} ANOM RUN] {sensor} anomaly run: {anomaly_run[sensor]} (event count={event_counts[sensor]})")

    return scores, anomaly_run, any_anomaly


def data_analytics(reading, site_id, anomaly_run):
    active_anomalies = {x for x in SENSORS if anomaly_run[x]}
    analysis = []


    if not active_anomalies:
        return {"analysis": analysis}
    
    #all
    if active_anomalies == {"temperature_c", "humidity_pct", "co2_ppm", "aqi"}:
        analysis.append("All sensors anomalous - possible fire with personnel present or severe failure")

    # 3 sensor 
    # door open AND person inside
    elif {"temperature_c", "humidity_pct", "co2_ppm"}.issubset(active_anomalies):
        analysis.append("Temp, humidity, and CO2 rising together indicating possible personnel access")

    # 2 sensor
    # potential door open/leak - temp and humidity rising, c02 normal - no human presence 
    elif {"temperature_c", "humidity_pct"}.issubset(active_anomalies):
        analysis.append("Possible AC failure, door open or breach - temperature and humidity rising together")

    # fire/smoke
    elif {"temperature_c", "aqi"}.issubset(active_anomalies):
        analysis.append("Possible fire or smoke - temperature and AQI rising together")

    # 1 sensor 
    # overheating
    elif active_anomalies == {"temperature_c"}:
        analysis.append("Possible equipment overheating - temperature anomaly")

    elif active_anomalies == {"humidity_pct"}:
        analysis.append("Humidity rising - possible ventilation issue")
    
    # person inside - CO2 rising, no temp/humidity event
    elif active_anomalies == {"co2_ppm"}:
        analysis.append("Possible personnel inside - CO2 rising")

    # polutant/contamination 
    elif active_anomalies == {"aqi"}:
        analysis.append("Possible polution or contamination - AQI anomaly")

    return {"analysis": analysis}