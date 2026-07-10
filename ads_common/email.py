"""Alert email via Resend — escalation path for missed Discord notifications.

Added after an incident where CRITICAL alerts sat unnoticed in Discord for
3 weeks. Discord is the notification channel; email is the escalation path
that reliably reaches the operator's inbox.
"""

import json
import logging
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.parent
load_dotenv(SCRIPT_DIR / ".env")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")


def send_alert_email(subject: str, body: str) -> bool:
    """Send one alert email. Returns False on missing config or failure
    (never raises — monitoring must not stop because email failed)."""
    if not (RESEND_API_KEY and ALERT_EMAIL_FROM and ALERT_EMAIL_TO):
        logger.warning(
            "Email not configured; skipping (set RESEND_API_KEY / "
            "ALERT_EMAIL_FROM / ALERT_EMAIL_TO in .env)"
        )
        return False
    payload = json.dumps({
        "from": ALERT_EMAIL_FROM,
        "to": ALERT_EMAIL_TO,
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "adgrants-monitor-alert/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        logger.info("Alert email sent: %s", subject)
        return True
    except Exception as e:  # noqa: BLE001 — email failure must not stop monitoring
        logger.warning("Alert email failed: %s", e)
        return False
