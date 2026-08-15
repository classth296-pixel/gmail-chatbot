# ================================================
# app/ingestion/preprocessor.py
# Turns raw email rows (dicts) into LangChain Documents, using recursive
# character splitting for long bodies so single giant emails don't become
# one unsearchable blob, while every chunk keeps full header metadata.
# ================================================

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=150,
    separators=["\n\n", "\n", ". ", " ", ""],
)


def rows_to_documents(rows: list[dict]) -> list[tuple[Document, str]]:
    """
    Returns a list of (Document, id) pairs. Short emails become a single
    chunk (id == message_id); long emails are split into multiple chunks
    (id == "{message_id}::{chunk_index}") so nothing is lost to truncation.
    """
    out = []
    for row in rows:
        header = (
            f"From: {row.get('sender', '')}\n"
            f"To: {row.get('recipient', '')}\n"
            f"Date: {row.get('date', '')} {row.get('receive_time', '')}\n"
            f"Subject: {row.get('subject', '')}\n"
        )
        body = row.get("message", "")
        metadata = {
            "message_id": row.get("message_id", ""),
            "thread_id": row.get("thread_id", ""),
            "sender": row.get("sender", ""),
            "recipient": row.get("recipient", ""),
            "subject": row.get("subject", ""),
            "date": row.get("date", ""),
            "receive_time": row.get("receive_time", ""),
        }

        if len(body) <= 1000:
            content = header + "Message: " + body
            out.append((Document(page_content=content, metadata=metadata), row["message_id"]))
            continue

        chunks = splitter.split_text(body)
        for i, chunk in enumerate(chunks):
            content = header + f"Message (part {i + 1}/{len(chunks)}): " + chunk
            chunk_meta = {**metadata, "chunk_index": i}
            out.append((Document(page_content=content, metadata=chunk_meta), f"{row['message_id']}::{i}"))

    return out
