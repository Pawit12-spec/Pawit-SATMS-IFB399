import sys
import os
import pytest
from unittest.mock import patch
from datetime import datetime

# Path setup: Since this file is in Alert_System/tests/, 
# the parent directory is Alert_System/
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

from sensor_system import Sensor, thermal_cam
from thermal_model import model

# ---------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------

@pytest.fixture
def timestamp():
    return datetime.now()

@pytest.fixture
def standard_sensor():
    return Sensor("Test_Basic_Sensor", min_threshold=10, max_threshold=50)

@pytest.fixture
def thermal():
    # Passed None to explicitly show this sensor doesn't use a minimum threshold
    return thermal_cam("Test_Thermal_Cam", min_threshold=None, max_threshold=100)


# --------------------------------------------------------
# 1. BASE SENSOR TESTS (Values & Alert Triggers)
# ---------------------------------------------------------

def test_valid_input_number(standard_sensor):
    # Ensure standard numbers are accepted
    assert standard_sensor.validate_input(25) is True
    assert standard_sensor.validate_input(45.5) is True
    assert standard_sensor.validate_input("string_value") is False

@patch('sensor_system.alert.LowPriorityAlert')
@patch('sensor_system.alert.MediumPriorityAlert')
@patch('sensor_system.alert.HighPriorityAlert')
def test_min_threshold_escalation(mock_high, mock_medium, mock_low, standard_sensor, timestamp):
    # Strike 1: Should trigger Low Priority Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_low.call_count == 1
    assert standard_sensor.fault_counters["minimum breach"] == 1

    # Strike 2: Should trigger Medium Priority Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_medium.call_count == 1
    assert standard_sensor.fault_counters["minimum breach"] == 2

    # Strike 3: Should trigger High Priority Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_high.call_count == 1
    assert standard_sensor.fault_counters["minimum breach"] == 3

def test_fault_list_retrieval(standard_sensor, timestamp):
    # Trigger a fault
    standard_sensor.check_reading(60, timestamp) # Max breach
    
    # Retrieve and verify the fault list tracking
    active_tracked_faults = {k: v for k, v in standard_sensor.fault_counters.items() if v > 0}
    assert "maximum breach" in active_tracked_faults
    assert active_tracked_faults["maximum breach"] == 1


# --------------------------------------------------------
# 2. BASE SENSOR TESTS (Alert Reset Logic)
# ---------------------------------------------------------

@patch('sensor_system.alert.LowPriorityAlert')
@patch('sensor_system.alert.MediumPriorityAlert')
@patch('sensor_system.alert.HighPriorityAlert')
def test_escalation_reset_and_full_escalation(mock_high, mock_medium, mock_low, standard_sensor, timestamp):
    """
    Tests the sequence: Low -> Medium -> Normal (Reset) -> Low -> Medium -> High
    """
    # 1. Strike 1: Low Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_low.call_count == 1

    # 2. Strike 2: Medium Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_medium.call_count == 1

    # 3. Normal Reading: Counters should reset to 0!
    standard_sensor.check_reading(25, timestamp) 
    assert standard_sensor.fault_counters["minimum breach"] == 0
    
    # Ensure no new alerts fired during the normal reading
    assert mock_low.call_count == 1 
    assert mock_medium.call_count == 1

    # 4. Strike 1 (Again): Low Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_low.call_count == 2

    # 5. Strike 2 (Again): Medium Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_medium.call_count == 2

    # 6. Strike 3: High Alert
    standard_sensor.check_reading(5, timestamp)
    assert mock_high.call_count == 1


# ---------------------------------------------------------
# 3. THERMAL CAMERA TESTS (Image Validation)
# ---------------------------------------------------------

def test_thermal_cam_invalid_inputs(thermal):
    # Test 1: Null / Wrong Data Type (Integer instead of String)
    assert thermal.validate_input(123) is False
    
    # Test 2: Invalid File Extension
    assert thermal.validate_input("test_image.jpg") is False
    assert thermal.validate_input("test_image.txt") is False
    
    # Test 3: Valid Extension, but file does not actually exist on the system
    assert thermal.validate_input("does_not_exist.png") is False


# ---------------------------------------------------------
# 4. THERMAL CAMERA TESTS (Instant Alert Logic)
# ---------------------------------------------------------

@patch('sensor_system.alert.LowPriorityAlert')
@patch('sensor_system.alert.MediumPriorityAlert')
@patch('sensor_system.alert.HighPriorityAlert')
@patch('sensor_system.model.analyse_image') 
def test_thermal_normal_reading(mock_analyse, mock_high, mock_medium, mock_low, thermal, timestamp):
    
    # Force the model to return exactly what a "normal" result looks like
    mock_analyse.return_value = {'status': 'normal', 'reconstruction_error': 0.01}
    
    normal_img_path = "Alert_System/Alert System tests/test_images/normal/normal_3.png" 
    
    if normal_img_path:
        thermal.check_reading(normal_img_path, timestamp)
        
        # Normal reading should not trigger any alerts
        assert mock_low.call_count == 0
        assert mock_medium.call_count == 0
        assert mock_high.call_count == 0
    else:
        pytest.skip("No image path provided for normal reading test.")


@patch('sensor_system.alert.LowPriorityAlert')
@patch('sensor_system.alert.MediumPriorityAlert')
@patch('sensor_system.alert.HighPriorityAlert')
@patch('sensor_system.model.analyse_image') 
@patch('os.path.exists')
def test_thermal_hotspot_instant_alert(mock_exists,mock_analyse, mock_high, mock_medium, mock_low, thermal, timestamp):
    mock_exists.return_value = True
    # Force the model to return a hotspot fault
    mock_analyse.return_value = {'status': 'anomaly', 'type': 'hotspot'}
    #will change once new model is input, strictly for testing purposes right now
    hotspot_img_path = "fake_github_path/hotspot_1.png"
    
    if hotspot_img_path:
        # First reading should immediately trigger High Priority Alert
        thermal.check_reading(hotspot_img_path, timestamp)
        
        # Ensure ONLY High Priority fired
        assert mock_low.call_count == 0
        assert mock_medium.call_count == 0
        assert mock_high.call_count == 1
        
        # Ensure it bypassed the counter logic
        assert thermal.fault_counters["hotspot"] == 0
    else:
        pytest.skip("No image path provided for hotspot test.")