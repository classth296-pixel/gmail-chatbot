# ================================================
# app/api/schemas.py
# ================================================

from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"


class ChatResponse(BaseModel):
    type: str  # "text" | "pending_send"
    content: Optional[str] = None
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


class SendEmailRequest(BaseModel):
    recipient: str
    subject: str
    body: str


class OAuthCallbackParams(BaseModel):
    code: str


class SyncResponse(BaseModel):
    status: str
    new_emails: int
    message: str


class StatusResponse(BaseModel):
    credentials_uploaded: bool
    gmail_connected: bool
    chat_ready: bool
    redirect_uri: str
