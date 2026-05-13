"""
core/emailer.py — SendGrid email helper

If SENDGRID_API_KEY or REPORT_EMAIL is not configured, all functions
log a short warning and return False. No agent ever crashes due to
missing email config.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def send_alert(subject: str, body: str) -> bool:
    """Send a plain-text error alert email. Safe to call even without SendGrid configured."""
    return _send(
        subject=subject,
        body_text=body,
        body_html=f"<pre style='font-family:monospace;white-space:pre-wrap'>{body}</pre>",
    )


def send_report(subject: str, html_body: str) -> bool:
    """Send a formatted HTML weekly report email. Safe to call even without SendGrid configured."""
    return _send(
        subject=subject,
        body_text="Open this email in an HTML-capable client to view the report.",
        body_html=html_body,
    )


def _send(subject: str, body_text: str, body_html: str) -> bool:
    """
    Internal send. Returns True on success, False on any failure or missing config.
    Never raises — all exceptions are caught and logged.
    """
    api_key      = os.getenv("SENDGRID_API_KEY", "").strip()
    report_email = os.getenv("REPORT_EMAIL",      "").strip()

    if not api_key or not report_email:
        print(f"[emailer] SendGrid not configured — skipping: '{subject}'")
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email="noreply@agenticsystems.local",
            to_emails=report_email,
            subject=subject,
            plain_text_content=body_text,
            html_content=body_html,
        )
        client   = SendGridAPIClient(api_key)
        response = client.send(message)
        success  = response.status_code in (200, 202)
        if success:
            print(f"[emailer] Sent '{subject}' → {report_email} ({response.status_code})")
        else:
            print(f"[emailer] Unexpected status {response.status_code} for '{subject}'")
        return success

    except Exception as e:
        print(f"[emailer] Failed to send '{subject}': {e}")
        return False
