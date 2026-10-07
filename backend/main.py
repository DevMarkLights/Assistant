# FastAPI application entry point: registers routers, initializes the database on startup,
# and exposes health-check and embedding-comparison endpoints. Serves the built frontend as static files.
from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
import httpx
import embeddings 
import models
import memory
from contextlib import asynccontextmanager
import db2
import conversations
import auth


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db2.init_db()
    yield
    await db2.close_db()
    
app = FastAPI(title="Personal Assistant", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(memory.router)

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"

import os

OLLAMA_URL = os.getenv("OLLAMA_URL")
EMBED_MODEL = os.getenv("EMBED_MODEL")

@app.get("/Assistant/api/health")
def health():
    return {"status": "ok"}

@app.get('/Assistant/api/health/ollama')
async def ollama_health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")

    models = [m["name"] for m in resp.json()["models"]]
    return {"status": "ok", "models": models}

@app.post("/Assistant/api/embed/compare", dependencies=[Depends(auth.current_user)])
async def compare(req: models.CompareRequest):
    va = await embeddings.embed(req.a, "query")
    vb = await embeddings.embed(req.b, "document")
    if 'error' in va:
        return {"error": va['error']}
    if 'error' in vb:
        return {"error": vb['error']}
    return {"similarity": round(embeddings.cosine_similarity(va, vb), 3), "dimensions": len(va)}

app.mount("/Assistant", StaticFiles(directory="dist", html=True), name="static")
