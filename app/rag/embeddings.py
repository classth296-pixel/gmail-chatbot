# ================================================
# app/rag/embeddings.py
# Thin wrapper around GoogleGenerativeAIEmbeddings with retry/backoff for
# transient errors (rate limits, disconnects) — shared by ingestion & RAG.
# ================================================

import time
import logging

from langchain_google_genai import GoogleGenerativeAIEmbeddings

from app.config import settings

logger = logging.getLogger("embeddings")

_TRANSIENT_MARKERS = ["RemoteProtocolError", "Server disconnected", "503", "timeout", "ConnectionError"]
_DAILY_QUOTA_MARKER = "PerDay"  # e.g. 'EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier'


def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    if not settings.GOOGLE_API_KEY:
        raise ValueError(
            "GOOGLE_API_KEY not set. Add it to your .env file in the project root."
        )
    return GoogleGenerativeAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        google_api_key=settings.GOOGLE_API_KEY,
    )


def invoke_with_retry(fn, *args, max_retries=3, base_delay=3, **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if _DAILY_QUOTA_MARKER in msg:
                # Daily quota exhausted — retrying within seconds/minutes cannot help.
                # Fail fast instead of burning the retry budget on a no-op wait.
                logger.error("Daily embedding quota exhausted — will not reset for hours: %s", msg[:200])
                raise
            transient = "RESOURCE_EXHAUSTED" in msg or any(marker in msg for marker in _TRANSIENT_MARKERS)
            if transient and attempt < max_retries:
                wait = base_delay * attempt
                logger.warning("Transient error (attempt %s/%s): %s — retrying in %ss", attempt, max_retries, msg[:120], wait)
                time.sleep(wait)
            else:
                raise
