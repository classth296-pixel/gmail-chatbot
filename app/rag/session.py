# ================================================
# app/rag/session.py
# One UserSession per authenticated user: owns the hybrid retriever bundle,
# the compiled LangGraph, and per-chat-session message history. This is the
# main entrypoint the API layer calls into.
# ================================================

import uuid

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage

from app.config import settings
from app.ingestion.gmail_client import get_user_paths
from app.rag.retriever import HybridRetrieverBundle
from app.rag.graph import compile_graph
from app.rag.nodes import (
    _extract_text, build_generate_messages, build_trimmer, NO_EVIDENCE_MESSAGE,
    make_contextualize_node, make_retrieve_node, make_grade_node,
)
from app.services import email_service

_sessions: dict[str, "UserSession"] = {}


class UserSession:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.paths = get_user_paths(user_id)

        self.model = ChatGoogleGenerativeAI(
            model=settings.CHAT_MODEL,
            google_api_key=settings.GOOGLE_API_KEY,
        )

        self._bundle = None  # lazy-built, rebuilt on refresh()
        self.graph = compile_graph(self.model, self._get_bundle, self.paths["checkpoint_db"])

        # Node functions reused directly by the streaming chat path (everything
        # up to generation), so token streaming doesn't have to re-implement
        # contextualize/retrieve/grade logic.
        self.trimmer = build_trimmer()
        self._contextualize = make_contextualize_node(self.model)
        self._retrieve = make_retrieve_node(self._get_bundle)
        self._grade = make_grade_node(self.model)

        # chat-session_id -> list[BaseMessage]
        self.histories: dict[str, list] = {}

    def _get_bundle(self) -> HybridRetrieverBundle:
        if self._bundle is None:
            self._bundle = HybridRetrieverBundle(self.user_id, self.paths)
        return self._bundle

    def refresh(self):
        """Call after fetching new mail so the retriever picks up fresh data."""
        self._bundle = HybridRetrieverBundle(self.user_id, self.paths)

    def _history(self, session_id: str) -> list:
        return self.histories.setdefault(session_id, [])

    # ---- Main entrypoint ----
    def chat(self, user_input: str, session_id: str = "default") -> dict:
        history = self._history(session_id)

        # "send this email to X" — resolved from the assistant's last drafted message.
        if user_input.strip().lower().startswith("send this email to "):
            recipient = user_input[len("send this email to "):].strip()
            last_ai = next((m for m in reversed(history) if isinstance(m, AIMessage)), None)
            if not last_ai:
                return {"type": "text", "content": "I don't have a drafted email to send yet — ask me to draft one first."}
            subject, body = email_service.extract_subject_and_body(_extract_text(last_ai.content))
            return {"type": "pending_send", "recipient": recipient, "subject": subject, "body": body}

        result = self.graph.invoke(
            {"question": user_input, "chat_history": history},
            config={"configurable": {"thread_id": f"{self.user_id}:{session_id}"}},
        )
        answer = result.get("answer", "Sorry, I couldn't generate a response.")

        history.append(HumanMessage(user_input))
        history.append(AIMessage(answer))

        return {"type": "text", "content": answer}

    # ---- Streaming entrypoint (used by the SSE /api/chat/stream route) ----
    def chat_stream(self, user_input: str, session_id: str = "default"):
        """
        Generator yielding text chunks as they're produced by the model.
        Runs contextualize -> retrieve -> grade synchronously (fast, no
        user-visible latency win from streaming those), then streams the
        final generation token-by-token. The last item yielded is always
        the special tuple ("__final__", full_answer_or_pending_dict) so the
        caller can persist history / detect a pending send.
        """
        history = self._history(session_id)

        if user_input.strip().lower().startswith("send this email to "):
            recipient = user_input[len("send this email to "):].strip()
            last_ai = next((m for m in reversed(history) if isinstance(m, AIMessage)), None)
            if not last_ai:
                msg = "I don't have a drafted email to send yet — ask me to draft one first."
                yield msg
                yield ("__final__", {"type": "text", "content": msg})
                return
            subject, body = email_service.extract_subject_and_body(_extract_text(last_ai.content))
            pending = {"type": "pending_send", "recipient": recipient, "subject": subject, "body": body}
            yield f"Draft ready for {recipient} — review it below."
            yield ("__final__", pending)
            return

        state = {"question": user_input, "chat_history": history}
        state.update(self._contextualize(state))
        state.update(self._retrieve(state))
        state.update(self._grade(state))

        messages = build_generate_messages(state, self.trimmer)
        if messages is None:
            yield NO_EVIDENCE_MESSAGE
            history.append(HumanMessage(user_input))
            history.append(AIMessage(NO_EVIDENCE_MESSAGE))
            yield ("__final__", {"type": "text", "content": NO_EVIDENCE_MESSAGE})
            return

        full_answer = ""
        for chunk in self.model.stream(messages):
            piece = _extract_text(chunk.content)
            if piece:
                full_answer += piece
                yield piece

        history.append(HumanMessage(user_input))
        history.append(AIMessage(full_answer))
        yield ("__final__", {"type": "text", "content": full_answer})

    def send_pending_email(self, recipient: str, subject: str, body: str):
        return email_service.send_email(
            token_file=self.paths["token_file"],
            to=recipient,
            subject=subject,
            body=body,
        )

    def new_chat_session(self) -> str:
        session_id = uuid.uuid4().hex[:12]
        self.histories[session_id] = []
        return session_id


def get_or_create_session(user_id: str) -> UserSession:
    if user_id not in _sessions:
        _sessions[user_id] = UserSession(user_id)
    return _sessions[user_id]


def refresh_session(user_id: str) -> UserSession:
    session = get_or_create_session(user_id)
    session.refresh()
    return session


def has_session(user_id: str) -> bool:
    return user_id in _sessions
