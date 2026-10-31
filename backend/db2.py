# MongoDB-backed persistence layer for conversations and exchanges.
# Uses Atlas Vector Search ($vectorSearch) for semantic similarity queries.
import uuid
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from config import MONGODB_DB, MONGODB_URI

client = AsyncMongoClient(MONGODB_URI, tz_aware=True)
database = client[MONGODB_DB]
conversations = database["conversations"]
exchanges = database["exchanges"]
facts = database["facts"]
users = database["users"]
sessions = database["sessions"]
push_subscriptions = database["push_subscriptions"]
briefs = database["briefs"]

VECTOR_INDEX = "exchange_vector_index"
FACT_VECTOR_INDEX = "fact_vector_index"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _conversation_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "title": doc.get("title"),
        "created_at": doc["created_at"].isoformat(),
    }


def _exchange_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "conversation_id": doc["conversation_id"],
        "user_text": doc["user_text"],
        "assistant_text": doc["assistant_text"],
        "tool_calls": doc.get("tool_calls", []),
        "created_at": doc["created_at"].isoformat(),
    }


async def init_db():
    await client.admin.command("ping")
    await conversations.create_index([("created_at", DESCENDING)])
    await exchanges.create_index([("conversation_id", ASCENDING), ("created_at", ASCENDING)])
    await facts.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)])
    await conversations.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await users.create_index("username", unique=True)
    # TTL index: MongoDB removes sessions once expires_at has passed
    await sessions.create_index("expires_at", expireAfterSeconds=0)
    # Keyword search for hybrid retrieval. Plain text indexes, unlike Atlas Search indexes,
    # don't count against the free tier's limit of 3 search indexes.
    await exchanges.create_index([("user_text", "text"), ("assistant_text", "text")], name="exchange_text")
    await facts.create_index([("text", "text")], name="fact_text")
    await push_subscriptions.create_index("user_id")

async def close_db():
    await client.close()


# ---- conversations ----

async def set_title_if_empty(user_id: str, conversation_id: str, title: str) -> bool:
    """Sets an auto-generated title unless the conversation already has one (e.g. renamed meanwhile)."""
    result = await conversations.update_one(
        {"_id": conversation_id, "user_id": user_id, "title": None}, {"$set": {"title": title}})
    return result.modified_count > 0


async def create_conversation(user_id: str, title: str | None = None) -> dict:
    doc = {"_id": uuid.uuid4().hex, "user_id": user_id, "title": title, "created_at": now()}
    await conversations.insert_one(doc)
    return _conversation_out(doc)


async def list_conversations(user_id: str) -> list[dict]:
    cursor = conversations.find({"user_id": user_id}).sort("created_at", DESCENDING)
    return [_conversation_out(doc) async for doc in cursor]


async def get_conversation(user_id: str, conversation_id: str) -> dict | None:
    doc = await conversations.find_one({"_id": conversation_id, "user_id": user_id})
    return _conversation_out(doc) if doc else None


async def rename_conversation(user_id: str, conversation_id: str, title: str | None) -> dict | None:
    doc = await conversations.find_one_and_update(
        {"_id": conversation_id, "user_id": user_id},
        {"$set": {"title": title}},
        return_document=ReturnDocument.AFTER,
    )
    return _conversation_out(doc) if doc else None


async def delete_conversation(user_id: str, conversation_id: str) -> bool:
    result = await conversations.delete_one({"_id": conversation_id, "user_id": user_id})
    if result.deleted_count == 0:
        return False
    await exchanges.delete_many({"conversation_id": conversation_id, "user_id": user_id})
    return True


# ---- exchanges ----

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

# ---- facts ----

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


# ---- users & sessions ----

def _user_out(doc: dict) -> dict:
    return {"id": doc["_id"], "username": doc["username"]}


async def create_user(username: str, password_hash: str) -> dict | None:
    doc = {"_id": uuid.uuid4().hex, "username": username,
           "password_hash": password_hash, "created_at": now()}
    try:
        await users.insert_one(doc)
    except DuplicateKeyError:
        return None
    return _user_out(doc)


