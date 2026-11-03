# MongoDB connection, collection handles, and index setup. The functions for each collection live
# in the sibling modules (conversations.py, facts.py, ...); app/db/__init__.py re-exports them, so
# callers just use `from app import db` and `db.add_fact(...)`. Similarity search uses Atlas Vector
# Search ($vectorSearch); keyword search uses plain MongoDB text indexes.
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from app.core.config import MONGODB_DB, MONGODB_URI

client = AsyncMongoClient(MONGODB_URI, tz_aware=True)
database = client[MONGODB_DB]
conversations = database["conversations"]
exchanges = database["exchanges"]
facts = database["facts"]
users = database["users"]
sessions = database["sessions"]
push_subscriptions = database["push_subscriptions"]
briefs = database["briefs"]
reminders = database["reminders"]
brief_sections = database["brief_sections"]

VECTOR_INDEX = "exchange_vector_index"
FACT_VECTOR_INDEX = "fact_vector_index"


def now() -> datetime:
    return datetime.now(timezone.utc)


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
    await reminders.create_index([("status", ASCENDING), ("due_at", ASCENDING)])
    await reminders.create_index([("user_id", ASCENDING), ("status", ASCENDING), ("due_at", ASCENDING)])
    await brief_sections.create_index([("user_id", ASCENDING), ("position", ASCENDING)])

async def close_db():
    await client.close()


async def keyword_search(coll, user_id: str, query: str, embed_model: str, limit: int) -> list[dict]:
    """Text-index matches with their raw text score and stored embedding, best first."""
    cursor = coll.find(
        {"$text": {"$search": query}, "user_id": user_id, "embed_model": embed_model},
        {"history": 0, "keyword_score": {"$meta": "textScore"}},
    ).sort([("keyword_score", {"$meta": "textScore"})]).limit(limit)
    return [doc async for doc in cursor]
