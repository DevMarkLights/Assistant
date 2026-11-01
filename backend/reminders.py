# Reminders the assistant sets for the user. Stored with due_at in UTC; shown and parsed in the
# user's own timezone (usertime.py), passed in as `tz`.
# A job runs every minute (scheduler.py) and sends what's due. Each reminder is claimed with an
# atomic pending -> sending update before its push goes out, so it can never be sent twice, even
# with two servers running the job.
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db2
import push
from embeddings import cosine_similarity, embed
from logs import get_logger

log = get_logger("reminders")

# cancel_reminder uses the same rule as the forget tool: cancel only a clear best match
CANCEL_CONFIDENT = 0.6
CANCEL_MARGIN = 0.1
# A clock time in a cancel request, e.g. "5pm", "5:30 pm", "17:00" (an hour alone needs am/pm)
CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE)


def mentioned_times(text: str) -> list[tuple[int, int | None]]:
    """(hour 0-23, minute or None) for each clock time in the text."""
    times = []
    for m in CLOCK.finditer(text):
        if m.group(3):
            hour, minute = int(m.group(1)) % 12, m.group(2)
            hour += 12 if m.group(3).lower() == "pm" else 0
        else:
            hour, minute = int(m.group(4)), m.group(5)
        if hour < 24:
            times.append((hour, int(minute) if minute else None))
    return times


def display(dt: datetime, tz: ZoneInfo) -> str:
    """e.g. "Tue Nov 3, 3:00 PM" in the user's timezone."""
    return dt.astimezone(tz).strftime("%a %b %-d, %-I:%M %p")


def parse_due(value: str, now: datetime, tz: ZoneInfo) -> datetime:
    """ISO 8601 -> aware UTC datetime. A time without an offset is taken as the user's timezone.
    Raises ValueError with a message meant for the model."""
    try:
        due = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"due_at {value!r} isn't an ISO 8601 datetime like 2026-11-03T15:00:00-05:00")
    if due.tzinfo is None:
        due = due.replace(tzinfo=tz)
    due = due.astimezone(timezone.utc)
    if due <= now:
        raise ValueError(f"{display(due, tz)} is in the past (it's now {display(now, tz)}). "
                         "Ask the user for a future time.")
    return due


async def create(user_id: str, conversation_id: str, text: str, due_at: str, tz: ZoneInfo) -> str:
    now = datetime.now(timezone.utc)
    try:
        due = parse_due(due_at, now, tz)
    except ValueError as e:
        return f"Error: {e}"
    await db2.add_reminder(user_id, conversation_id, text.strip(), due)
    return f"Reminder set for {display(due, tz)}: {text.strip()}"


async def upcoming_lines(user_id: str, tz: ZoneInfo) -> str:
    pending = await db2.list_pending_reminders(user_id)
    if not pending:
        return "No upcoming reminders."
    # No ids here: whatever the model sees, it may repeat to the user
    return "\n".join(f"- {label(r, tz)}" for r in pending)


def label(reminder: dict, tz: ZoneInfo) -> str:
    """How a reminder is shown and referred to, e.g. "Call mom at Tue Nov 3, 3:00 PM"."""
    return f"{reminder['text']} at {display(reminder['due_at'], tz)}"


async def cancel(user_id: str, tz: ZoneInfo, description: str) -> str:
    pending = await db2.list_pending_reminders(user_id)
    if not pending:
        return "There are no upcoming reminders to cancel."
    if not description.strip():
        return "Error: describe the reminder to cancel, e.g. 'the 3pm stretch reminder'."
    wanted = description.strip().lower()
    exact = [r for r in pending if wanted == label(r, tz).lower()]
    if len(exact) == 1:
        target = exact[0]
    else:
        # "the 5pm one": embeddings can't tell 5pm from 3pm, so narrow by the clock time first
        candidates = pending
        if times := mentioned_times(description):
            at_time = [r for r in pending if any(
                r["due_at"].astimezone(tz).hour == h and (m is None or r["due_at"].astimezone(tz).minute == m)
                for h, m in times)]
            candidates = at_time or pending
        # Then compare meanings on the fly (there are only ever a few reminders)
        query = await embed(description, "query")
        scored = sorted([(cosine_similarity(query, await embed(label(r, tz), "document")), r) for r in candidates],
                        key=lambda pair: pair[0], reverse=True)
        best_score, target = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        # Exactly one reminder at the time they named ("my 10am one") identifies it on its own
        unique_time_match = len(candidates) == 1 and candidates is not pending
        if not unique_time_match and (best_score < CANCEL_CONFIDENT or best_score - runner_up < CANCEL_MARGIN):
            listing = "\n".join(f"- {label(r, tz)}" for _, r in scored[:5])
            return ("Not sure which reminder is meant, so nothing was cancelled. Ask the user which one "
                    "(or whether none of these), then call cancel_reminder again with that line exactly, "
                    f"e.g. \"{label(scored[0][1], tz)}\":\n{listing}")
    if not await db2.cancel_reminder(user_id, target["_id"]):
        return "Error: that reminder was already sent or cancelled."
    return f"Cancelled: {label(target, tz)}"


async def today_lines(user_id: str, now: datetime, tz: ZoneInfo) -> list[str]:
    """Pending reminders due between now and the end of the user's day (for the morning brief)."""
    end_of_day = now.astimezone(tz).replace(hour=23, minute=59, second=59, microsecond=0)
    reminders = await db2.list_pending_reminders(user_id, until=end_of_day.astimezone(timezone.utc))
    return [f"{r['due_at'].astimezone(tz):%-I:%M %p}: {r['text']}" for r in reminders]


async def send_due() -> int:
    """The every-minute job: claims and sends every due reminder. Returns how many it sent."""
    sent = 0
    now = datetime.now(timezone.utc)
    while reminder := await db2.claim_due_reminder(now):
        try:
            url = f"/Assistant/?c={reminder['conversation_id']}" if reminder.get("conversation_id") else "/Assistant/"
            devices = await push.send_push(reminder["user_id"], "Reminder", reminder["text"], url)
            await db2.mark_reminder_sent(reminder["_id"], devices)
            log.info("sent reminder %s to %d device(s): %s", reminder["_id"], devices, reminder["text"])
            sent += 1
        except Exception:
            # Stays "sending": better to miss one reminder than to risk sending it twice
            log.exception("reminder %s failed while sending", reminder["_id"])
    return sent
