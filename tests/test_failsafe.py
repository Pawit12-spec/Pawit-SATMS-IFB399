import pytest
import sys
import os
from unittest.mock import patch

# Setup paths so Python can find the app folder
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# Import state dictionaries and checking function from routes
from app.routes import pi_last_seen, pi_alert_state, monitor_offline_devices

# Clear dictionaries before every test to prevent overlap
@pytest.fixture(autouse=True)
def reset_state():
    pi_last_seen.clear()
    pi_alert_state.clear()

@patch("app.Alert_System.alert.HighPriorityAlert.trigger")
def test_device_online_no_alert(mock_trigger):
    """Verifies that a device seen recently (under 20s) triggers nothing."""
    pi_last_seen["pi_1"] = 100.0
    
    # Run check at time 110 (10s offline)
    monitor_offline_devices(110.0)
    
    mock_trigger.assert_not_called()

@patch("app.Alert_System.alert.HighPriorityAlert.trigger")
def test_instant_alert_fires(mock_trigger):
    """Verifies the first alert fires after 20 seconds of downtime."""
    pi_last_seen["pi_1"] = 100.0
    
    # Run check at time 130 (30s offline)
    monitor_offline_devices(130.0)
    
    mock_trigger.assert_called_once()
    assert pi_alert_state["pi_1"]["instant"] is True
    assert pi_alert_state["pi_1"]["is_offline"] is True

@patch("app.Alert_System.alert.HighPriorityAlert.trigger")
def test_10m_milestone_alert_fires(mock_trigger):
    """Verifies the 10-minute escalation alert fires properly."""
    pi_last_seen["pi_1"] = 100.0
    
    # Simulate device that already sent the instant alert
    pi_alert_state["pi_1"] = {
        "instant": True, "10m": False, "1h": False, 
        "last_daily": None, "is_offline": True
    }
    
    # Run check at time 710 (610s offline)
    monitor_offline_devices(710.0)
    
    mock_trigger.assert_called_once()
    assert pi_alert_state["pi_1"]["10m"] is True

@patch("app.Alert_System.alert.HighPriorityAlert.trigger")
def test_1h_milestone_alert_fires(mock_trigger):
    """Verifies the 1-hour escalation alert fires properly."""
    pi_last_seen["pi_1"] = 100.0
    
    # Simulate device that already sent instant and 10m alerts
    pi_alert_state["pi_1"] = {
        "instant": True, "10m": True, "1h": False, 
        "last_daily": None, "is_offline": True
    }
    
    # Run check at time 3800 (3700s offline)
    monitor_offline_devices(3800.0)
    
    mock_trigger.assert_called_once()
    assert pi_alert_state["pi_1"]["1h"] is True

@patch("app.Alert_System.alert.HighPriorityAlert.trigger")
def test_recovery_alert_fires_and_resets(mock_trigger):
    """Verifies that an offline device coming back online sends a recovery email and resets."""
    pi_last_seen["pi_1"] = 100.0
    
    # Simulate device that was previously marked offline
    pi_alert_state["pi_1"] = {
        "instant": True, "10m": False, "1h": False, 
        "last_daily": None, "is_offline": True
    }
    
    # Simulate the Pi checking in (updating last_seen to recent time)
    pi_last_seen["pi_1"] = 150.0
    
    # Run check at time 155 (only 5s offline)
    monitor_offline_devices(155.0)
    
    # The trigger called here is the Recovery notification
    mock_trigger.assert_called_once()
    
    # Verify the state dictionary was completely wiped clean for the next event
    assert pi_alert_state["pi_1"]["is_offline"] is False
    assert pi_alert_state["pi_1"]["instant"] is False