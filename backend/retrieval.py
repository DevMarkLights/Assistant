# Hybrid retrieval for chat context: vector search (meaning) plus keyword search (exact terms such
# as "OMSCS" that embeddings can miss), merged and re-ranked with a recency bonus.
#
#   score = similarity + KEYWORD_WEIGHT * keyword + RECENCY_WEIGHT * recency
#
# keyword is the text score relative to the best keyword match (0-1); recency is 1 for something
# from today and halves every RECENCY_HALF_LIFE_DAYS. A result is kept if its similarity clears
# min_similarity or it matched a keyword. Dedupe (remember) and forget keep using plain vector
# search, since their thresholds were calibrated on raw similarity.
from datetime import datetime, timezone

import db2
from config import EMBED_MODEL, KEYWORD_WEIGHT, RECENCY_HALF_LIFE_DAYS, RECENCY_WEIGHT
from embeddings import cosine_similarity

CANDIDATES = 4  # fetch this many times top_k from each search before re-ranking


def recency(timestamp: str, now: datetime) -> float:
    age_days = max(0.0, (now - datetime.fromisoformat(timestamp)).total_seconds() / 86400)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def rank(vector_hits: list[dict], keyword_hits: list[dict], query_vec: list[float], time_field: str,
         top_k: int, min_similarity: float, now: datetime | None = None) -> list[dict]:
    """Merges both result lists by id and returns the top_k by combined score."""
    now = now or datetime.now(timezone.utc)
    best_keyword = max((h["keyword_score"] for h in keyword_hits), default=0) or 1
    merged = {h["id"]: {**h, "similarity": h["score"], "keyword": 0.0} for h in vector_hits}
    for h in keyword_hits:
        item = merged.setdefault(h["id"], {**h, "similarity": cosine_similarity(query_vec, h["embedding"])})
        item["keyword"] = h["keyword_score"] / best_keyword

    results = []
    for item in merged.values():
        if item["similarity"] < min_similarity and not item["keyword"]:
            continue
        item["recency"] = recency(item[time_field], now)
        item["score"] = round(item["similarity"] + KEYWORD_WEIGHT * item["keyword"]
                              + RECENCY_WEIGHT * item["recency"], 3)
        item.pop("embedding", None)
        item.pop("keyword_score", None)
        results.append(item)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


async def recall_exchanges(user_id: str, query: str, query_vec: list[float], top_k: int,
                           min_similarity: float, exclude_ids=()) -> list[dict]:
    excluded = set(exclude_ids)
    n = top_k * CANDIDATES
    vector = await db2.search_exchanges(user_id, query_vec, EMBED_MODEL, n, 0.0, exclude_ids=excluded)
    keyword = [h for h in await db2.keyword_search_exchanges(user_id, query, EMBED_MODEL, n)
               if h["id"] not in excluded]
    return rank(vector, keyword, query_vec, "created_at", top_k, min_similarity)


async def recall_facts(user_id: str, query: str, query_vec: list[float], top_k: int,
                       min_similarity: float) -> list[dict]:
    n = top_k * CANDIDATES
    vector = await db2.search_facts(user_id, query_vec, EMBED_MODEL, n, 0.0)
    keyword = await db2.keyword_search_facts(user_id, query, EMBED_MODEL, n)
    return rank(vector, keyword, query_vec, "updated_at", top_k, min_similarity)
