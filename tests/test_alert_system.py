"""
Alert system unit tests.

Covers:
  - get_zone_temperatures: known camera, unknown camera, max value selection
  - update_equipment_counters: escalation counter increment, reset, status levels
"""

import pytest
from app.thermal_model.substation_configs import (
    get_zone_temperatures,
    SUBSTATION_ZONES,
    EQUIPMENT_THRESHOLDS,
    update_equipment_counters,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def blank_array(val=0.0):
    return [val] * 768

def fresh_counters():
    return {name: 0 for name in EQUIPMENT_THRESHOLDS}


# ── 1. get_zone_temperatures ──────────────────────────────────────────────────

def test_unknown_camera_returns_empty():
    assert get_zone_temperatures("Unknown_Cam", blank_array()) == []


def test_known_camera_returns_one_result_per_zone():
    result = get_zone_temperatures("Substation_Alpha_Cam1", blank_array())
    assert len(result) == len(SUBSTATION_ZONES["Substation_Alpha_Cam1"])


def test_max_temp_in_zone_is_returned():
    arr = blank_array()
    # Place a hot pixel inside Substation_Alpha_Cam1 zone 0 (startY=5, endY=13, startX=7, endX=13)
    arr[5 * 32 + 7] = 55.0
    arr[6 * 32 + 8] = 42.0
    result = get_zone_temperatures("Substation_Alpha_Cam1", arr)
    assert result[0] == 55.0


def test_pixel_outside_zone_not_counted():
    arr = blank_array()
    # Hot pixel outside all zones for Substation_Alpha_Cam1
    arr[0 * 32 + 0] = 99.0
    result = get_zone_temperatures("Substation_Alpha_Cam1", arr)
    assert result[0] == 0.0


def test_multiple_zones_independent():
    arr = blank_array()
    # Zone 0 hot pixel (startY=5, startX=7)
    arr[5 * 32 + 7] = 50.0
    # Zone 1 hot pixel (startY=10, startX=20)
    arr[10 * 32 + 20] = 38.0
    result = get_zone_temperatures("Substation_Alpha_Cam1", arr)
    assert result[0] == 50.0
    assert result[1] == 38.0


# ── 2. update_equipment_counters ─────────────────────────────────────────────

def test_equipment_below_threshold_is_normal():
    counters = fresh_counters()
    readings = update_equipment_counters([30.0, 30.0], counters)
    for name in EQUIPMENT_THRESHOLDS:
        assert readings[name]["status"] == "normal"
        assert counters[name] == 0


def test_equipment_first_breach_is_warning():
    counters = fresh_counters()
    high_temps = [t + 10 for t in EQUIPMENT_THRESHOLDS.values()]
    readings = update_equipment_counters(high_temps, counters)
    for name in EQUIPMENT_THRESHOLDS:
        assert readings[name]["status"] == "warning"
        assert counters[name] == 1


def test_equipment_third_breach_is_critical():
    counters = {name: 2 for name in EQUIPMENT_THRESHOLDS}
    high_temps = [t + 10 for t in EQUIPMENT_THRESHOLDS.values()]
    readings = update_equipment_counters(high_temps, counters)
    for name in EQUIPMENT_THRESHOLDS:
        assert readings[name]["status"] == "critical"
        assert counters[name] == 3


def test_equipment_counter_resets_on_normal_reading():
    counters = {name: 5 for name in EQUIPMENT_THRESHOLDS}
    readings = update_equipment_counters([30.0, 30.0], counters)
    for name in EQUIPMENT_THRESHOLDS:
        assert counters[name] == 0
        assert readings[name]["status"] == "normal"


def test_equipment_partial_zone_temps():
    # Fewer zone_temps than equipment entries — extras are skipped, not errored
    counters = fresh_counters()
    readings = update_equipment_counters([50.0], counters)
    names = list(EQUIPMENT_THRESHOLDS.keys())
    assert names[0] in readings
    assert names[1] not in readings


def test_equipment_readings_include_temp_and_threshold():
    counters = fresh_counters()
    high_temps = [t + 5 for t in EQUIPMENT_THRESHOLDS.values()]
    readings = update_equipment_counters(high_temps, counters)
    for name, threshold in EQUIPMENT_THRESHOLDS.items():
        assert readings[name]["threshold"] == threshold
        assert readings[name]["temp"] == round(threshold + 5, 2)
