# Morning briefs: one record per user per local date (its _id makes it unique), so a brief is
# never sent twice.
from pymongo.errors import DuplicateKeyError

from .client import briefs, now

__all__ = ['claim_brief', 'get_brief', 'finish_brief', 'release_brief']


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
