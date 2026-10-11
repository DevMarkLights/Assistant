# Tools the chat model can call (Groq path only). Each tool is an async function plus the JSON
# schema the model sees, and the labels the chat UI shows while it runs and once it's done.
# ToolRegistry.call never raises: failures come back as "Error: ..." text for the model to read,
# and every call and result is logged to the terminal.
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db2
import memory_writer
from config import EMBED_MODEL
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
    calls: list[str] = field(default_factory=list)  # names of tools called this turn


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[str]]
    running: str  # UI label while the tool runs, e.g. "Saving to memory"
    summarize: Callable[[str], tuple[str, str]]  # result -> (UI summary, UI detail)

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def tool(self, description: str, parameters: dict, running: str, summarize=None):
        """Decorator: registers an async fn(ctx, **args) -> str under its function name."""
        def register(fn):
            self._tools[fn.__name__] = Tool(fn.__name__, description, parameters, fn, running,
                                            summarize or (lambda result: (fn.__name__, result)))
            return fn
        return register

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

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
        result = await self._run(name, raw_args, ctx)
        level = logging.WARNING if result.startswith("Error:") else logging.INFO
        log.log(level, "%s(%s) -> %s", name, raw_args or "", result)
        return result

    async def _run(self, name: str, raw_args: str, ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool {name!r}. Available: {', '.join(self._tools)}"
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
        candidates = [line.split(": ", 1)[-1] for line in result.splitlines() if line.startswith("- ")]
        return "Asked which memory to forget", "\n".join(candidates)
    return "Nothing matched to forget", result


@registry.tool(
    description="Get the current date and time. Use when the user asks what time or day it is.",
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone such as 'America/New_York'. Omit for the server's local time.",
            },
        },
    },
    running="Checking the time",
    summarize=lambda result: ("Checked the time", result),
)
async def get_current_time(ctx: ToolContext, timezone: str | None = None) -> str:
    try:
        now = datetime.now(ZoneInfo(timezone)) if timezone else datetime.now().astimezone()
    except ZoneInfoNotFoundError:
        return f"Error: unknown timezone {timezone!r}. Use an IANA name like 'America/Chicago'."
    return now.strftime("%A, %B %-d, %Y, %-I:%M %p ") + (timezone or now.tzname() or "local time")


@registry.tool(
    description=(
        "Save a lasting fact about the user to long-term memory. Use only when the user explicitly "
        "asks you to remember something. Merges with or skips facts that are already stored."
    ),
    parameters={
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "One short third-person sentence, e.g. 'The user has a dog named Max.'",
            },
            "category": {
                "type": "string",
                "enum": ["personal", "work", "preference", "project", "goal", "other"],
            },
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
    description=(
        "Delete a stored fact about the user. Use only when the user asks you to forget something. "
        "Pass a description of the fact; it is deleted only if one stored fact clearly matches. "
        "Otherwise you get candidates back: ask the user which one they mean, then call again "
        "with that fact's exact text as the description (or its fact_id in the same turn)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What to forget, e.g. 'my dog's name'."},
            "fact_id": {"type": "string", "description": "Exact id of a candidate from a previous forget call."},
        },
    },
    running="Looking for the memory to forget",
    summarize=_summarize_forget,
)
async def forget(ctx: ToolContext, description: str = "", fact_id: str | None = None) -> str:
    if fact_id:
        fact = await db2.get_fact(ctx.user_id, fact_id)
        if not fact:
            return f"Error: no stored fact with id {fact_id!r}. Nothing was deleted."
        await db2.delete_fact(ctx.user_id, fact_id)
        return f"Forgot: {fact['text']}"

    if not description.strip():
        return "Error: pass a description of the fact to forget, or a fact_id."
    vec = await embed(description, "query")
    matches = await db2.search_facts(ctx.user_id, vec, EMBED_MODEL, FORGET_CANDIDATES, FORGET_CANDIDATE_MIN)
    if not matches:
        return f"No stored fact matches {description!r}. Nothing was deleted."

    best = matches[0]
    runner_up = matches[1]["score"] if len(matches) > 1 else 0.0
    if best["score"] >= FORGET_CONFIDENT and best["score"] - runner_up >= FORGET_MARGIN:
        await db2.delete_fact(ctx.user_id, best["id"])
        return f"Forgot: {best['text']}"

    listing = "\n".join(f"- fact_id={m['id']}: {m['text']}" for m in matches)
    return ("Not sure which fact is meant, so nothing was deleted. Ask the user which one to forget "
            "(or whether none of these), then call forget again with that fact's exact text "
            f"as the description:\n{listing}")
