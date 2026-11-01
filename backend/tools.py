# Tools the chat model can call (Groq path only). Each tool is an async function plus the JSON
# schema the model sees, and the labels the chat UI shows while it runs and once it's done.
# ToolRegistry.call never raises: failures come back as "Error: ..." text for the model to read,
# and every call and result is logged to the terminal.
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db2
import homelab
import memory_writer
import reminders
import weather
import websearch
from config import BRAVE_API_KEY, EMBED_MODEL, HOMELAB_USERS, USER_TIMEZONE
from embeddings import embed
from logs import get_logger

log = get_logger("tools")

# forget deletes on its own only when the best match is this similar to the request
# and clearly ahead of the runner-up; otherwise it hands candidates back to the model.
# Measured with nomic-embed-text: correct matches score ~0.6-0.8 with a gap of 0.15+ to the
# next fact, while ambiguous requests ("my job" with two jobs stored) have gaps under 0.06.
# The gap is the stronger signal: "where I work" scored "lives in Atlanta" highest.
FORGET_CONFIDENT = 0.6
FORGET_MARGIN = 0.1
FORGET_CANDIDATE_MIN = 0.5
FORGET_CANDIDATES = 5

MEMORY_TOOLS = {"remember", "forget"}


@dataclass
class ToolContext:
    user_id: str
    conversation_id: str
    username: str = ""
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(USER_TIMEZONE))  # the user's own timezone
    calls: list[str] = field(default_factory=list)  # names of tools called this turn
    tool_seconds: float = 0.0  # total time spent running tools this turn


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[str]]
    running: str  # UI label while the tool runs, e.g. "Saving to memory"
    summarize: Callable[[str], tuple[str, str]]  # result -> (UI summary, UI detail)
    available: Callable[[ToolContext], bool]  # whether this user may use the tool

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def tool(self, description: str, parameters: dict, running: str, summarize=None, available=None):
        """Decorator: registers an async fn(ctx, **args) -> str under its function name."""
        def register(fn):
            self._tools[fn.__name__] = Tool(fn.__name__, description, parameters, fn, running,
                                            summarize or (lambda result: (fn.__name__, result)),
                                            available or (lambda ctx: True))
            return fn
        return register

    def schemas(self, ctx: ToolContext) -> list[dict]:
        """The tools offered to the model for this user."""
        return [t.schema() for t in self._tools.values() if t.available(ctx)]

    def running_label(self, name: str) -> str:
        tool = self._tools.get(name)
        return tool.running if tool else f"Running {name}"

    def describe(self, name: str, result: str) -> dict:
        """What the chat UI shows for a finished call."""
        tool = self._tools.get(name)
        if result.startswith("Error:") or not tool:
            return {"ok": False, "summary": f"{tool.running if tool else name} failed", "detail": result}
        summary, detail = tool.summarize(result)
        return {"ok": True, "summary": summary, "detail": detail}

    async def call(self, name: str, raw_args: str, ctx: ToolContext) -> str:
        ctx.calls.append(name)
        started = time.perf_counter()
        result = await self._run(name, raw_args, ctx)
        ctx.tool_seconds += time.perf_counter() - started
        level = logging.WARNING if result.startswith("Error:") else logging.INFO
        log.log(level, "%s(%s) -> %s", name, raw_args or "", result)
        return result

    async def _run(self, name: str, raw_args: str, ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if not tool or not tool.available(ctx):
            offered = [n for n, t in self._tools.items() if t.available(ctx)]
            return f"Error: unknown tool {name!r}. Available: {', '.join(offered)}"
        try:
            args = json.loads(raw_args) if raw_args and raw_args.strip() else {}
        except json.JSONDecodeError as e:
            return f"Error: arguments are not valid JSON ({e})"
        if not isinstance(args, dict):
            return "Error: arguments must be a JSON object"
        try:
            return str(await tool.fn(ctx, **args))
        except TypeError as e:  # missing or unexpected argument
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:
            return f"Error: {name} failed: {e!r}"


registry = ToolRegistry()


def _after(prefix: str, result: str) -> str:
    return result.split(prefix, 1)[1].strip() if prefix in result else result


def _summarize_remember(result: str) -> tuple[str, str]:
    for prefix, summary in (("Saved:", "Saved to memory"), ("Updated:", "Updated memory"),
                            ("Already known:", "Already in memory"),
                            ("Removed outdated fact:", "Removed an outdated memory")):
        if result.startswith(prefix):
            return summary, _after(prefix, result)
    return "Didn't save to memory", result


def _summarize_forget(result: str) -> tuple[str, str]:
    if result.startswith("Forgot:"):
        return "Forgot", _after("Forgot:", result)
    if result.startswith("Not sure"):
        # Show the candidate facts, not the instructions and ids meant for the model
        candidates = [line.removeprefix("- ") for line in result.splitlines() if line.startswith("- ")]
        return "Asked which memory to forget", "\n".join(candidates)
    return "Nothing matched to forget", result


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


@registry.tool(
    description="Save a lasting fact about the user (only when asked). Merges with or skips existing facts.",
    parameters={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "Short third-person sentence, e.g. 'The user has a dog named Max.'"},
            "category": {"type": "string", "enum": ["personal", "work", "preference", "project", "goal", "other"]},
        },
        "required": ["fact"],
    },
    running="Saving to memory",
    summarize=_summarize_remember,
)
async def remember(ctx: ToolContext, fact: str, category: str = "other") -> str:
    fact = fact.strip()
    if not fact:
        return "Error: fact is empty"
    return await memory_writer.reconcile(
        ctx.user_id, memory_writer.ExtractedFact(text=fact, category=category), ctx.conversation_id)


