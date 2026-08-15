# ================================================
# main.py
# FastAPI entrypoint. Run with:
#   uvicorn main:app --host 0.0.0.0 --port 8000
# ================================================

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from app.config import settings
from app.api.routes import router as api_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Correspondent — redirect URI: %s", settings.REDIRECT_URI)
    logger.info("Using CHAT_MODEL=%r  EMBEDDING_MODEL=%r", settings.CHAT_MODEL, settings.EMBEDDING_MODEL)
    if "/" in settings.CHAT_MODEL.replace("models/", "", 1) or settings.CHAT_MODEL.startswith("google/"):
        logger.warning(
            "CHAT_MODEL looks malformed (%r) — it should be a bare model name like "
            "'gemini-3.1-flash-lite', not prefixed with 'google/'. Check your .env file.",
            settings.CHAT_MODEL,
        )
    if settings.EMBEDDING_MODEL.startswith("google/"):
        logger.warning(
            "EMBEDDING_MODEL looks malformed (%r) — it should look like "
            "'models/gemini-embedding-001', not prefixed with 'google/'. Check your .env file.",
            settings.EMBEDDING_MODEL,
        )
    yield
    logger.info("Shutting down.")


app = FastAPI(title="Correspondent — Email Assistant", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.include_router(api_router)


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "redirect_uri": settings.REDIRECT_URI})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=False)