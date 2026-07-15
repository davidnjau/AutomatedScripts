#!/usr/bin/env python3
"""
email_service.py
=================
Shared email delivery — every feature that offers "email me the report"
(Bulk Export, DLV Tasks, Morning Briefing, Auto Fetch) sends through here
instead of building its own SMTP boilerplate.

Two entry points, matching the two MIME shapes currently in use:
- `_send_bulk_export_email` — plain-text body + a binary attachment.
- `_send_auto_fetch_email`  — plain-text body with an auto-generated HTML
  alternative, no attachment.

Both raise RuntimeError if SMTP_USER/SMTP_PASS aren't configured, and
propagate any smtplib exception on send failure — callers are expected to
catch and report those the same way they always have.

Run this file directly to smoke-test SMTP connectivity from wherever it's
run (useful for checking from inside the server/container, since that's a
different network path than a local machine):

    python3 email_service.py                    # connect + login only
    python3 email_service.py you@example.com    # also sends a test email
"""

import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders as _email_encoders

from common import SMTP_HOST, SMTP_PASS, SMTP_PORT, SMTP_USER


def _dispatch(msg: MIMEMultipart, to_email: str) -> None:
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_USER / SMTP_PASS not configured in .env")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())


def _send_bulk_export_email(to_email: str, filename: str, xlsx_bytes: bytes) -> None:
    """Send the Excel file as an email attachment. Raises on failure."""
    msg            = MIMEMultipart()
    msg["Subject"] = f"Ardhisasa Export Valuation Report — {filename}"
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email

    body = (
        f"Please find attached the Ardhisasa stamp-duty bulk export.\n\n"
        f"File: {filename}\n"
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(xlsx_bytes)
    _email_encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    _dispatch(msg, to_email)


def _send_auto_fetch_email(to_email: str, subject: str, body: str) -> None:
    """Send a plain-text email with an auto-generated HTML alternative. Raises on failure."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email

    msg.attach(MIMEText(body, "plain"))

    html_body = "<pre style='font-family:monospace'>" + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    msg.attach(MIMEText(html_body, "html"))

    _dispatch(msg, to_email)


def check_smtp_connection() -> None:
    """Connect, STARTTLS, and login to the configured SMTP server without sending
    anything. Raises RuntimeError if creds aren't configured, or whatever smtplib
    raises on connection/login failure. A 15s timeout keeps a blocked outbound
    port from hanging indefinitely instead of failing fast."""
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_USER / SMTP_PASS not configured in .env")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)


if __name__ == "__main__":
    import sys

    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} as {SMTP_USER or '(SMTP_USER not set)'}...")
    try:
        check_smtp_connection()
        print("OK — SMTP connection + login succeeded.")
    except Exception as e:
        print(f"FAIL — {type(e).__name__}: {e}")
        sys.exit(1)

    if len(sys.argv) > 1:
        to_email = sys.argv[1]
        print(f"Sending a test email to {to_email}...")
        try:
            _send_auto_fetch_email(
                to_email, "Ardhisasa Bot — SMTP Test",
                "This is a test email confirming SMTP delivery is working.",
            )
            print("OK — test email sent.")
        except Exception as e:
            print(f"FAIL — {type(e).__name__}: {e}")
            sys.exit(1)