async def get_user_with_hash(username: str) -> dict | None:
    doc = await users.find_one({"username": username})
    return {**_user_out(doc), "password_hash": doc["password_hash"]} if doc else None


async def create_session(token_hash: str, user_id: str, expires_at: datetime):
    await sessions.insert_one({"_id": token_hash, "user_id": user_id, "expires_at": expires_at})


async def get_session_user(token_hash: str) -> dict | None:
    session = await sessions.find_one({"_id": token_hash, "expires_at": {"$gt": now()}})
    if not session:
        return None
    doc = await users.find_one({"_id": session["user_id"]})
    return _user_out(doc) if doc else None


async def delete_session(token_hash: str):
    await sessions.delete_one({"_id": token_hash})


# ---- keyword search (hybrid retrieval) ----

async def _keyword_search(coll, user_id: str, query: str, embed_model: str, limit: int) -> list[dict]:
    """Text-index matches with their raw text score and stored embedding, best first."""
    cursor = coll.find(
        {"$text": {"$search": query}, "user_id": user_id, "embed_model": embed_model},
        {"history": 0, "keyword_score": {"$meta": "textScore"}},
    ).sort([("keyword_score", {"$meta": "textScore"})]).limit(limit)
    return [doc async for doc in cursor]


async def keyword_search_exchanges(user_id: str, query: str, embed_model: str, limit: int) -> list[dict]:
    out = []
    for doc in await _keyword_search(exchanges, user_id, query, embed_model, limit):
        item = _exchange_out(doc)
        item["keyword_score"], item["embedding"] = doc["keyword_score"], doc["embedding"]
        out.append(item)
    return out


async def keyword_search_facts(user_id: str, query: str, embed_model: str, limit: int) -> list[dict]:
    out = []
    for doc in await _keyword_search(facts, user_id, query, embed_model, limit):
        item = _fact_out(doc)
        item["keyword_score"], item["embedding"] = doc["keyword_score"], doc["embedding"]
        out.append(item)
    return out


# ---- push subscriptions (one per device; the push service's endpoint URL is the id) ----

async def save_push_subscription(user_id: str, endpoint: str, keys: dict, user_agent: str) -> None:
    # Upsert: re-subscribing the same device (or a different user on it) replaces the record
    await push_subscriptions.update_one(
        {"_id": endpoint},
        {"$set": {"user_id": user_id, "keys": keys, "user_agent": user_agent, "updated_at": now()},
         "$setOnInsert": {"created_at": now()}},
        upsert=True,
    )


async def list_push_subscriptions(user_id: str) -> list[dict]:
    return [doc async for doc in push_subscriptions.find({"user_id": user_id})]


async def delete_push_subscription(endpoint: str, user_id: str | None = None) -> bool:
    query = {"_id": endpoint} if user_id is None else {"_id": endpoint, "user_id": user_id}
    return (await push_subscriptions.delete_one(query)).deleted_count > 0


# ---- morning briefs (one record per user per local date; its _id makes it unique) ----

async def claim_brief(user_id: str, date: str) -> bool:
    """Claims today's brief for this user. False if one was already started or sent."""
    try:
        await briefs.insert_one({"_id": f"{user_id}:{date}", "user_id": user_id, "date": date,
                                 "status": "creating", "created_at": now()})
        return True
    except DuplicateKeyError:
        return False


async def get_brief(user_id: str, date: str) -> dict | None:
    return await briefs.find_one({"_id": f"{user_id}:{date}"})


async def finish_brief(user_id: str, date: str, conversation_id: str) -> None:
    await briefs.update_one({"_id": f"{user_id}:{date}"},
                            {"$set": {"status": "sent", "conversation_id": conversation_id, "sent_at": now()}})


async def release_brief(user_id: str, date: str) -> None:
    """Drops a claim whose brief failed before anything was sent, so it can be retried."""
    await briefs.delete_one({"_id": f"{user_id}:{date}", "status": "creating"})
