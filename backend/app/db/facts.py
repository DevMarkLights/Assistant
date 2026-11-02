# Facts: lasting things the assistant knows about each user, with embeddings for search.
import uuid

from pymongo import DESCENDING

from .client import FACT_VECTOR_INDEX, facts, keyword_search, now

__all__ = ['add_fact', 'update_fact', 'get_fact', 'delete_fact', 'list_facts', 'search_facts', 'keyword_search_facts']


def _fact_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "text": doc["text"],
        "category": doc.get("category"),
        "created_at": doc["created_at"].isoformat(),
        "updated_at": doc["updated_at"].isoformat(),
    }


async def add_fact(user_id: str, text: str, category: str, embedding: list[float], embed_model: str,
                   source_conversation_id: str) -> str:
    ts = now()
    doc = {
        "_id": uuid.uuid4().hex,
        "user_id": user_id,
        "text": text,
        "category": category,
        "embedding": embedding,
        "embed_model": embed_model,
        "source_conversation_id": source_conversation_id,
        "history": [],
        "created_at": ts,
        "updated_at": ts,
    }
    await facts.insert_one(doc)
    return doc["_id"]


async def update_fact(user_id: str, fact_id: str, text: str, embedding: list[float]) -> bool:
    old = await facts.find_one({"_id": fact_id, "user_id": user_id}, {"text": 1})
    if not old:
        return False
    ts = now()
    await facts.update_one(
        {"_id": fact_id, "user_id": user_id},
        {
            "$set": {"text": text, "embedding": embedding, "updated_at": ts},
            "$push": {"history": {"text": old["text"], "replaced_at": ts}},
        },
    )
    return True


async def get_fact(user_id: str, fact_id: str) -> dict | None:
    doc = await facts.find_one({"_id": fact_id, "user_id": user_id}, {"embedding": 0})
    return _fact_out(doc) if doc else None


async def delete_fact(user_id: str, fact_id: str) -> bool:
    result = await facts.delete_one({"_id": fact_id, "user_id": user_id})
    return result.deleted_count > 0


async def list_facts(user_id: str, limit: int = 0) -> list[dict]:
    cursor = facts.find({"user_id": user_id}, {"embedding": 0}).sort("updated_at", DESCENDING).limit(limit)
    return [_fact_out(doc) async for doc in cursor]


async def search_facts(user_id: str, query_vec: list[float], embed_model: str, top_k: int = 5,
                       min_similarity: float = 0.0) -> list[dict]:
    pipeline = [
        {
            "$vectorSearch": {
                "index": FACT_VECTOR_INDEX,
                "path": "embedding",
                "queryVector": query_vec,
                "numCandidates": top_k * 20,
                "limit": top_k,
                "filter": {"embed_model": embed_model, "user_id": user_id},
            }
        },
        {"$project": {"embedding": 0, "history": 0, "score": {"$meta": "vectorSearchScore"}}},
    ]

    results = []
    cursor = await facts.aggregate(pipeline)
    async for doc in cursor:
        cosine = 2 * doc["score"] - 1
        if cosine < min_similarity:
            break
        out = _fact_out(doc)
        out["score"] = round(cosine, 3)
        results.append(out)
    return results


async def keyword_search_facts(user_id: str, query: str, embed_model: str, limit: int) -> list[dict]:
    out = []
    for doc in await keyword_search(facts, user_id, query, embed_model, limit):
        item = _fact_out(doc)
        item["keyword_score"], item["embedding"] = doc["keyword_score"], doc["embedding"]
        out.append(item)
    return out
