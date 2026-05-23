import os
import resend
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token  = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(account_sid, auth_token)


class HighPriorityAlert:
    """High-priority alert dispatched by email (Resend) and SMS (Twilio).

    Construct with message, source, and timestamp. Pass recipient lists at
    construction or per trigger() call; if neither is provided, trigger() falls
    back to the ALERT_TO_EMAIL / ALERT_TO_PHONE env vars.
    """

    def __init__(self, message, source, timestamp, recipients_email=None, recipients_phone=None):
        self.message          = message
        self.source           = source
        self.timestamp        = timestamp
        self.recipients_email = recipients_email or []
        self.recipients_phone = recipients_phone or []

    def print_alert(self):
        print(f"[HIGH ALERT] {self.source} — timestamp: {self.timestamp}")

    def send_email(self, message, recipient):
        from_email = os.getenv("RESEND_FROM_EMAIL")
        params: resend.Emails.SendParams = {
            "from":    from_email,
            "to":      [recipient],
            "subject": "EQL Alert: Immediate Attention Required",
            "html":    f"<p>{message}</p><p><small>Alert dispatched to: {recipient}</small></p>",
        }
        resend.Emails.send(params)

    def send_sms(self, message_body, recipient):
        """Send SMS via Twilio. recipient must be a phone number without a leading +;
        this method prepends it before calling the API."""
        from_phone = os.getenv("TWILIO_FROM_PHONE")
        print(f"SMS sent to +{recipient} with message: {message_body}")
        client.messages.create(
            body=f"{message_body}\nThis is a high-priority EQL alert. Immediate action required.\nDispatched to: emergency contact",
            from_=from_phone,
            to=f"+{recipient}",
        )

    def trigger(self, recipient_email=None, recipient_phone=None):
        """Dispatch the alert. Per-call args override the instance recipient lists;
        if neither is set, falls back to ALERT_TO_EMAIL / ALERT_TO_PHONE env vars."""
        self.print_alert()

        if recipient_email is not None or recipient_phone is not None:
            if recipient_email:
                self.send_email(self.message, recipient_email)
            if recipient_phone:
                self.send_sms(self.message, recipient_phone)
        else:
            email_targets = self.recipients_email or (
                [os.getenv("ALERT_TO_EMAIL")] if os.getenv("ALERT_TO_EMAIL") else []
            )
            phone_targets = self.recipients_phone or (
                [os.getenv("ALERT_TO_PHONE")] if os.getenv("ALERT_TO_PHONE") else []
            )
            for email in email_targets:
                self.send_email(self.message, email)
            for phone in phone_targets:
                self.send_sms(self.message, phone)
