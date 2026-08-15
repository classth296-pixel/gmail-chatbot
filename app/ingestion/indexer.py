# ================================================
# app/ingestion/indexer.py
# Adds new email rows into the Chroma vectorstore. No artificial sleeps —
# batches are submitted back-to-back and only retried (with real backoff)
# if a batch actually fails, instead of always sleeping "just in case".
# ================================================

import time
import logging

from app.ingestion.preprocessor import rows_to_documents

logger = logging.getLogger("indexer")


def add_rows_to_vectorstore(rows, vectorstore, batch_size: int = 10, max_retries: int = 3):
    if not rows:
        return {"succeeded": 0, "failed": 0}

    pairs = rows_to_documents(rows)
    docs = [p[0] for p in pairs]
    ids = [p[1] for p in pairs]

    total = len(docs)
    succeeded, failed = 0, 0

    for i in range(0, total, batch_size):
        batch_docs, batch_ids = docs[i:i + batch_size], ids[i:i + batch_size]
        for attempt in range(1, max_retries + 1):
            try:
                vectorstore.add_documents(documents=batch_docs, ids=batch_ids)
                succeeded += len(batch_docs)
                break
            except Exception as e:
                msg = str(e)
                if "PerDay" in msg:
                    # Daily embedding quota exhausted — no amount of retrying today will help.
                    # Stop the whole sync immediately instead of burning through remaining
                    # batches on doomed retries; whatever was already embedded is kept.
                    remaining = total - i
                    logger.error(
                        "Daily embedding quota exhausted after %s/%s chunks — stopping sync. "
                        "%s chunk(s) skipped this run; re-sync once quota resets.",
                        succeeded, total, remaining,
                    )
                    failed += remaining
                    return {"succeeded": succeeded, "failed": failed}
                if attempt == max_retries:
                    logger.error("Batch failed permanently: %s", e)
                    failed += len(batch_docs)
                else:
                    wait = 2 ** attempt  # real exponential backoff, only on actual failure
                    logger.warning("Batch failed (attempt %s/%s): %s — retrying in %ss", attempt, max_retries, e, wait)
                    time.sleep(wait)

    logger.info("%s/%s chunks embedded (%s failed)", succeeded, total, failed)
    return {"succeeded": succeeded, "failed": failed}
