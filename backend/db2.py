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

async def close_db():
    await client.close()


# ---- conversations ----

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
                       embedding: list[float], embed_model: str) -> str:
    doc = {
        "_id": uuid.uuid4().hex,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "embedding": embedding,
        "embed_model": embed_model,
        "created_at": now(),
    }
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
