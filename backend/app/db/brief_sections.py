# Morning brief sections: what each user's brief contains, in order. A section is data, not code:
# a title, a source ({"type": "feed", "url": ...}, {"type": "weather"}, ...) and optional
# instructions for how to write it. app/services/brief_sections.py fetches and validates them.
import uuid

from pymongo import ASCENDING

from .client import brief_sections, now, users

__all__ = ['list_brief_sections', 'insert_brief_section', 'update_brief_section', 'delete_brief_section',
           'set_brief_section_order', 'brief_sections_seeded', 'mark_brief_sections_seeded']


def _section_out(doc: dict) -> dict:
    return {"id": doc["_id"], "title": doc["title"], "source": doc["source"],
            "instructions": doc.get("instructions") or ""}


async def list_brief_sections(user_id: str) -> list[dict]:
    cursor = brief_sections.find({"user_id": user_id}).sort("position", ASCENDING)
    return [_section_out(doc) async for doc in cursor]


async def insert_brief_section(user_id: str, title: str, source: dict, instructions: str = "") -> dict:
    """Adds a section at the end; use set_brief_section_order to move it."""
    last = await brief_sections.find_one({"user_id": user_id}, sort=[("position", -1)])
    doc = {"_id": uuid.uuid4().hex, "user_id": user_id, "title": title, "source": source,
           "instructions": instructions, "position": (last["position"] + 1) if last else 0, "created_at": now()}
    await brief_sections.insert_one(doc)
    return _section_out(doc)


async def update_brief_section(user_id: str, section_id: str, fields: dict) -> None:
    await brief_sections.update_one({"_id": section_id, "user_id": user_id}, {"$set": fields})


async def delete_brief_section(user_id: str, section_id: str) -> None:
    await brief_sections.delete_one({"_id": section_id, "user_id": user_id})


async def set_brief_section_order(user_id: str, section_ids: list[str]) -> None:
    for position, section_id in enumerate(section_ids):
        await brief_sections.update_one({"_id": section_id, "user_id": user_id}, {"$set": {"position": position}})


async def brief_sections_seeded(user_id: str) -> bool:
    """Whether the user has been given the default sections (so removing them all sticks)."""
    return bool(await users.count_documents({"_id": user_id, "brief_sections_seeded": True}, limit=1))


async def mark_brief_sections_seeded(user_id: str) -> None:
    await users.update_one({"_id": user_id}, {"$set": {"brief_sections_seeded": True}})
