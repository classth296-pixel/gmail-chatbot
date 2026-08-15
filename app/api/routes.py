# ================================================
# app/api/routes.py
# REST + SSE endpoints. Single-tenant by default (settings.DEFAULT_USER_ID)
# via a cookie-based user id, so multiple browsers/EC2 visitors don't share
# one Gmail account by accident, while still needing no login system.
# ================================================

import os
import json
import logging
import uuid
from fastapi import APIRouter, Request, Response, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, RedirectResponse

from app.config import settings
from app.ingestion import gmail_client
from app.ingestion.indexer import add_rows_to_vectorstore
from app.rag import session as rag_session
from app.api.schemas import ChatRequest, SendEmailRequest, SyncResponse, StatusResponse

logger = logging.getLogger("routes")
router = APIRouter(prefix="/api")

USER_COOKIE = "correspondent_uid"


def get_user_id(request: Request, response: Response) -> str:
    """Each new browser gets a random, unique id on first visit — never a
    shared default — so concurrent visitors never collide into the same
    users/<id>/ folder (own Gmail connection, own inbox index, own history)."""
    uid = request.cookies.get(USER_COOKIE)
    if not uid:
        uid = uuid.uuid4().hex[:20]
        response.set_cookie(USER_COOKIE, uid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return uid


# ---------------- Status ----------------

@router.get("/status", response_model=StatusResponse)
def status(request: Request, response: Response):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)
    return StatusResponse(
        credentials_uploaded=os.path.exists(paths["credentials_file"]),
        gmail_connected=os.path.exists(paths["token_file"]),
        chat_ready=rag_session.has_session(user_id),
        redirect_uri=settings.REDIRECT_URI,
    )


# ---------------- OAuth ----------------

@router.post("/oauth/credentials")
async def upload_credentials(request: Request, response: Response, file: UploadFile = File(...)):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)
    contents = await file.read()
    try:
        json.loads(contents)
    except json.JSONDecodeError:
        raise HTTPException(400, "That doesn't look like a valid credentials.json file.")
    with open(paths["credentials_file"], "wb") as f:
        f.write(contents)
    return {"status": "ok"}


@router.get("/oauth/url")
def oauth_url(request: Request, response: Response):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)
    if not os.path.exists(paths["credentials_file"]):
        raise HTTPException(400, "Upload credentials.json first.")
    try:
        auth_url, _ = gmail_client.build_auth_url(paths["credentials_file"], settings.REDIRECT_URI)
    except Exception as e:
        raise HTTPException(500, f"Could not build authorization URL — {e}")
    return {"auth_url": auth_url}


@router.get("/oauth/callback")
def oauth_callback(request: Request, response: Response, code: str = None, error: str = None):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)

    if error:
        return RedirectResponse(url=f"/?oauth_error={error}")
    if not code:
        return RedirectResponse(url="/?oauth_error=missing_code")

    try:
        gmail_client.complete_auth(paths["credentials_file"], settings.REDIRECT_URI, code, paths["token_file"])
    except Exception as e:
        logger.exception("OAuth exchange failed")
        return RedirectResponse(url=f"/?oauth_error={e}")

    return RedirectResponse(url="/?connected=1")


@router.post("/oauth/disconnect")
def disconnect(request: Request, response: Response):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)
    for key in ("credentials_file", "token_file"):
        if os.path.exists(paths[key]):
            os.remove(paths[key])
    return {"status": "ok"}


# ---------------- Sync ----------------

