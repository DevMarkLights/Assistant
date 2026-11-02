# Conversations: one per chat in the sidebar.
import uuid

from pymongo import DESCENDING, ReturnDocument

from .client import conversations, exchanges, now

__all__ = ['set_title_if_empty', 'create_conversation', 'list_conversations', 'get_conversation', 'rename_conversation', 'delete_conversation']


def _conversation_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "title": doc.get("title"),
        "created_at": doc["created_at"].isoformat(),
    }


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
