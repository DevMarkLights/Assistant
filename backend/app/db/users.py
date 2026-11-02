# Users, login sessions, and per-user settings (morning brief switch, cached timezone).
import uuid
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from .client import now, sessions, users

__all__ = ['create_user', 'get_user_with_hash', 'create_session', 'get_timezone_cache', 'set_timezone_cache', 'get_brief_enabled', 'set_brief_enabled', 'brief_recipients', 'get_session_user', 'delete_session']


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


async def get_timezone_cache(user_id: str) -> tuple[str | None, str | None]:
    """(timezone, the location it was looked up for) saved on the user, if any."""
    doc = await users.find_one({"_id": user_id}, {"timezone": 1, "timezone_location": 1}) or {}
    return doc.get("timezone"), doc.get("timezone_location")


async def set_timezone_cache(user_id: str, timezone_name: str, location: str) -> None:
    await users.update_one({"_id": user_id}, {"$set": {"timezone": timezone_name, "timezone_location": location}})


async def get_brief_enabled(user_id: str) -> bool | None:
    """The user's own morning-brief setting, or None if they never changed it."""
    doc = await users.find_one({"_id": user_id}, {"brief_enabled": 1})
    return doc.get("brief_enabled") if doc else None


async def set_brief_enabled(user_id: str, enabled: bool) -> None:
    await users.update_one({"_id": user_id}, {"$set": {"brief_enabled": enabled}})


async def brief_recipients(default_usernames: set[str]) -> list[dict]:
    """Users who switched the brief on, plus default users who never changed the setting."""
    cursor = users.find({"$or": [
        {"brief_enabled": True},
        {"brief_enabled": {"$exists": False}, "username": {"$in": sorted(default_usernames)}},
    ]}, {"username": 1})
    return [{"id": doc["_id"], "username": doc["username"]} async for doc in cursor]


async def get_session_user(token_hash: str) -> dict | None:
    session = await sessions.find_one({"_id": token_hash, "expires_at": {"$gt": now()}})
    if not session:
        return None
    doc = await users.find_one({"_id": session["user_id"]})
    return _user_out(doc) if doc else None


async def delete_session(token_hash: str):
    await sessions.delete_one({"_id": token_hash})
