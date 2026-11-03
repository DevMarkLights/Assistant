# Morning brief: once a day, gather the user's brief sections (weather, feeds, searches, reminders,
# server health; see brief_sections.py, which users change by asking in chat), have the background
# model write a short brief, save it as a new conversation, and push a one-line summary that opens
# it. Each user gets it at MORNING_BRIEF_TIME in their own timezone (usertime.py), and at most one
# per local date (db.claim_brief), even across restarts.
from datetime import datetime, timedelta

from app import db
from app.core.config import BRIEF_USERS, MEMORY_MODEL, MORNING_BRIEF_TIME
from app.core.logs import get_logger
from app.llm.client import complete_json
from app.services import brief_sections, push, usertime

log = get_logger("brief")

BRIEF_PROMPT = """You write a user's morning brief from the sections below. Start with a one-line
greeting that includes the date, then cover each section in the order given, under a short bold
heading, following its "How to write it" note (by default one to three short sentences or bullets).
For weather, say if they need an umbrella or a jacket. Every paper, story or search result you
mention keeps its link: write its title as the Markdown link given in the notes, [Title](url). Don't invent anything that isn't in the notes. A section with nothing in it gets one
short line saying so, or is left out if it's reminders or the home server. Be concise.
Respond with JSON only: {"brief": "<the brief, Markdown>", "summary": "<one line under 90 characters for a phone notification>"}"""

CATCH_UP = timedelta(hours=3)  # a brief that's due but missed (server down at 7:00) still goes out until then


def brief_time() -> tuple[int, int]:
    hour, minute = MORNING_BRIEF_TIME.split(":")
    return int(hour), int(minute)


def in_brief_window(now: datetime) -> bool:
    """True if `now` (in the user's timezone) is past their brief time today, but within CATCH_UP of it."""
    hour, minute = brief_time()
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return scheduled <= now <= scheduled + CATCH_UP


async def gather(user: dict, now: datetime) -> dict:
    """The date plus each brief section's notes, in order; `now` is in the user's timezone. Each
    section fails on its own without stopping the rest."""
    sections = await brief_sections.sections_for(user["id"], user["username"])
    gathered = []
    for section in sections:  # one at a time: Brave allows one search per second
        gathered.append({"title": section["title"], "type": section["source"]["type"],
                         "instructions": section["instructions"],
                         "data": await brief_sections.data_for(section, user, now)})
    return {"date": now.strftime("%A, %B %-d, %Y"), "sections": gathered}


def _fallback(notes: dict) -> dict:
    """A plain brief for when the model is unavailable (e.g. rate-limited): each section's first
    few lines, as they came."""
    parts = [f"Good morning! It's {notes['date']}."]
    for section in notes["sections"]:
        if section["type"] in ("reminders", "homelab") and section["data"].startswith("("):
            continue  # "(none today)", "(not available)": nothing worth a heading
        lines = section["data"].splitlines()
        if section["type"] == "weather":  # "Now: ..." and today's "- <date>: ..." forecast line
            lines = [l.removeprefix("- ") for l in lines if l.startswith(("Now:", "- "))] or lines[:1]
        else:
            lines = [l for l in lines if not l.startswith("  ")][:3]  # skip feed summaries
        parts.append(f"**{section['title']}**\n" + "\n".join(lines))
    return {"brief": "\n\n".join(parts), "summary": f"Your brief for {notes['date']}"}


def notes_text(notes: dict) -> str:
    """The gathered sections as the text the model writes from."""
    return f"Date: {notes['date']}\n\n" + "\n\n".join(
        f"## {s['title']}\nHow to write it: {s['instructions'] or 'briefly'}\n{s['data']}" for s in notes["sections"])


async def write(notes: dict) -> dict:
    text = notes_text(notes)
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
    now = datetime.now(await usertime.user_zone(user["id"]))
    date = now.strftime("%Y-%m-%d")  # the user's local date
    if not await db.claim_brief(user["id"], date):
        existing = await db.get_brief(user["id"], date) or {}
        if not force:
            log.info("brief for %s on %s already %s", user["username"], date, existing.get("status"))
            return {"status": "already_sent", "conversation_id": existing.get("conversation_id")}
        log.info("forcing a new brief for %s on %s (testing)", user["username"], date)
    try:
        written = await write(await gather(user, now))
        conversation = await db.create_conversation(user["id"], title=f"Morning brief, {now:%b %-d}")
        await db.add_exchange(user["id"], conversation["id"], "", written["brief"], None, "none")
    except Exception:
        log.exception("brief for %s failed before sending; it can be retried", user["username"])
        await db.release_brief(user["id"], date)
        return {"status": "failed", "conversation_id": None}
    # From here on the brief counts as sent, so a crash can't cause a second one
    await db.finish_brief(user["id"], date, conversation["id"])
    await push.send_push(user["id"], "Morning brief", written["summary"], f"/Assistant/?c={conversation['id']}")
    log.info("sent morning brief to %s: %s", user["username"], written["summary"])
    return {"status": "sent", "conversation_id": conversation["id"]}


async def is_enabled(user: dict) -> bool:
    """The user's own setting if they've set one; otherwise on for BRIEF_USERS."""
    setting = await db.get_brief_enabled(user["id"])
    return setting if setting is not None else user["username"].lower() in BRIEF_USERS


async def run_due() -> None:
    """Runs every few minutes: sends the brief to each user whose local time is in their brief
    window and who hasn't had today's yet. Never raises."""
    try:
        recipients = await db.brief_recipients(BRIEF_USERS)
    except Exception:
        log.exception("couldn't load brief recipients")
        return
    for user in recipients:
        try:
            now = datetime.now(await usertime.user_zone(user["id"]))
            if in_brief_window(now) and not await db.get_brief(user["id"], now.strftime("%Y-%m-%d")):
                await run_for_user(user)
        except Exception:
            log.exception("scheduled brief for %s failed", user["username"])
