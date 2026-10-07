# Generates text embeddings via the Ollama /api/embed endpoint and provides a cosine similarity helper.
import httpx
import numpy as np
import os
from config import OLLAMA_URL, EMBED_MODEL


async def embed(text: str, kind: str = "document") -> list[float]:
    if not OLLAMA_URL or not EMBED_MODEL:
        return {"error": "no Ollama Url or Embed Model"}
    # nomic-embed-text was trained with these prefixes
    prefix = "search_query: " if kind == "query" else "search_document: "
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": prefix + text},
        )
        resp.raise_for_status()
    return resp.json()["embeddings"][0]

def cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))