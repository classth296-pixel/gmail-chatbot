# ================================================
# app/rag/nodes.py
# LangGraph node functions. Each node takes the shared GraphState dict and
# returns a partial update to merge into it.
# ================================================

import json
import logging
from datetime import date
from typing import TypedDict, Optional

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, trim_messages

from app.config import settings
from app.rag.embeddings import invoke_with_retry

logger = logging.getLogger("rag.nodes")


class GraphState(TypedDict, total=False):
    question: str                # possibly rewritten, standalone question
    original_question: str
    chat_history: list           # list of BaseMessage
    filters: dict                # {sender, date, subject}
    documents: list              # [{content, metadata}]
    answer: str
    mode: str                    # "data_question" | "draft_email"
    no_evidence: bool


SYSTEM_PROMPT = (
    "You are a helpful email assistant that helps the user search, understand, and act on their inbox. "
    "You have access to (1) retrieved emails as CONTEXT and (2) the ongoing chat HISTORY.\n\n"
    "## Modes\n"
    "**DATA QUESTIONS** — answer strictly using the given context and history. If asked for recent "
    "emails, list them with date, time, and sender. If the answer isn't in the context or history, "
    "say clearly that you don't know — never guess or invent email content. Cite the source of every "
    "factual claim like: [Source: Subject | Sender | Date].\n\n"
    "**EDIT REQUESTS** — if the user is asking you to modify something YOU already wrote earlier in "
    "this conversation, apply the change directly using chat history; do not require inbox context.\n\n"
    "## Writing Emails\n"
    "When asked to draft an email, produce a complete, professional email with a clear Subject line "
    "and a well-structured body. No date unless asked. No source citations in drafted emails. Match "
    "tone to the recipient. If details are missing, make a reasonable assumption and note it briefly.\n\n"
    "## General\n"
    "Be concise, avoid filler preamble. Ask a brief clarifying question only if the request is "
    "genuinely ambiguous between multiple people/emails. Never fabricate senders, dates, or content."
)

CONTEXTUALIZE_PROMPT = (
    "Given the chat history and a new user question, do two things and respond ONLY with JSON:\n"
    '{{"standalone_question": "...", "sender": null or "...", "date": null or "YYYY-MM-DD", '
    '"subject_keyword": null or "...", "mode": "data_question" or "draft_email"}}\n\n'
    "- standalone_question: rewrite the question so it's understandable without the chat history "
    "(resolve pronouns like 'that', 'him', 'it'). If it's already standalone, return it unchanged.\n"
    "- sender: an email address or name mentioned as the sender to filter by, else null.\n"
    "- date: an explicit date the user is asking about, resolved to YYYY-MM-DD (today is {today}), else null.\n"
    "- subject_keyword: a distinctive subject keyword to filter by, else null.\n"
    "- mode: 'draft_email' if the user wants you to compose/write/reply to an email, otherwise 'data_question'.\n\n"
    "Chat history:\n{history}\n\nNew question: {question}\n\nJSON:"
)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in content
        )
    return str(content)


def _history_to_text(history: list) -> str:
    lines = []
    for m in history[-6:]:
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        lines.append(f"{role}: {_extract_text(m.content)[:400]}")
    return "\n".join(lines) if lines else "(none)"


# ---------------- Nodes ----------------

def make_contextualize_node(model):
    def contextualize_and_extract_filters(state: GraphState) -> dict:
        question = state["question"]
        history = state.get("chat_history", [])

        if not history:
            return {
                "original_question": question,
                "question": question,
                "filters": {},
                "mode": "data_question",
            }

        prompt = CONTEXTUALIZE_PROMPT.format(
            today=date.today().isoformat(),
            history=_history_to_text(history),
            question=question,
        )
        try:
            response = invoke_with_retry(model.invoke, prompt)
            raw = _extract_text(response.content).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(raw)
        except Exception as e:
            logger.warning("Contextualizer failed, falling back to raw question: %s", e)
            parsed = {"standalone_question": question, "sender": None, "date": None,
                       "subject_keyword": None, "mode": "data_question"}

        return {
            "original_question": question,
            "question": parsed.get("standalone_question") or question,
            "filters": {
                "sender": parsed.get("sender"),
                "date": parsed.get("date"),
                "subject": parsed.get("subject_keyword"),
            },
            "mode": parsed.get("mode", "data_question"),
        }

    return contextualize_and_extract_filters


