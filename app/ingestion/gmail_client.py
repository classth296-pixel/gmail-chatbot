# ================================================
# app/ingestion/gmail_client.py
# OAuth flow + Gmail fetching. Uses the History API for cheap incremental
# sync once an initial full fetch has happened, and falls back to a full
# INBOX list on first run or if the history id has expired.
# ================================================

import os
import re
import json
import base64
from html import unescape
from datetime import datetime
from email.utils import parsedate_to_datetime

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings

SCOPES = settings.GMAIL_SCOPES


def get_user_paths(user_id: str) -> dict:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", user_id)
    base = os.path.join(settings.USERS_DIR, safe_id)
    os.makedirs(base, exist_ok=True)
    return {
        "base_dir": base,
        "credentials_file": os.path.join(base, "credentials.json"),
        "token_file": os.path.join(base, "token.json"),
        "state_file": os.path.join(base, "fetch_state.json"),
        "csv_file": os.path.join(base, "emails.csv"),
        "chroma_dir": os.path.join(base, "chroma_db"),
        "checkpoint_db": os.path.join(base, "checkpoints.sqlite"),
    }


# ---------------- OAuth ----------------

def build_service_from_token(token_file: str, scopes=SCOPES):
    if not os.path.exists(token_file):
        raise RuntimeError("Not authorized yet — connect Gmail first.")

    creds = Credentials.from_authorized_user_file(token_file, scopes)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, "w") as token:
                token.write(creds.to_json())
        else:
            raise RuntimeError("Authorization expired — reconnect Gmail.")

    return build("gmail", "v1", credentials=creds)


def build_auth_url(credentials_file: str, redirect_uri: str, scopes=SCOPES) -> tuple:
    flow = Flow.from_client_secrets_file(credentials_file, scopes=scopes, redirect_uri=redirect_uri)
    flow.autogenerate_code_verifier = False
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url, state


def complete_auth(credentials_file: str, redirect_uri: str, code: str, token_file: str, scopes=SCOPES):
    flow = Flow.from_client_secrets_file(credentials_file, scopes=scopes, redirect_uri=redirect_uri)
    flow.autogenerate_code_verifier = False
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open(token_file, "w") as token:
        token.write(creds.to_json())
    return creds


# ---------------- Parsing helpers ----------------

def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<(br|/p|/div|/tr|/li)[^>]*>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract_body(payload):
    plain_text, html_text = None, None

    def walk(part):
        nonlocal plain_text, html_text
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain" and "data" in part.get("body", {}):
            plain_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
        elif mime_type == "text/html" and "data" in part.get("body", {}):
            html_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
        for sub_part in part.get("parts", []):
            walk(sub_part)

    walk(payload)

    if plain_text and plain_text.strip():
        return plain_text
    if html_text and html_text.strip():
        return html_to_text(html_text)
    return ""


def _row_from_message(msg: dict) -> dict:
    headers = msg["payload"]["headers"]
    date_header = get_header(headers, "Date")
    body = extract_body(msg["payload"])
    body = " ".join(body.split())

    try:
        dt = parsedate_to_datetime(date_header)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
    except Exception:
        date_str, time_str = date_header, ""

    return {
        "message_id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "date": date_str,
        "receive_time": time_str,
        "sender": get_header(headers, "From"),
        "recipient": get_header(headers, "To"),
        "subject": get_header(headers, "Subject"),
        "snippet": msg.get("snippet", ""),
        "message": body,
    }


# ---------------- State ----------------

def load_state(state_file):
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            return json.load(f)
    return {"history_id": None, "last_message_id": None}


def save_state(state_file, state):
    with open(state_file, "w") as f:
        json.dump(state, f)


# ---------------- Fetching ----------------

def _full_fetch(service, max_results):
    """First-ever sync: pull the most recent N inbox messages."""
    results = service.users().messages().list(
        userId="me", maxResults=max_results, labelIds=["INBOX"]
    ).execute()
    message_ids = [m["id"] for m in results.get("messages", [])]

    rows = []
    for mid in message_ids:
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        rows.append(_row_from_message(msg))

    profile = service.users().getProfile(userId="me").execute()
    return rows, profile.get("historyId")


def _incremental_fetch(service, history_id):
    """Cheap sync using the Gmail History API — only pulls what changed."""
    rows = []
    page_token = None
    new_history_id = history_id

    while True:
        try:
            resp = service.users().history().list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                # historyId too old / expired — caller falls back to a full fetch.
                raise
            raise  # any other Gmail API error should surface, not be swallowed as "just re-sync everything"

        new_history_id = resp.get("historyId", new_history_id)
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                mid = added["message"]["id"]
                msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
                rows.append(_row_from_message(msg))

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return rows, new_history_id


def fetch_new_emails(service, state: dict, max_results=100):
    """
    Returns (rows, new_state). Prefers History API incremental sync; falls
    back to a full fetch on the very first run or if the stored historyId
    has expired on Google's side.
    """
    history_id = state.get("history_id")

    if history_id:
        try:
            rows, new_history_id = _incremental_fetch(service, history_id)
            return rows, {"history_id": new_history_id}
        except HttpError as e:
            if e.resp.status != 404:
                raise  # real failure (auth/network/etc) — don't mask it as "start over"
            # historyId expired — fall through to full fetch below

    rows, new_history_id = _full_fetch(service, max_results)
    return rows, {"history_id": new_history_id}


def load_existing_message_ids(csv_file: str) -> set:
    """Message IDs already synced, so a fallback full-fetch (e.g. after an
    expired historyId) never re-embeds emails that are already indexed."""
    if not os.path.exists(csv_file):
        return set()
    import csv
    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        return {row["message_id"] for row in csv.DictReader(f) if row.get("message_id")}


CSV_FIELDS = ["message_id", "thread_id", "date", "receive_time", "sender", "recipient", "subject", "snippet", "message"]


def append_to_csv(rows, path):
    import csv
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)
