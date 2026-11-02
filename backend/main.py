# FastAPI entry point (run from backend/: `uvicorn main:app` or `fastapi dev main.py`). Sets up the
# database, background jobs, and warm-ups on startup, the login-only API, and serves the built
# frontend from dist/. The code lives in the app/ package.
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import auth, brief, chat, health, memory, push, tts
from app.core.config import ENABLE_DOCS
from app.llm import embeddings
from app.services import scheduler
from app.services import tts as tts_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    scheduler.start()
    # Load the embedding model and Piper voices in the background so the first replies are fast
    warm_up = asyncio.create_task(embeddings.warm_up())
    voices = asyncio.create_task(tts_service.preload())
    yield
    warm_up.cancel()
    voices.cancel()
    scheduler.stop()
    await db.close_db()


docs = {} if ENABLE_DOCS else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Personal Assistant", lifespan=lifespan, **docs)
app.middleware("http")(auth.require_login)
for module in (auth, chat, memory, push, brief, tts, health):
    app.include_router(module.router)

# Last, so API routes take precedence over the static files
app.mount("/Assistant", StaticFiles(directory="dist", html=True), name="static")
