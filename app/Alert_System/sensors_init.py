import sys
import os


current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

image_path1 = os.path.join(current_dir, "Alert System tests", "irregular_hotspot", "hotspot_1.png")
image_path2 = os.path.join(current_dir, "Alert System tests", "irregular_hotspot", "hotspot_2.png")
image_path3 = os.path.join(current_dir, "Alert System tests", "irregular_hotspot", "hotspot_3.png")
# -----------------------------------------------------------

from Alert_System import sensor_system

#initialize sensors with their respective thresholds
""" 
ML logic is yet to be addded for CO2, humidity, air quality sensors and thermal equipment, 
so thresholds are currently placeholders and may need to be adjusted after ML model development and testing

"""
battery = sensor_system.thermal_equipment("Battery", min_threshold= 0, max_threshold= 41.5, )
switchboard = sensor_system.thermal_equipment("Switchboard", min_threshold= 0, max_threshold=41)
cO2_Sensor = sensor_system.CO2_Sensor("Server Room CO2",min_threshold= 100, max_threshold= 150)
humidity  = sensor_system.humidity_sensor("Server Room Humidity", min_threshold= 52.5, max_threshold= 57.5)
air_quality = sensor_system.Air_Quality_Sensor("Server Room Air Quality", min_threshold= 2, max_threshold= 5)

#thermal camera has no thresholds, it will rely on ML model for fault detection, so we can set dummy thresholds or ignore them in the class
thermal_cam = sensor_system.thermal_cam("Equipment_Room", min_threshold= 0, max_threshold= 0) # timestamp can be set to None or current time, as it may not be relevant for the thermal camera's ML-based analysis



def process_sensors(battery_reading, switchboard_reading, co2_reading, humidity_reading, air_quality_reading, thermal_image, timestamp):
    """
    This function takes in the latest readings from all sensors, checks them against their thresholds, and triggers alerts if necessary.
    
    """
    battery.check_reading(battery_reading, timestamp)
    switchboard.check_reading(switchboard_reading, timestamp)
    cO2_Sensor.check_reading(co2_reading, timestamp)
    humidity.check_reading(humidity_reading, timestamp)
    air_quality.check_reading(air_quality_reading, timestamp)
    thermal_cam.check_reading(thermal_image, timestamp)
        


process_sensors(battery_reading= 42, switchboard_reading= 40, co2_reading= 120, humidity_reading= 55, air_quality_reading= 3, thermal_image= image_path1, timestamp = "2024-06-01 12:00:00")
process_sensors(battery_reading= 42, switchboard_reading= 40, co2_reading= 120, humidity_reading= 55, air_quality_reading= 3, thermal_image= image_path2, timestamp = "2024-06-01 12:00:00")
process_sensors(battery_reading= 42, switchboard_reading= 40, co2_reading= 120, humidity_reading= 55, air_quality_reading= 3, thermal_image= image_path3, timestamp = "2024-06-01 12:00:00")
