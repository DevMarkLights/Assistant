# Reminder tools: remind_me, list_reminders, and cancel_reminder (cancels only a clear match).
from app.services import reminders
from app.tools.registry import ToolContext, registry


def _summarize_cancel(result: str) -> tuple[str, str]:
    if result.startswith("Cancelled:"):
        return "Cancelled a reminder", result.removeprefix("Cancelled: ")
    if result.startswith("Not sure"):
        candidates = [line.removeprefix("- ") for line in result.splitlines() if line.startswith("- ")]
        return "Asked which reminder to cancel", "\n".join(candidates)
    return "No reminder cancelled", result


@registry.tool(
    description="Send the user a reminder notification at a time.",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "e.g. 'Stretch'."},
            "in_minutes": {"type": "number", "description": "Relative times: minutes from now ('in an hour' = 60)."},
            "due_at": {"type": "string", "description": "Specific times: ISO 8601 with offset, e.g. '2026-11-03T15:00:00-05:00'."},
        },
        "required": ["text"],
    },
    running="Setting a reminder",
    summarize=lambda result: ("Reminder set", result.removeprefix("Reminder set for ")),
)
async def remind_me(ctx: ToolContext, text: str, due_at: str | None = None, in_minutes: float | None = None) -> str:
    if not text.strip():
        return "Error: text is empty"
    return await reminders.create(ctx.user_id, ctx.conversation_id, text, ctx.tz, due_at, in_minutes)


@registry.tool(
    description="List the user's upcoming reminders.",
    parameters={"type": "object", "properties": {}},
    running="Checking your reminders",
    summarize=lambda result: ("Checked your reminders", "\n".join(
        line.removeprefix("- ") for line in result.splitlines())),
)
async def list_reminders(ctx: ToolContext) -> str:
    return await reminders.upcoming_lines(ctx.user_id, ctx.tz)


@registry.tool(
    description=("Cancel an upcoming reminder. Cancels only a clear match (include its time if known); "
                 "otherwise returns candidates to ask about, then call again with the chosen line."),
    parameters={
        "type": "object",
        "properties": {"description": {"type": "string", "description": "e.g. 'the 3pm stretch reminder'."}},
        "required": ["description"],
    },
    running="Cancelling a reminder",
    summarize=_summarize_cancel,
)
async def cancel_reminder(ctx: ToolContext, description: str) -> str:
    return await reminders.cancel(ctx.user_id, ctx.tz, description)
