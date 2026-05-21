SUBSTATION_ZONES = {
    "Substation_Alpha_Cam1": [
        {"startY": 5, "endY": 13, "startX": 7,  "endX": 13},  # Transformer 1
        {"startY": 10, "endY": 15, "startX": 20, "endX": 27},  # Switchboard
    ],
    "Substation_Beta_Cam1": [
        {"startY": 5, "endY": 20, "startX": 12, "endX": 26},   # Central Equipment
    ],
}

# Overheating thresholds (°C) for each equipment zone, ordered to match SUBSTATION_ZONES zone indices.
EQUIPMENT_THRESHOLDS = {"Battery": 41.5, "Switchboard": 41.0}


def get_zone_temperatures(camera_id, temp_array):
    """
    Given a camera_id and a flat 768-float array (24 rows × 32 cols from MLX90640),
    return the max temperature (°C) per defined equipment zone.
    Pi sends this array alongside the image as the temp_data form field.
    Returns an empty list if the camera_id has no zone config.
    """
    zones = SUBSTATION_ZONES.get(camera_id, [])
    result = []
    for zone in zones:
        temps = [
            temp_array[y * 32 + x]
            for y in range(zone["startY"], zone["endY"])
            for x in range(zone["startX"], zone["endX"])
        ]
        if temps:
            result.append(max(temps))
    return result


def update_equipment_counters(zone_temps, counters):
    """Update fault counters in-place from zone temperatures; return readings dict.

    Args:
        zone_temps: list of max °C per zone, same order as EQUIPMENT_THRESHOLDS.
        counters:   mutable dict {equipment_name: consecutive_breach_count}.

    Returns:
        dict {equipment_name: {"temp": float, "status": str, "threshold": float}}
    """
    readings = {}
    for i, (name, threshold) in enumerate(EQUIPMENT_THRESHOLDS.items()):
        if i >= len(zone_temps):
            break
        temp = zone_temps[i]
        if temp > threshold:
            counters[name] += 1
        else:
            counters[name] = 0
        count = counters[name]
        status = "critical" if count >= 3 else "warning" if count > 0 else "normal"
        readings[name] = {"temp": round(temp, 2), "status": status, "threshold": threshold}
    return readings
