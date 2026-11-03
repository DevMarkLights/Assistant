# The tool framework: ToolRegistry, Tool, and ToolContext. Each tool is an async function plus the
# JSON schema the model sees, and the labels the chat UI shows while it runs and once it's done.
# The tools themselves live in the sibling modules and register with `registry`.
# ToolRegistry.call never raises: failures come back as "Error: ..." text for the model to read,
# and every call and result is logged to the terminal.
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.core.config import USER_TIMEZONE
from app.core.logs import get_logger

log = get_logger("tools")


@dataclass
class ToolContext:
    user_id: str
    conversation_id: str
    username: str = ""
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(USER_TIMEZONE))  # the user's own timezone
    calls: list[str] = field(default_factory=list)  # names of tools called this turn
    tool_seconds: float = 0.0  # total time spent running tools this turn
    offered: set[str] | None = None  # tools sent to the model for this message (groups.py); None = all


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
        return [t.schema() for t in self._tools.values()
                if t.available(ctx) and (ctx.offered is None or t.name in ctx.offered)]

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
        # Models sometimes add arguments the tool doesn't declare (gpt-oss sends {"": ""} to tools
        # that take none): drop them instead of failing the call
        declared = tool.parameters.get("properties", {})
        if extra := [k for k in args if k not in declared]:
            log.info("%s: ignoring undeclared arguments %s", name, extra)
            args = {k: v for k, v in args.items() if k in declared}
        try:
            return str(await tool.fn(ctx, **args))
        except TypeError as e:  # missing or unexpected argument
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:
            return f"Error: {name} failed: {e!r}"


registry = ToolRegistry()
