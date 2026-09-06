"""
mailer.py
Minimal SMTP email sender used for account email-verification.

Configure via the SETTINGS section below.
"""

import os
import smtplib
from email.mime.text import MIMEText


# ============================================================
# GMAIL SETTINGS — EDIT THESE TWO VALUES
# ============================================================

GMAIL_ADDRESS = "vanshdoshi75@gmail.com"
GMAIL_APP_PASSWORD = "uxhd uzvr raks emlk"


# ============================================================
# SMTP CONFIGURATION
# ============================================================

# Put the Gmail settings into environment variables so the
# rest of the original code continues to work unchanged.
os.environ["MAIL_SERVER"] = "smtp.gmail.com"
os.environ["MAIL_PORT"] = "587"
os.environ["MAIL_USERNAME"] = GMAIL_ADDRESS
os.environ["MAIL_PASSWORD"] = GMAIL_APP_PASSWORD
os.environ["MAIL_USE_TLS"] = "1"
os.environ["MAIL_SENDER"] = GMAIL_ADDRESS


# ============================================================
# ORIGINAL CODE
# ============================================================

def _mail_configured():
    return all([
        os.environ.get("MAIL_SERVER"),
        os.environ.get("MAIL_USERNAME"),
        os.environ.get("MAIL_PASSWORD"),
    ])


def send_email(to_address, subject, body):
    """Send a plain-text email. Returns True if actually sent over SMTP,
    False if it fell back to the console (dev mode)."""
    if not _mail_configured():
        print("=" * 70)
        print("[DEV MODE] MAIL_SERVER/MAIL_USERNAME/MAIL_PASSWORD not set —")
        print("email was NOT sent. Printing it here instead so you can test:")
        print(f"To:      {to_address}")
        print(f"Subject: {subject}")
        print("-" * 70)
        print(body)
        print("=" * 70)
        return False

    host = os.environ["MAIL_SERVER"]
    port = int(os.environ.get("MAIL_PORT", "587"))
    username = os.environ["MAIL_USERNAME"]
    password = os.environ["MAIL_PASSWORD"]
    use_tls = os.environ.get("MAIL_USE_TLS", "1") != "0"
    sender = os.environ.get("MAIL_SENDER", username)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_address

    with smtplib.SMTP(host, port, timeout=10) as server:
        if use_tls:
            server.starttls()
        server.login(username, password)
        server.sendmail(sender, [to_address], msg.as_string())

    return True