@router.post("/sync", response_model=SyncResponse)
def sync(request: Request, response: Response):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)

    if not os.path.exists(paths["token_file"]):
        raise HTTPException(400, "Connect Gmail first.")

    try:
        service = gmail_client.build_service_from_token(paths["token_file"])
        state = gmail_client.load_state(paths["state_file"])
        rows, new_state = gmail_client.fetch_new_emails(service, state, max_results=settings.MAX_FETCH_RESULTS)

        # Guards against re-embedding emails already indexed — matters when a
        # fallback full-fetch happens (e.g. an expired/stale historyId after
        # reconnecting Gmail), which would otherwise re-pull recent messages
        # that are already in the CSV/vectorstore.
        existing_ids = gmail_client.load_existing_message_ids(paths["csv_file"])
        rows = [r for r in rows if r["message_id"] not in existing_ids]

        if rows:
            from app.rag.embeddings import get_embeddings
            from langchain_community.vectorstores import Chroma

            vectorstore = Chroma(persist_directory=paths["chroma_dir"], embedding_function=get_embeddings())
            add_rows_to_vectorstore(rows, vectorstore, batch_size=settings.EMBED_BATCH_SIZE)
            gmail_client.append_to_csv(rows, paths["csv_file"])

        gmail_client.save_state(paths["state_file"], new_state)

        if rag_session.has_session(user_id):
            rag_session.refresh_session(user_id)

        msg = f"{len(rows)} new email(s) synced." if rows else "No new emails."
        return SyncResponse(status="ok", new_emails=len(rows), message=msg)

    except Exception as e:
        logger.exception("Sync failed")
        raise HTTPException(500, f"Sync failed — {e}")


# ---------------- Chatbot lifecycle ----------------

@router.post("/chatbot/start")
def start_chatbot(request: Request, response: Response):
    user_id = get_user_id(request, response)
    paths = gmail_client.get_user_paths(user_id)
    if not os.path.exists(paths["token_file"]):
        raise HTTPException(400, "Connect Gmail first.")
    try:
        rag_session.get_or_create_session(user_id)
    except Exception as e:
        logger.exception("Failed to start chatbot")
        raise HTTPException(500, f"Failed to load chatbot — {e}")
    return {"status": "ready"}


@router.post("/chat/new")
def new_chat(request: Request, response: Response):
    """Starts a fresh conversation — new session_id, empty history, empty graph checkpoint thread."""
    user_id = get_user_id(request, response)
    if not rag_session.has_session(user_id):
        raise HTTPException(400, "Start the chatbot first.")
    session = rag_session.get_or_create_session(user_id)
    session_id = session.new_chat_session()
    return {"session_id": session_id}


# ---------------- Chat ----------------

@router.post("/chat")
def chat(request: Request, response: Response, body: ChatRequest):
    user_id = get_user_id(request, response)
    if not rag_session.has_session(user_id):
        raise HTTPException(400, "Start the chatbot first.")
    session = rag_session.get_or_create_session(user_id)
    try:
        result = session.chat(body.message, session_id=body.session_id or "default")
    except Exception as e:
        logger.exception("Chat failed")
        result = {"type": "text", "content": f"Something went wrong — {e}"}
    return result


@router.post("/chat/stream")
def chat_stream(request: Request, response: Response, body: ChatRequest):
    user_id = get_user_id(request, response)
    if not rag_session.has_session(user_id):
        raise HTTPException(400, "Start the chatbot first.")
    session = rag_session.get_or_create_session(user_id)

    def event_gen():
        try:
            for item in session.chat_stream(body.message, session_id=body.session_id or "default"):
                if isinstance(item, tuple) and item[0] == "__final__":
                    yield f"event: final\ndata: {json.dumps(item[1])}\n\n"
                else:
                    yield f"event: chunk\ndata: {json.dumps({'text': item})}\n\n"
        except Exception as e:
            logger.exception("Streaming chat failed")
            yield f"event: final\ndata: {json.dumps({'type': 'text', 'content': f'Something went wrong — {e}'})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------------- Send email ----------------

@router.post("/send-email")
def send_email(request: Request, response: Response, body: SendEmailRequest):
    user_id = get_user_id(request, response)
    session = rag_session.get_or_create_session(user_id)
    try:
        session.send_pending_email(recipient=body.recipient, subject=body.subject, body=body.body)
        return {"status": "sent"}
    except Exception as e:
        logger.exception("Send failed")
        raise HTTPException(500, f"Failed to send — {e}")


# ---------------- Health ----------------

@router.get("/health")
def health():
    return {"status": "ok"}
