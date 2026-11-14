# Daily LLM usage per model: one document per model per UTC date (_id "<model>:<YYYY-MM-DD>"), so
# the rate limiter's daily counts survive restarts. The per-minute and per-hour windows stay in memory.
from .client import llm_usage, now

__all__ = ['add_llm_usage', 'llm_usage_for_day']


async def add_llm_usage(model: str, day: str, requests: int, tokens: int, audio_seconds: float = 0.0) -> None:
    await llm_usage.update_one({"_id": f"{model}:{day}"},
                               {"$inc": {"requests": requests, "tokens": tokens, "audio_seconds": audio_seconds},
                                "$set": {"model": model, "day": day, "updated_at": now()}}, upsert=True)


async def llm_usage_for_day(day: str) -> dict[str, dict]:
    """model -> {"requests", "tokens", "audio_seconds"} for one UTC date."""
    return {doc["model"]: {"requests": doc.get("requests", 0), "tokens": doc.get("tokens", 0),
                           "audio_seconds": round(doc.get("audio_seconds", 0.0), 1)}
            async for doc in llm_usage.find({"day": day})}
