# LLM usage for the server's owner: each model's budget and usage today, cooldowns, each task's
# model order, and recent 429s, failovers and other routing events. Other users get a 403: budgets
# are shared server details, like the homelab stats.
from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.api.auth import current_user, is_owner
from app.llm import router as llm_router

router = APIRouter(prefix="/Assistant/api/usage", tags=["usage"])


@router.get("")
async def usage(user: dict = Depends(current_user)):
    if not is_owner(user):
        raise HTTPException(status_code=403, detail="Only the server's owner can see LLM usage")
    if llm_router.limiter is None:
        return {"local": True}  # LOCAL=true: everything runs on Ollama, nothing to route
    state = llm_router.status()
    # Today's totals from Mongo cover every server sharing the database (the Mac and the Pi); the
    # per-minute numbers and cooldowns are this server's own
    today = await db.llm_usage_for_day(state["day"])
    for model in state["models"]:
        model["used_today"] = today.get(model["key"], {"requests": 0, "tokens": 0, "audio_seconds": 0.0})
    state["events"] = await db.recent_llm_events(50)
    return state
