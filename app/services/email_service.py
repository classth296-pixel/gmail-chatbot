# ================================================
# app/services/email_service.py
# Sends email using the same combined OAuth token used for reading mail
# (token already includes gmail.send scope — no separate flow needed).
# ================================================

import base64
from email.mime.text import MIMEText

from app.ingestion.gmail_client import build_service_from_token


def send_email(token_file: str, to: str, subject: str, body: str) -> dict:
    """Sends a plain-text email via the authenticated Gmail account."""
    service = build_service_from_token(token_file)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

    return service.users().messages().send(
        userId="me",
        body={"raw": raw_message},
    ).execute()


def extract_subject_and_body(drafted_text: str, default_subject: str = "Message from your email assistant"):
    """Pulls a 'Subject: ...' line out of a model-drafted email; the rest is the body."""
    subject = default_subject
    body_lines = []
    subject_set = False

    for line in drafted_text.splitlines():
        if not subject_set and line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            subject_set = True
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return subject, body
