# Notable LLM routing events (429s, failovers, sidelined providers, "all models busy", replies cut
# off mid-stream), for the owner's usage page and the morning brief's usage line. A capped
# collection: MongoDB keeps only the newest ~1000, so it never grows.
from datetime import datetime

from .client import llm_events, now

__all__ = ['add_llm_event', 'recent_llm_events', 'llm_events_between']


def _event_out(doc: dict) -> dict:
    return {"at": doc["at"].isoformat(), "kind": doc["kind"], "task": doc.get("task"),
            "model": doc.get("model"), "detail": doc.get("detail", "")}


async def add_llm_event(kind: str, task: str | None, model: str | None, detail: str = "") -> None:
    await llm_events.insert_one({"at": now(), "kind": kind, "task": task, "model": model, "detail": detail})


async def recent_llm_events(limit: int = 50) -> list[dict]:
    """Newest first."""
    return [_event_out(doc) async for doc in llm_events.find().sort("$natural", -1).limit(limit)]


async def llm_events_between(start: datetime, end: datetime) -> list[dict]:
    return [_event_out(doc) async for doc in llm_events.find({"at": {"$gte": start, "$lt": end}})]
