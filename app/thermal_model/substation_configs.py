import json as _json
import os as _os

_zones_path = _os.path.join(_os.path.dirname(__file__), "substation_zones.json")
with open(_zones_path) as _f:
    SUBSTATION_ZONES = _json.load(_f)

# Overheating thresholds (°C) for each equipment zone, ordered to match SUBSTATION_ZONES zone indices.
EQUIPMENT_THRESHOLDS = {"Battery": 41.5, "Switchboard": 41.0}


def fetch_zones_from_db(camera_id, conn):
    """Return zone dicts for camera_id ordered by sort_order.

    Keys match SUBSTATION_ZONES (startY/endY/startX/endX/name) so callers
    are interchangeable with the JSON fallback. sort_order preserves the
    index mapping expected by update_equipment_counters.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, start_y, end_y, start_x, end_x "
            "FROM camera_zone WHERE camera_id = %s ORDER BY sort_order, name",
            (camera_id,),
        )
        return [
            {"name": r[0], "startY": r[1], "endY": r[2], "startX": r[3], "endX": r[4]}
            for r in cur.fetchall()
        ]


def get_zone_temperatures(camera_id, temp_array, zones=None):
    """
    Given a camera_id and a flat 768-float array (24 rows × 32 cols from MLX90640),
    return the max temperature (°C) per defined equipment zone.
    Pi sends this array alongside the image as the temp_data form field.
    Returns an empty list if the camera_id has no zone config.

    Pass zones explicitly (fetched from DB) to avoid the JSON fallback.
    """
    if zones is None:
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
    """Map zone temperatures to EQUIPMENT_THRESHOLDS by index; update counters in-place.

    zone_temps order must match EQUIPMENT_THRESHOLDS insertion order — enforced by
    sort_order on camera_zone. counters is a persistent {name: breach_count} dict
    shared across requests; status becomes 'critical' on 3 consecutive breaches.
    Returns {name: {"temp": float, "status": str, "threshold": float}} per zone.
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
