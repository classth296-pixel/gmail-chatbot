# Correspondent — AI Email Assistant

FastAPI + LangGraph rewrite of the original Streamlit prototype, built to
run comfortably on an EC2 free-tier instance (1GB RAM / t2.micro).

## What changed from the Streamlit version

- **Streamlit → FastAPI + vanilla HTML/CSS/JS.** Streamlit re-executes the
  whole script on every interaction; FastAPI serves a static page once and
  talks to small REST/SSE endpoints, cutting steady-state RAM from 200MB+
  to roughly 60–100MB.
- **Heuristic routing → LangGraph.** The old `is_structured_query` regex
  hack bypassed the LLM entirely for sender/date lookups, which broke
  follow-up questions. It's now a proper graph:
  `contextualize → retrieve → grade → generate`, so filters are extracted
  by the LLM (with conversational context) and results still flow through
  grounded generation.
- **Grading node.** Retrieved chunks are graded for relevance before
  generation, instead of being fed straight to the model.
- **Richer metadata + chunking.** Emails now carry `thread_id`, `recipient`,
  `snippet`, and long email bodies are recursively split so nothing is
  silently truncated into one blob.
- **No artificial `time.sleep(10)` during ingestion.** Batches are
  submitted back-to-back; real exponential backoff only kicks in if a
  batch actually fails.
- **Gmail History API** for incremental sync after the first fetch, instead
  of always re-listing the whole inbox.
- **SSE streaming** for the chat response (`/api/chat/stream`).

## Project structure

```
app/
├── config.py                 # env vars & constants
├── ingestion/
│   ├── gmail_client.py       # OAuth + fetch (full + incremental via History API)
│   ├── preprocessor.py       # header-aware chunking → Documents
│   └── indexer.py            # Chroma writes, real backoff, no fake sleeps
├── rag/
│   ├── embeddings.py         # embeddings client + retry wrapper
│   ├── retriever.py          # hybrid Chroma+BM25 retriever, metadata filters
│   ├── nodes.py              # LangGraph nodes (contextualize/retrieve/grade/generate)
│   ├── graph.py               # compiles the StateGraph w/ SQLite checkpointer
│   └── session.py            # per-user session: history + chat + chat_stream
├── services/email_service.py # draft parsing + Gmail send
├── api/
│   ├── schemas.py
│   └── routes.py              # /api/status, /oauth/*, /sync, /chat, /chat/stream, /send-email
├── static/{css,js}
└── templates/index.html
main.py                        # FastAPI app entrypoint
setup_ec2_swap.sh               # 2GB swap for free-tier RAM headroom
deploy_ec2.sh                   # systemd + nginx reverse proxy
```

Your original `emails.csv`, `credentials.json`, `token.json`, and
`chroma_db` under `users/<id>/` are read as-is — no migration needed,
just drop this project next to your existing `users/` folder (or copy it
in) before first run.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set GOOGLE_API_KEY, leave REDIRECT_URI as the localhost default

uvicorn main:app --reload
```

Open `http://localhost:8000`. In Google Cloud Console → APIs & Services →
Credentials, make sure your OAuth "Web application" client has
`http://localhost:8000/api/oauth/callback` registered as a redirect URI,
then upload your `credentials.json` in the UI and click **Connect Gmail**.

## Deploying to EC2 free tier

1. Launch a `t2.micro`/`t3.micro` Ubuntu 22.04/24.04 instance, open port 80
   (and 22 for SSH) in its security group.
2. Copy the project to the instance (`git clone` or `scp`), and your
   existing `users/` folder if migrating data.
3. `sudo bash setup_ec2_swap.sh` — adds a 2GB swap file so embedding
   batches don't OOM on 1GB RAM.
4. Create `.env` from `.env.example`. Set `REDIRECT_URI` to
   `http://<your-ec2-public-ip>/api/oauth/callback` (or your domain), and
   register that same URI in Google Cloud Console.
5. `sudo bash deploy_ec2.sh` — installs deps into a venv, and sets up:
   - a `systemd` service (`ai-email-assistant`) running Uvicorn on
     `127.0.0.1:8000`, with `MemoryMax=700M` as a guardrail
   - an `nginx` reverse proxy on port 80, with buffering off so SSE chat
     streaming flushes promptly
6. Visit `http://<your-ec2-public-ip>/`, connect Gmail, and start chatting.

Check memory headroom under load: `free -h` and `top`. Logs:
`journalctl -u ai-email-assistant -f`.

## Verification checklist

- **Startup**: `uvicorn main:app` boots without import errors; visiting
  `/api/health` returns `{"status": "ok"}`.
- **Chunking/metadata**: after `/api/sync`, check
  `users/<id>/emails.csv` has `thread_id`/`recipient`/`snippet` columns,
  and that a long email produced multiple `message_id::N` chunks in Chroma.
- **Follow-up questions**: ask "What did John send about the budget?" then
  "When was that sent?" — the second question should resolve "that"
  correctly via the contextualize node.
- **Grounding**: ask something with no matching emails (e.g. "what's the
  secret recipe for chocolate cake?") — the assistant should say it found
  nothing, not invent an answer.
- **Draft + send**: ask "draft an email to a@example.com about project
  updates", confirm the envelope card shows recipient/subject/body, then
  confirm-send and check it lands in Sent Mail.
- **Memory**: under active use on a 1GB instance, `free -h` should stay
  within budget thanks to the swap file and `MemoryMax` guardrail.

## Notes / follow-ups

- Single-tenant-per-browser via a cookie (`correspondent_uid`) rather than
  a full login system — good enough for a personal EC2 deployment; swap in
  real auth if you expose this beyond yourself.
- The conversation ledger is cached client-side (localStorage) for display;
  wire it to a server-side store if you need it to survive across devices.
- `langgraph-checkpoint-sqlite` persists graph checkpoints per user in
  `users/<id>/checkpoints.sqlite`, so state layout survives restarts even
  though chat history itself is currently kept in-process (add a
  Redis/SQLite-backed history store if you need it to survive process
  restarts too).
