import os
import resend
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()
resend.api_key = os.getenv("RESEND_API_KEY")

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(account_sid, auth_token)

class Alert:
    """
    Parent alert class for all alert types. Contains common attributes and methods for logging and triggering alerts.
    
    
    """
    def __init__(self, message, source, timestamp):
        self.message = message
        self.source = source
        self.timestamp = timestamp
        
    def log_alert(self, level):
        # add database alert logic here, do when uni starts, contact Lee
        print(f"{self.source} information logged to database, level = {level}", "timestamp = ", self.timestamp)
        
    def trigger(self):
        # This method will be overridden by child classes to implement specific alert triggering logic (e.g., sending emails, logging to database, etc.)
        pass
        
    
    
class LowPriorityAlert(Alert):
    def trigger(self):
        # add logic for low priority alert, consult EQL and team for UI warning implementation
        self.log_alert("low")
        
        
        
class MediumPriorityAlert(Alert):
    def trigger(self):
        # add logic for medium priority alert, consult EQL and team for UI warning implementation
        self.log_alert("medium")
        
        #add warning for UI logic below, consult EQL and team
        

class HighPriorityAlert(Alert):
    
    " Recipient logic needs to be changed to array of reciepients based off relevancy to the alert "
    def send_email(self, message, recipient):
        to_email = recipient or os.getenv("ALERT_TO_EMAIL")
        from_email = os.getenv("RESEND_FROM_EMAIL")
        template_id = os.getenv("RESEND_TEMPLATE_ID")
        params = {
            "from": from_email,
            "to": [to_email],
            "subject": "Alert: Immediate Attention Required!",
            "template": {
                "id": template_id,
                "variables": {
                    "MESSAGE": message,
                }
            }
    }


        email = resend.Emails.send(params)
        
    def send_sms(self, message_body, recipient):
        #need to add recipient logic here, consult EQL and team for details
        to_phone = recipient or os.getenv("ALERT_TO_PHONE")
        from_phone = os.getenv("TWILIO_FROM_PHONE")
        
        recipient_formatted = f"+{to_phone}"
        message_str = str(message_body)
        
        print(f"SMS sent to {recipient_formatted} with message: {message_str}")
        
        message = client.messages.create(
            body=message_str,
            from_=from_phone, 
            to=recipient_formatted    
        )
        
        return message.sid
            
    
    def trigger(self):
        self.log_alert("high")
        self.send_email(self.message,recipient=None) # add recipient logic here, consult EQL and team for details)
        self.send_sms(self.message, recipient=None) # add recipient logic here, consult EQL and team for details)
        
    
    
