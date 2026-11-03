# Exchanges: one user message plus the assistant's reply, with its embedding for memory search.
import uuid

from pymongo import ASCENDING

from .client import VECTOR_INDEX, exchanges, keyword_search, now

__all__ = ['add_exchange', 'list_exchanges', 'last_exchange_embedding', 'search_exchanges', 'keyword_search_exchanges']


def _exchange_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "conversation_id": doc["conversation_id"],
        "user_text": doc["user_text"],
        "assistant_text": doc["assistant_text"],
        "tool_calls": doc.get("tool_calls", []),
        "created_at": doc["created_at"].isoformat(),
    }


async def add_exchange(user_id: str, conversation_id: str, user_text: str, assistant_text: str,
                       embedding: list[float] | None, embed_model: str,
                       tool_calls: list[dict] | None = None) -> str:
    """user_text may be "" for assistant-initiated messages such as the morning brief."""
    doc = {
        "_id": uuid.uuid4().hex,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "embed_model": embed_model,
        "created_at": now(),
    }
    if embedding is not None:  # without one the exchange is still shown, just not vector-searchable
        doc["embedding"] = embedding
    if tool_calls:
        doc["tool_calls"] = tool_calls  # what the chat UI shows for each call: name, summary, detail
    await exchanges.insert_one(doc)
    return doc["_id"]


async def list_exchanges(user_id: str, conversation_id: str) -> list[dict]:
    cursor = exchanges.find({"conversation_id": conversation_id, "user_id": user_id}).sort("created_at", ASCENDING)
    return [_exchange_out(doc) async for doc in cursor]


async def last_exchange_embedding(user_id: str, conversation_id: str) -> list[float] | None:
    """The embedding of the conversation's latest exchange (to pick tools for a follow-up)."""
    doc = await exchanges.find_one({"conversation_id": conversation_id, "user_id": user_id, "embedding": {"$exists": True}},
                                   {"embedding": 1}, sort=[("created_at", -1)])
    return doc["embedding"] if doc else None


async def search_exchanges(user_id: str, query_vec: list[float], embed_model: str, top_k: int = 4,
                           min_similarity: float = 0.0, exclude_ids=()) -> list[dict]:
    excluded = set(exclude_ids)
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX,
                "path": "embedding",
                "queryVector": query_vec,
                "numCandidates": (top_k + len(excluded)) * 20,
                "limit": top_k + len(excluded),
                "filter": {"embed_model": embed_model, "user_id": user_id},
            }
        },
        {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
    ]

    results = []
    cursor = await exchanges.aggregate(pipeline)
    async for doc in cursor:
        if doc["_id"] in excluded:
            continue
        cosine = 2 * doc["score"] - 1
        if cosine < min_similarity:
            break
        out = _exchange_out(doc)
        out["score"] = round(cosine, 3)
        results.append(out)
        if len(results) == top_k:
            break
    return results


async def keyword_search_exchanges(user_id: str, query: str, embed_model: str, limit: int) -> list[dict]:
    out = []
    for doc in await keyword_search(exchanges, user_id, query, embed_model, limit):
        item = _exchange_out(doc)
        item["keyword_score"], item["embedding"] = doc["keyword_score"], doc["embedding"]
        out.append(item)
    return out
