# get_current_time: the date and time in another timezone (the user's own is already in the prompt).
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tools.registry import ToolContext, registry


@registry.tool(
    description="Current date and time, e.g. in another timezone (the user's own is in the prompt).",
    parameters={
        "type": "object",
        "properties": {"timezone": {"type": "string", "description": "IANA name, e.g. 'Europe/London'."}},
    },
    running="Checking the time",
    summarize=lambda result: ("Checked the time", result),
)
async def get_current_time(ctx: ToolContext, timezone: str | None = None) -> str:
    try:
        zone = ZoneInfo(timezone) if timezone else ctx.tz
    except ZoneInfoNotFoundError:
        return f"Error: unknown timezone {timezone!r}. Use an IANA name like 'America/Chicago'."
    return datetime.now(zone).strftime("%A, %B %-d, %Y, %-I:%M %p ") + str(zone)
