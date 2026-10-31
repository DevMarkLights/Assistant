# Morning brief: once a day, gather the date, weather, Pi health, and today's reminders, have the
# background model write a short brief, save it as a new conversation, and push a one-line summary
# that opens it. One brief per user per local date (db2.claim_brief), even across restarts.
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

import db2
import homelab
import push
import weather
from auth import current_user
from config import BRIEF_USERS, HOMELAB_USERS, MEMORY_MODEL, USER_LOCATION, USER_TIMEZONE
from llm import complete_json
from logs import get_logger

log = get_logger("brief")

router = APIRouter(prefix="/Assistant/api/brief", tags=["brief"])

BRIEF_PROMPT = """You write a user's morning brief from the notes below. Be warm and concise: under
120 words, plain sentences or a few short bullet points in Markdown. Cover the weather (say if they
need an umbrella or a jacket), anything wrong with their home server, and today's reminders. Don't
invent anything that isn't in the notes; leave out a section with nothing in it.
Respond with JSON only: {"brief": "<the brief>", "summary": "<one line under 90 characters for a phone notification>"}"""

LIVES_IN = re.compile(r"lives in (.+?)\.?$", re.IGNORECASE)


def local_now() -> datetime:
    return datetime.now(ZoneInfo(USER_TIMEZONE))


async def reminders_today(user_id: str, now: datetime) -> list[str]:
    """Reminders due today, as display lines. Filled in by Stage 3 (reminders)."""
    return []


async def _location(user_id: str) -> str | None:
    if USER_LOCATION:
        return USER_LOCATION
    for fact in await db2.list_facts(user_id):
        if match := LIVES_IN.search(fact["text"]):
            return match.group(1)
    return None


async def _pi_health() -> str:
    report = await homelab.report()
    lines = report.splitlines()
    overall = next((l for l in lines if l.startswith("Overall:")), "")
    if overall == "Overall: healthy":
        watching = next((l for l in lines if l.startswith("Worth watching:")), "")
        return "all healthy." + (f" {watching}" if watching else "")
    return f"problems: {overall.removeprefix('Overall: ')}"


async def gather(user: dict, now: datetime) -> dict:
    """Everything the brief needs, as short notes. Each part fails on its own without stopping the rest."""
    notes = {"date": now.strftime("%A, %B %-d, %Y")}
    location = await _location(user["id"])
    if location:
        try:
            notes["weather"] = await weather.forecast(location, days=1)
        except Exception:
            log.warning("brief weather failed for %s", location, exc_info=True)
            notes["weather"] = "Weather is unavailable right now."
    else:
        notes["weather"] = "Location unknown (set USER_LOCATION, or tell the assistant where you live)."
    if user["username"].lower() in HOMELAB_USERS:
        try:
            notes["home_server"] = await _pi_health()
        except Exception:
            log.warning("brief Pi health failed", exc_info=True)
            notes["home_server"] = "Couldn't check the home server."
    notes["reminders"] = await reminders_today(user["id"], now)
    return notes


def _fallback(notes: dict) -> dict:
    """A plain brief for when the model is unavailable (e.g. rate-limited)."""
    weather_lines = notes["weather"].splitlines()
    parts = [f"Good morning! It's {notes['date']}."]
    # "Now: ..." and today's "- <date>: ..." forecast line, or the single line of an error message
    parts += [line.removeprefix("- ") for line in weather_lines if line.startswith(("Now:", "- "))] or weather_lines[:1]
    if "home_server" in notes:
        parts.append(f"Home server: {notes['home_server']}")
    if notes["reminders"]:
        parts.append("Today: " + "; ".join(notes["reminders"]))
    return {"brief": "\n\n".join(parts), "summary": f"Your brief for {notes['date']}"}


async def write(notes: dict) -> dict:
    reminders = "\n".join(f"- {r}" for r in notes["reminders"]) or "(none)"
    text = (f"Date: {notes['date']}\n\nWeather:\n{notes['weather']}\n\n"
            + (f"Home server: {notes['home_server']}\n\n" if "home_server" in notes else "")
            + f"Today's reminders:\n{reminders}")
    try:
        data = await complete_json([{"role": "system", "content": BRIEF_PROMPT},
                                    {"role": "user", "content": text}], model=MEMORY_MODEL, purpose="brief")
        brief, summary = str(data.get("brief") or "").strip(), str(data.get("summary") or "").strip()
        if brief:
            return {"brief": brief, "summary": summary[:120] or f"Your brief for {notes['date']}"}
        log.warning("brief model returned no text; using the plain brief")
    except Exception:
        log.warning("brief model failed; using the plain brief", exc_info=True)
    return _fallback(notes)


async def run_for_user(user: dict, force: bool = False) -> dict:
    """Creates and pushes today's brief. Returns {"status", "conversation_id"}; never raises."""
    now = local_now()
    date = now.strftime("%Y-%m-%d")
    if not await db2.claim_brief(user["id"], date):
        existing = await db2.get_brief(user["id"], date) or {}
        if not force:
            log.info("brief for %s on %s already %s", user["username"], date, existing.get("status"))
            return {"status": "already_sent", "conversation_id": existing.get("conversation_id")}
        log.info("forcing a new brief for %s on %s (testing)", user["username"], date)
    try:
        written = await write(await gather(user, now))
        conversation = await db2.create_conversation(user["id"], title=f"Morning brief, {now:%b %-d}")
        await db2.add_exchange(user["id"], conversation["id"], "", written["brief"], None, "none")
    except Exception:
        log.exception("brief for %s failed before sending; it can be retried", user["username"])
        await db2.release_brief(user["id"], date)
        return {"status": "failed", "conversation_id": None}
    # From here on the brief counts as sent, so a crash can't cause a second one
    await db2.finish_brief(user["id"], date, conversation["id"])
    await push.send_push(user["id"], "Morning brief", written["summary"], f"/Assistant/?c={conversation['id']}")
    log.info("sent morning brief to %s: %s", user["username"], written["summary"])
    return {"status": "sent", "conversation_id": conversation["id"]}


async def run_scheduled() -> None:
    """The daily job: briefs for everyone in BRIEF_USERS. Never raises."""
    for username in sorted(BRIEF_USERS):
        try:
            user = await db2.users.find_one({"username": username})
            if not user:
                log.warning("BRIEF_USERS lists %r, but there's no such account", username)
                continue
            await run_for_user({"id": user["_id"], "username": user["username"]})
        except Exception:
            log.exception("scheduled brief for %s failed", username)


@router.post("/run")
async def run_now(force: bool = Query(False, description="Testing only: create another brief even if today's exists"),
                  user: dict = Depends(current_user)):
    """Creates today's brief for the logged-in user now (for testing). Idempotent unless force=true."""
    return await run_for_user(user, force=force)
