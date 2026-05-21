import alert
from collections import defaultdict
from thermal_model import model
import os
class Sensor:
    def __init__(self, name, min_threshold, max_threshold):
        """
        
        Parent class for all sensors. Contains common attributes and methods for validating input, checking readings against thresholds, and triggering alerts.
        
        """
        self.name = name
        
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.measurement_name = "Units" # default measurement name, can be overridden by child classes
        
        # Tracks HOW MANY times a fault occurred (prevents KeyError)
        self.fault_counters = defaultdict(int) 
        
        # Tracks METADATA (like ML confidence) for the current reading
        self.fault_details = {}
    def validate_input(self, value):
        """
        Parent logic: Sensors should output numbers (int or float).
        """
        # We allow floats too, because sensors often read like 40.5
        if not isinstance(value, (int, float)):
            print(f"[SYSTEM ERROR] {self.name}: Invalid data type. Expected number, got {type(value).__name__}")
            # Later, trigger your Data Format Alert here
            return False
        return True
        
        
    def check_reading(self, value, timestamp):
        self.value = value
        """
        Logic 
            1. Gather all faults/detections, threshold or ML detections
            2. process active faults, instant and counter-based alerts
            3. reset inactive fault counters
            4. repeat
            
            Detection Logic
            1. check standard faults
            2. have custom based fault detecter function for child class
            3. check short/long term anomalies for sensor reading values"""
        
        if not self.validate_input(value):
            return
          
        self.timestamp = timestamp
        self.fault_details.clear() # Clear previous fault details for the new reading
        
        active_faults = self.detect_standard_faults(value)
       
        active_faults.extend(self.detect_custom_faults(value))
       
        for fault_type in active_faults:
            if self.is_instant_alert(fault_type):
                self.trigger_instant_alert(value,fault_type)
            else:
                self.fault_counters[fault_type] += 1
                self.escalate_counter_alert(fault_type)
                    
        for known_fault in list(self.fault_counters.keys()):
            if known_fault not in active_faults:
                self.fault_counters[known_fault] = 0
       
    def detect_standard_faults(self, value):
        """
        Standard fault detection logic for threshold breaches and anomalies.
        
        """
        faults = []
        if isinstance(value, (int, float)):
            if value < self.min_threshold:
                faults.append("minimum breach")
            if value > self.max_threshold:
                faults.append("maximum breach")
                
            if self.check_short_term_anomalies(value):
                faults.append("short term anomaly")
            if self.check_long_term_anomalies(value):
                faults.append("long term anomaly")
        return faults
    
    def detect_custom_faults(self, value):
        # add logic for custom fault detection for different sensor types, consult EQL and team
        return []
    
    def check_short_term_anomalies(self, value):
        # add logic to check for short term anomalies, consult EQL and team
        return False
    def check_long_term_anomalies(self, value):
        # add logic to check for long term anomalies, consult EQL and team
        return False
    
    def is_instant_alert(self, fault_type):
        """ 
        Define which faults should trigger instant alerts vs counter-based alerts.
        """
        instant_faults = ["short term anomaly", "long term anomaly"]
        return fault_type in instant_faults
    
    def trigger_instant_alert(self, value, fault_type):
        """
        Define the logic to trigger instant alerts based on the fault type.
        """
        if fault_type == "short term anomaly":
            Alert = alert.MediumPriorityAlert(f"Short term anomaly detected on {self.name},", self.name, self.timestamp)
            Alert.trigger()
        elif fault_type == "long term anomaly":
            Alert = alert.HighPriorityAlert(f"Long term anomaly detected on {self.name}", self.name, self.timestamp)
            Alert.trigger()
            
            
    def escalate_counter_alert(self, fault_type):    
        """
        Define the logic to escalate alerts based on the count of consecutive faults.
        """
        count = self.fault_counters[fault_type]
        if count == 1:
            Alert = alert.LowPriorityAlert(f"Low Alert", self.name, self.timestamp)
            
        elif count == 2:
            Alert = alert.MediumPriorityAlert(f"{self.name} Sensor Alert: {count} consecutive readings out of {fault_type} threshold. \n Last reading was: {self.value} {self.measurement_name} \n Timestamp: {self.timestamp}", self.name, self.timestamp)
            
        elif count >= 3:
            Alert = alert.HighPriorityAlert(f"{self.name} Sensor Alert: {count} consecutive readings out of {fault_type} threshold. \n Last reading was: {self.value} {self.measurement_name}. \n Immediate attention required! Timestamp: {self.timestamp}", self.name, self.timestamp)
        
        Alert.trigger()
            
    
    
