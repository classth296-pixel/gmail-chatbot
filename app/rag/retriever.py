# ================================================
# app/rag/retriever.py
# Builds a hybrid (dense + sparse) retriever per user, with support for
# metadata filters (sender / date range / subject keywords) extracted by
# the contextualizer node.
# ================================================

import os
import re
import logging
from difflib import get_close_matches

from langchain_community.document_loaders import CSVLoader
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from langchain_chroma import Chroma

from app.config import settings
from app.rag.embeddings import get_embeddings

logger = logging.getLogger("retriever")


class HybridRetrieverBundle:
    """Holds everything needed to retrieve + filter for one user."""

    def __init__(self, user_id: str, paths: dict):
        self.user_id = user_id
        self.paths = paths
        self.embeddings = get_embeddings()

        self.vectorstore = Chroma(
            persist_directory=paths["chroma_dir"],
            embedding_function=self.embeddings,
        )

        if os.path.exists(paths["csv_file"]):
            loader = CSVLoader(file_path=paths["csv_file"], encoding="utf-8")
            self.documents = loader.load()
        else:
            self.documents = []

        dense = self.vectorstore.as_retriever(search_kwargs={"k": settings.RETRIEVER_K})
        self.bm25 = None
        if self.documents:
            self.bm25 = BM25Retriever.from_documents(self.documents)
            self.bm25.k = settings.RETRIEVER_K
            self.hybrid = EnsembleRetriever(retrievers=[self.bm25, dense], weights=[0.4, 0.6])
        else:
            self.hybrid = dense

    # ---- Metadata-filtered lookups (bypass embedding similarity entirely) ----

    def get_known_sender_names(self) -> set:
        all_docs = self.vectorstore.get(include=["metadatas"])
        names = set()
        for metadata in all_docs["metadatas"]:
            sender = (metadata.get("sender") or "").lower()
            display_name = sender.split("<")[0].strip()
            if display_name:
                names.add(display_name)
            email_match = re.search(r"[\w.\-]+@[\w.\-]+", sender)
            if email_match:
                names.add(email_match.group(0))
        return names

    def extract_sender(self, question: str):
        match = re.search(r"[\w.\-]+@[\w.\-]+", question)
        if match:
            return match.group(0)
        known = self.get_known_sender_names()
        q_lower = question.lower()
        for name in known:
            if name and name in q_lower:
                return name
        for word in re.findall(r"\w+", q_lower):
            if len(word) < 4:
                continue
            close = get_close_matches(word, known, n=1, cutoff=0.7)
            if close:
                return close[0]
        return None

    def lookup_by_filter(self, sender: str = None, target_date: str = None, subject_kw: str = None):
        all_docs = self.vectorstore.get(include=["documents", "metadatas"])
        matched = []
        for doc_text, metadata in zip(all_docs["documents"], all_docs["metadatas"]):
            meta_sender = (metadata.get("sender") or "").lower()
            meta_date = metadata.get("date") or ""
            meta_subject = (metadata.get("subject") or "").lower()

            sender_ok = sender.lower() in meta_sender if sender else True
            date_ok = (meta_date == target_date) if target_date else True
            subject_ok = subject_kw.lower() in meta_subject if subject_kw else True

            if sender_ok and date_ok and subject_ok:
                matched.append({"content": doc_text, "metadata": metadata})
        return matched

    def retrieve(self, query: str):
        try:
            return self.hybrid.invoke(query)
        except Exception as e:
            # Embeddings quota/outage — fall back to keyword-only search instead
            # of failing the whole chat turn. Still useful, just less precise.
            logger.warning("Dense retrieval failed (%s) — falling back to BM25-only search.", e)
            if self.bm25:
                return self.bm25.invoke(query)
            return []