def make_retrieve_node(get_bundle):
    def retrieve_documents(state: GraphState) -> dict:
        bundle = get_bundle()
        filters = state.get("filters") or {}
        sender, target_date, subject_kw = filters.get("sender"), filters.get("date"), filters.get("subject")

        if sender or target_date or subject_kw:
            if not sender:
                sender = bundle.extract_sender(state["question"])
            docs = bundle.lookup_by_filter(sender=sender, target_date=target_date, subject_kw=subject_kw)
            if docs:
                return {"documents": docs}
            # filters matched nothing — fall back to semantic search instead of dead-ending

        semantic_docs = bundle.retrieve(state["question"])
        documents = [{"content": d.page_content, "metadata": d.metadata} for d in semantic_docs]
        return {"documents": documents}

    return retrieve_documents


def make_grade_node(model):
    """Cheap relevance filter — drops chunks the LLM judges irrelevant to the question."""

    def grade_documents(state: GraphState) -> dict:
        docs = state.get("documents", [])
        if not docs:
            return {"documents": [], "no_evidence": True}

        # Batch-grade in one call to keep latency/cost low.
        listing = "\n".join(f"[{i}] {d['content'][:300]}" for i, d in enumerate(docs))
        prompt = (
            "You are grading which of these numbered email excerpts are relevant evidence for "
            f"answering the question. Question: {state['question']}\n\n{listing}\n\n"
            "Respond ONLY with a JSON array of the relevant indices, e.g. [0, 2]. "
            "If none are relevant, respond with []."
        )
        try:
            response = invoke_with_retry(model.invoke, prompt)
            raw = _extract_text(response.content).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            keep_idx = set(json.loads(raw))
        except Exception as e:
            logger.warning("Grading failed, keeping all retrieved docs: %s", e)
            keep_idx = set(range(len(docs)))

        graded = [d for i, d in enumerate(docs) if i in keep_idx]
        return {"documents": graded, "no_evidence": len(graded) == 0}

    return grade_documents


def build_generate_messages(state: GraphState, trimmer) -> Optional[list]:
    """Shared by the graph's generate node and the streaming chat path."""
    docs = state.get("documents", [])
    history = trimmer.invoke(state.get("chat_history", []))

    if state.get("mode") == "data_question" and state.get("no_evidence") and not history:
        return None  # signals "no evidence, no history — short-circuit with a fixed message"

    context = "\n\n".join(
        f"{d['content']}\n[Source: {d['metadata'].get('subject', '')} | "
        f"{d['metadata'].get('sender', '')} | {d['metadata'].get('date', '')}]"
        for d in docs
    ) or "(no matching emails found)"

    return [SystemMessage(SYSTEM_PROMPT), *history,
            HumanMessage(f"Context:\n{context}\n\nQuestion:\n{state['original_question']}")]


NO_EVIDENCE_MESSAGE = "I couldn't find any emails matching that — try fetching new mail or rephrasing the sender/date/subject."


def make_generate_node(model, trimmer):
    def generate_grounded_answer(state: GraphState) -> dict:
        messages = build_generate_messages(state, trimmer)
        if messages is None:
            return {"answer": NO_EVIDENCE_MESSAGE}

        response = invoke_with_retry(model.invoke, messages)
        return {"answer": _extract_text(response.content)}

    return generate_grounded_answer


def build_trimmer():
    def approx_token_counter(messages) -> int:
        text = " ".join(_extract_text(getattr(m, "content", "")) for m in messages)
        return len(text) // 4

    return trim_messages(
        max_tokens=1500,
        strategy="last",
        token_counter=approx_token_counter,
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