class thermal_cam(Sensor):
    def __init__(self, name, min_threshold, max_threshold):
        super().__init__(name, min_threshold, max_threshold)

    def validate_input(self, value):
        """
        Child logic: Thermal camera needs a string (file path) to a 32x48 PNG.
        """
        # 1. Check if it's actually a string (file path)
        if not isinstance(value, str):
            #add trigger for invalid data format alert here, consult EQL and team
            print(f"[SYSTEM ERROR] {self.name}: Expected a file path string, got {type(value).__name__}")
            return False
        
        # 2. Check if it's a PNG
        if not value.lower().endswith('.png'):
            #add trigger for invalid file type alert here, consult EQL and team
            print(f"[SYSTEM ERROR] {self.name}: Invalid file type. Must be a .png file.")
            return False
        
        # 3. Check if the file exists
        if not os.path.exists(value):
            #add trigger for file not found alert here, consult EQL and team
            print(f"[SYSTEM ERROR] {self.name}: File not found at path: {value}")
            return False
        
        # if all checks pass, return True
        return True
        
    def detect_custom_faults(self, img):
        
        custom_faults = []
        
        # Check if the image contains a hotspot
        result = model.analyse_image(img)
        
        if result.get("is_anomaly"):
            custom_faults.append("hotspot")
            
            # If it IS a hotspot, run Grad-CAM to find exactly where it is
            circled_img_path, x, y = model.localize_and_draw(img)
            
            # Store everything for the alert payload
            self.fault_details["hotspot"] = {
                "confidence": result.get("confidence"),
                "coordinates": f"X:{x}, Y:{y}",
                "annotated_image": circled_img_path
            }
        return custom_faults
    
    def is_instant_alert(self, fault_type):
        """ Override parent: Make thermal detections instant. """
        thermal_instant_faults = [ "hotspot"]
        if fault_type in thermal_instant_faults:
            return True
            
        # Fall back to parent logic for anything else (like short/long term anomalies)
        return super().is_instant_alert(fault_type)

    def trigger_instant_alert(self, value, fault_type):
        """ Override parent: Trigger the correct High Priority alerts immediately. """
        
        if fault_type == "hotspot":
            #retrieve confidence score from fault_counters for potential use in alert message or escalation logic
            details = self.fault_details.get("hotspot", {})
            confidence = details.get("confidence", "unknown") # default to "unknown" if confidence score is not available
            #can possibly add image attachment to email here
            Alert = alert.HighPriorityAlert(f"Irregular hotspot detected in thermal scan with {confidence}% confidence. Please ensure equipment in the image is behaving correctly. \n Timestamp: {self.timestamp}", self.name, self.timestamp)
            Alert.trigger()
        else:
            # Fall back to parent logic for anything else
            super().trigger_instant_alert(value, fault_type)
        
        

class humidity_sensor(Sensor):
    def __init__(self, name, min_threshold, max_threshold):
        super().__init__(name, min_threshold, max_threshold)
        self.measurement_name = "% RH"      
        
    def detect_custom_faults(self, value):
        # TODO: add ML logic for humidity sensor specific fault detection
        return []
    
class CO2_Sensor(Sensor):
    def __init__(self, name, min_threshold, max_threshold):
        super().__init__(name, min_threshold, max_threshold)
        self.measurement_name = "ppm"     
        
    def detect_custom_faults(self, value):
        #TODO: add ML logic for CO2 sensor specific fault detection
        return []
    
class Air_Quality_Sensor(Sensor):
    def __init__(self, name, min_threshold, max_threshold):
        super().__init__(name, min_threshold, max_threshold)
        self.measurement_name = "AQI"        

class thermal_equipment(Sensor):
    def __init__(self, name, min_threshold, max_threshold):
        super().__init__(name, min_threshold, max_threshold)
        self.measurement_name = "Degrees"
        
    
    def detect_custom_faults(self, value):
        #TODO: add ML logic for thermal equipment specific fault detection
        return []
    
    
        






            
            
    
        
        