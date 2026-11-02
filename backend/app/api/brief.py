# Morning brief settings (per-user on/off switch, shown with the user's brief time and timezone) and a
# manual trigger for testing. The brief itself lives in app/services/brief.py.
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app import db
from app.api.auth import current_user
from app.core.config import MORNING_BRIEF_TIME
from app.core.logs import get_logger
from app.services import brief, usertime

log = get_logger("brief")

router = APIRouter(prefix="/Assistant/api/brief", tags=["brief"])


class BriefSettings(BaseModel):
    enabled: bool


@router.get("/settings")
async def get_settings(user: dict = Depends(current_user)):
    return {
        "enabled": await brief.is_enabled(user),
        "time": MORNING_BRIEF_TIME,
        "timezone": await usertime.user_timezone(user["id"]),
        "location": await usertime.user_location(user["id"]),
    }


@router.put("/settings")
async def update_settings(body: BriefSettings, user: dict = Depends(current_user)):
    await db.set_brief_enabled(user["id"], body.enabled)
    log.info("%s turned the morning brief %s", user["username"], "on" if body.enabled else "off")
    return {"enabled": body.enabled}


@router.post("/run")
async def run_now(force: bool = Query(False, description="Testing only: create another brief even if today's exists"),
                  user: dict = Depends(current_user)):
    """Creates today's brief for the logged-in user now (for testing). Idempotent unless force=true."""
    return await brief.run_for_user(user, force=force)
