# Reminders: due_at in UTC; status goes pending -> sending -> sent, or cancelled.
import uuid
from datetime import datetime

from pymongo import ASCENDING, ReturnDocument

from .client import now, reminders

__all__ = ['add_reminder', 'list_pending_reminders', 'cancel_reminder', 'claim_due_reminder', 'mark_reminder_sent']


async def add_reminder(user_id: str, conversation_id: str, text: str, due_at: datetime) -> str:
    doc = {"_id": uuid.uuid4().hex, "user_id": user_id, "conversation_id": conversation_id, "text": text,
           "due_at": due_at, "status": "pending", "created_at": now()}
    await reminders.insert_one(doc)
    return doc["_id"]


async def list_pending_reminders(user_id: str, until: datetime | None = None) -> list[dict]:
    query = {"user_id": user_id, "status": "pending"}
    if until:
        query["due_at"] = {"$lte": until}
    return [doc async for doc in reminders.find(query).sort("due_at", ASCENDING)]


async def cancel_reminder(user_id: str, reminder_id: str) -> bool:
    result = await reminders.update_one({"_id": reminder_id, "user_id": user_id, "status": "pending"},
                                        {"$set": {"status": "cancelled", "cancelled_at": now()}})
    return result.modified_count > 0


async def claim_due_reminder(at: datetime) -> dict | None:
    """Atomically moves one due reminder from pending to sending and returns it. Only one caller
    can ever claim a given reminder, so it is sent at most once."""
    return await reminders.find_one_and_update(
        {"status": "pending", "due_at": {"$lte": at}},
        {"$set": {"status": "sending", "claimed_at": now()}},
        sort=[("due_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


async def mark_reminder_sent(reminder_id: str, devices: int) -> None:
    await reminders.update_one({"_id": reminder_id},
                               {"$set": {"status": "sent", "sent_at": now(), "devices": devices}})
