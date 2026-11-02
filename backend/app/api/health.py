# Health checks (the API is login-only, like every route) and a debugging endpoint that compares two
# texts' embeddings.
import httpx
from fastapi import APIRouter, HTTPException

from app.api import schemas
from app.core.config import OLLAMA_URL
from app.llm import embeddings

router = APIRouter(prefix="/Assistant/api", tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/ollama")
async def ollama_health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")
    return {"status": "ok", "models": [m["name"] for m in resp.json()["models"]]}


@router.post("/embed/compare")
async def compare(req: schemas.CompareRequest):
    va = await embeddings.embed(req.a, "query")
    vb = await embeddings.embed(req.b, "document")
    if "error" in va:
        return {"error": va["error"]}
    if "error" in vb:
        return {"error": vb["error"]}
    return {"similarity": round(embeddings.cosine_similarity(va, vb), 3), "dimensions": len(va)}
