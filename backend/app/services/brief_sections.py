# What goes in each user's morning brief: an ordered list of sections the user manages by asking
# in chat (tools/brief.py), stored in the brief_sections collection. A section is data, not code,
# so new sections never need a developer: a few general source types cover almost anything.
#   weather    {"location"?}  forecast for a place (default: where the user lives)
#   feed       {"url", "limit"?}  any RSS/Atom feed: arXiv, news, blogs, subreddits, YouTube...
#   search     {"query"}      a Brave web search, e.g. "Raspberry Pi news this week"
#   reminders  {}             today's reminders
#   homelab    {}             home server health (HOMELAB_USERS only: it exposes the host)
# Users start with weather, reminders and (for the owner) the server; removing them all sticks.
import re
from datetime import datetime

from app import db
from app.core.config import BRAVE_API_KEY, HOMELAB_USERS
from app.core.logs import get_logger
from app.services import feeds, homelab, reminders, usertime, weather, websearch

log = get_logger("brief")

SOURCE_TYPES = ("weather", "feed", "search", "reminders", "homelab")
MAX_SECTIONS = 10
DATA_CHARS = 2000  # cap on each section's data in the brief prompt (the free tier's tokens/min is small)
FEED_LIMIT = 8  # feed items per section unless the section asks for fewer or more (max 15)


def allowed_types(username: str) -> list[str]:
    types = [t for t in SOURCE_TYPES if t != "homelab" or username.lower() in HOMELAB_USERS]
    return [t for t in types if t != "search" or BRAVE_API_KEY]


def _defaults(username: str) -> list[tuple[str, dict]]:
    sections = [("Weather", {"type": "weather"}), ("Reminders", {"type": "reminders"})]
    if username.lower() in HOMELAB_USERS:
        sections.append(("Home server", {"type": "homelab"}))
    return sections


async def sections_for(user_id: str, username: str) -> list[dict]:
    """The user's sections, in order; gives them the defaults the first time."""
    if not await db.brief_sections_seeded(user_id):
        if not await db.list_brief_sections(user_id):
            for title, source in _defaults(username):
                await db.insert_brief_section(user_id, title, source)
        await db.mark_brief_sections_seeded(user_id)
    return await db.list_brief_sections(user_id)


def describe(section: dict) -> str:
    """One line for listing: 'New AI papers (feed: https://...) - pick the 3 best'."""
    source = section["source"]
    detail = source.get("url") or source.get("query") or source.get("location") or ""
    line = f"{section['title']} ({source['type']}{': ' + detail if detail else ''})"
    return line + (f" - {section['instructions']}" if section["instructions"] else "")


async def check_source(username: str, source: dict) -> tuple[str, str]:
    """Validates a new section's source before it's saved. Returns ("Error: ...", "") for the model,
    or ("", note) where note confirms what it found; feeds are fetched once to be sure they work."""
    kind = source.get("type")
    if kind not in allowed_types(username):
        return f"Error: source must be one of {', '.join(allowed_types(username))}", ""
    if kind == "feed":
        if not source.get("url"):
            return "Error: a feed section needs the feed's url", ""
        try:
            title, items = await feeds.read(source["url"])
        except feeds.FeedError as e:
            return f"Error: {e}", ""
        if not items:  # valid but empty for now, e.g. arXiv on weekends
            return "", f"{title or 'The feed'} works but has no items right now"
        return "", f"{title or 'The feed'} has {len(items)} items now, e.g. {items[0]['title']!r}"
    if kind == "search" and not source.get("query"):
        return "Error: a search section needs a query", ""
    if kind == "weather" and source.get("location") and not await weather.lookup(source["location"]):
        return f"Error: couldn't find a place called {source['location']!r}", ""
    return "", ""


async def data_for(section: dict, user: dict, now: datetime) -> str:
    """The section's content for today's brief, as plain notes. Failures become a short note so one
    broken source doesn't stop the brief."""
    source = section["source"]
    kind = source["type"]
    try:
        if kind == "weather":
            location = source.get("location") or await usertime.user_location(user["id"]) or usertime.default_location()
            text = await weather.forecast(location, days=1)
        elif kind == "feed":
            _, items = await feeds.read(source["url"])
            text = feeds.format_items(items, max(1, min(int(source.get("limit") or FEED_LIMIT), 15))) or "(no items)"
        elif kind == "search":
            text = await websearch.search(source["query"])
        elif kind == "reminders":
            text = "\n".join(f"- {line}" for line in await reminders.today_lines(user["id"], now, now.tzinfo)) or "(none today)"
        elif kind == "homelab":
            # Checked here as well as when added: server details are private to HOMELAB_USERS
            if user["username"].lower() not in HOMELAB_USERS:
                return "(not available)"
            text = await homelab_summary()
        else:
            return f"(unknown source type {kind!r})"
    except Exception as e:
        log.warning("brief section %r failed", section["title"], exc_info=True)
        return f"(couldn't load this today: {type(e).__name__})"
    return text if len(text) <= DATA_CHARS else text[:DATA_CHARS].rsplit("\n", 1)[0] + "\n(more not shown)"


async def homelab_summary() -> str:
    lines = (await homelab.report()).splitlines()
    overall = next((l for l in lines if l.startswith("Overall:")), "")
    if overall == "Overall: healthy":
        watching = next((l for l in lines if l.startswith("Worth watching:")), "")
        return "All healthy." + (f" {watching}" if watching else "")
    return f"Problems: {overall.removeprefix('Overall: ')}"


def find(sections: list[dict], description: str) -> tuple[dict | None, list[dict]]:
    """The section the user means: a position number ("2"), the exact title, or the only title that
    matches by words. Returns (section, []) or (None, candidates to ask about)."""
    text = description.strip().lower()
    if text.isdigit() and 1 <= int(text) <= len(sections):
        return sections[int(text) - 1], []
    for section in sections:
        if section["title"].lower() == text:
            return section, []
    words = set(re.findall(r"[a-z0-9]+", text)) - {"the", "a", "my", "section", "brief", "morning"}
    scored = [(len(words & set(re.findall(r"[a-z0-9]+", describe(s).lower()))), s) for s in sections]
    best = max((score for score, _ in scored), default=0)
    matches = [s for score, s in scored if score == best and best > 0]
    return (matches[0], []) if len(matches) == 1 else (None, matches or sections)
