# ================================================
# app/config.py
# Centralised environment / constants for the whole app.
# ================================================

import os
from dotenv import load_dotenv

load_dotenv(override=True)  # .env always wins over stray shell/session env vars


class Settings:
    # --- Google Gemini ---
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-001")
    CHAT_MODEL: str = os.getenv("CHAT_MODEL", "gemini-3.1-flash-lite")

    # --- OAuth / Gmail ---
    GMAIL_SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]
    # Must exactly match a redirect URI registered in Google Cloud Console.
    # Set this to your real domain (or EC2 public IP/DNS) in production, e.g.
    # "https://your-domain.com/api/oauth/callback"
    REDIRECT_URI: str = os.getenv("REDIRECT_URI", "http://localhost:8000/api/oauth/callback")

    # --- Storage paths ---
    USERS_DIR: str = os.getenv("USERS_DIR", "users")

    # --- App behaviour ---
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "dev-secret-change-me")
    DEFAULT_USER_ID: str = os.getenv("DEFAULT_USER_ID", "manoj")  # fallback single-tenant id
    MAX_FETCH_RESULTS: int = int(os.getenv("MAX_FETCH_RESULTS", "100"))
    EMBED_BATCH_SIZE: int = int(os.getenv("EMBED_BATCH_SIZE", "10"))
    RETRIEVER_K: int = int(os.getenv("RETRIEVER_K", "5"))

    # --- Server ---
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))


settings = Settings()
