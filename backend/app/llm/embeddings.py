# Generates text embeddings via the Ollama /api/embed endpoint and provides a cosine similarity helper.
# The embedding model is kept loaded (OLLAMA_KEEP_ALIVE) and warmed up at startup: on the Pi,
# reloading it after Ollama's default 5 idle minutes takes seconds and delays the next reply.
import httpx
import numpy as np

from app.core.config import EMBED_MODEL, OLLAMA_KEEP_ALIVE, OLLAMA_URL
from app.core.logs import get_logger

log = get_logger("embeddings")

# One client for all calls, so connections to Ollama are reused
_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))


def _keep_alive():
    """Ollama takes a number of seconds (-1 = forever) or a duration string like "24h"."""
    try:
        return int(OLLAMA_KEEP_ALIVE)
    except ValueError:
        return OLLAMA_KEEP_ALIVE


async def embed(text: str, kind: str = "document") -> list[float]:
    if not OLLAMA_URL or not EMBED_MODEL:
        return {"error": "no Ollama Url or Embed Model"}
    # nomic-embed-text was trained with these prefixes
    prefix = "search_query: " if kind == "query" else "search_document: "
    resp = await _client.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": prefix + text, "keep_alive": _keep_alive()},
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


async def embed_many(texts: list[str], kind: str = "document") -> list[list[float]]:
    """Embeds several texts in one Ollama call."""
    prefix = "search_query: " if kind == "query" else "search_document: "
    resp = await _client.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": [prefix + t for t in texts], "keep_alive": _keep_alive()},
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


async def warm_up() -> None:
    """Loads the embedding model into memory now, so the first chat message doesn't wait for it."""
    try:
        await embed("warm up", "query")
        log.info("embedding model %s loaded (keep_alive=%s)", EMBED_MODEL, OLLAMA_KEEP_ALIVE)
    except Exception:
        log.warning("couldn't warm up the embedding model at %s", OLLAMA_URL, exc_info=True)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