@registry.tool(
    description=("Delete a stored fact (only when asked). Deletes only a clear match; otherwise returns "
                 "candidates to ask about, then call again with the chosen fact's exact text."),
    parameters={
        "type": "object",
        "properties": {"description": {"type": "string", "description": "What to forget, e.g. 'my dog's name'."}},
        "required": ["description"],
    },
    running="Looking for the memory to forget",
    summarize=_summarize_forget,
)
async def forget(ctx: ToolContext, description: str) -> str:
    if not description.strip():
        return "Error: describe the fact to forget."
    vec = await embed(description, "query")
    matches = await db2.search_facts(ctx.user_id, vec, EMBED_MODEL, FORGET_CANDIDATES, FORGET_CANDIDATE_MIN)
    if not matches:
        return f"No stored fact matches {description!r}. Nothing was deleted."

    # The follow-up after a "which one?" passes a candidate's exact text
    exact = [m for m in matches if m["text"].strip().lower() == description.strip().lower()]
    best = exact[0] if exact else matches[0]
    runner_up = matches[1]["score"] if len(matches) > 1 else 0.0
    if exact or (best["score"] >= FORGET_CONFIDENT and best["score"] - runner_up >= FORGET_MARGIN):
        await db2.delete_fact(ctx.user_id, best["id"])
        return f"Forgot: {best['text']}"

    # No ids in the listing: whatever the model sees, it may repeat to the user
    listing = "\n".join(f"- {m['text']}" for m in matches)
    return ("Not sure which fact is meant, so nothing was deleted. Ask the user which one to forget "
            f"(or whether none of these), then call forget again with that fact's exact text:\n{listing}")


@registry.tool(
    description="Current weather and daily forecast: temperature, conditions, rain chance, wind.",
    parameters={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City, e.g. 'Chicago, IL'."},
            "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Default 3."},
            "units": {"type": "string", "enum": ["imperial", "metric"]},
        },
        "required": ["location"],
    },
    running="Checking the weather",
    summarize=lambda result: ("Checked the weather", result.splitlines()[0].removesuffix(":")),
)
async def get_weather(ctx: ToolContext, location: str, days: int = 3, units: str = "imperial") -> str:
    return await weather.forecast(location, days, units)


@registry.tool(
    description="Search the web for current information. Returns top results with URLs.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    running="Searching the web",
    summarize=lambda result: ("Searched the web", result),
    available=lambda ctx: bool(BRAVE_API_KEY),
)
async def web_search(ctx: ToolContext, query: str) -> str:
    if not query.strip():
        return "Error: query is empty"
    return await websearch.search(query)


def _summarize_homelab(result: str) -> tuple[str, str]:
    overall = next((l for l in result.splitlines() if l.startswith("Overall:")), "")
    summary = "Pi is healthy" if overall == "Overall: healthy" else "Pi has issues" if overall else "Checked the Pi"
    return summary, result


@registry.tool(
    description="Health of the user's Raspberry Pi home server: temperature, load, memory, disks, power, services.",
    parameters={"type": "object", "properties": {}},
    running="Checking the Pi",
    summarize=_summarize_homelab,
    available=lambda ctx: ctx.username.lower() in HOMELAB_USERS,
)
async def homelab_stats(ctx: ToolContext) -> str:
    return await homelab.report()


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
