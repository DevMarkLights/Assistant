# Each user's location and timezone. The location is the user's own "lives in ..." fact; its
# timezone comes from Open-Meteo's geocoder and is cached on the user document, re-looked-up only
# when the location changes. Without a location (or if the lookup fails) USER_TIMEZONE is used.
# Datetimes are stored in UTC everywhere; this timezone is for prompts, display, and scheduling.
import re
from zoneinfo import ZoneInfo

from app import db
from app.services import weather
from app.core.config import USER_TIMEZONE
from app.core.logs import get_logger

log = get_logger("usertime")

LIVES_IN = re.compile(r"lives in (.+?)\.?$", re.IGNORECASE)


def default_location() -> str:
    """The city in USER_TIMEZONE, e.g. "America/New_York" -> "New York"."""
    return USER_TIMEZONE.rsplit("/", 1)[-1].replace("_", " ")


async def user_location(user_id: str, facts: list[dict] | None = None) -> str | None:
    """The place from the user's "lives in ..." fact, if they have one. Pass `facts` (all of the
    user's facts) if they're already loaded, to save a database round trip."""
    for fact in facts if facts is not None else await db.list_facts(user_id):
        if match := LIVES_IN.search(fact["text"]):
            return match.group(1)
    return None


async def user_timezone(user_id: str, facts: list[dict] | None = None) -> str:
    """IANA timezone name for the user. Never raises."""
    try:
        location = await user_location(user_id, facts)
        if not location:
            return USER_TIMEZONE
        cached, cached_for = await db.get_timezone_cache(user_id)
        if cached and cached_for == location:
            return cached
        try:
            geo = await weather.lookup(location)
        except Exception:
            # Geocoder down: keep the last known timezone rather than jumping to the default
            log.warning("timezone lookup for %r failed; using %s", location, cached or USER_TIMEZONE, exc_info=True)
            return cached or USER_TIMEZONE
        name = (geo or {}).get("timezone")
        if name:
            ZoneInfo(name)  # make sure Python knows it before saving
            await db.set_timezone_cache(user_id, name, location)
            log.info("timezone for user %s is %s (from %r)", user_id, name, location)
            return name
        log.warning("couldn't find a timezone for %r; using %s", location, cached or USER_TIMEZONE)
        return cached or USER_TIMEZONE
    except Exception:
        log.warning("timezone lookup failed for user %s; using %s", user_id, USER_TIMEZONE, exc_info=True)
        return USER_TIMEZONE


async def user_zone(user_id: str, facts: list[dict] | None = None) -> ZoneInfo:
    return ZoneInfo(await user_timezone(user_id, facts))
