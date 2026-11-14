# Chooses which model serves each request. Every cloud LLM call names a task (chat_tools,
# background_json, titles, morning_brief, from models.yaml); the router offers that task's models
# in order of preference, keeping only those that:
#   - have the capabilities the task needs (tools, json_mode, ...);
#   - may receive the task's data (a provider gets personal data only with allow_personal_data);
#   - belong to a provider with an API key;
#   - have rate-limit budget for the request right now (limits.py): not cooling down after a 429,
#     provider not sidelined, and enough requests and tokens left this minute and today.
# Callers reserve the estimated tokens before a request and report the outcome after it, and every
# request is logged with its provider, model, tokens and the budget left.
import asyncio
import json
import math

from app import db
from app.core.logs import get_logger
from app.llm import providers
from app.llm.limits import SIDELINE_SECONDS, Limiter, ModelState, ProviderState, Reservation, day_of
from app.llm.providers import Model

log = get_logger("llm")


class NoModelAvailable(Exception):
    """No configured model can serve the task (missing capabilities, keys, or data permission)."""


class AllModelsBusy(Exception):
    """Every model that could serve the task is out of budget or cooling down for now."""

    def __init__(self, task: str, wait: float, reasons: list[str]):
        self.task, self.wait, self.reasons = task, wait, reasons
        super().__init__(f"all models for {task} are busy ({'; '.join(reasons)})")

    def user_message(self) -> str:
        if math.isinf(self.wait):
            return "That request is too large for the available AI models' limits. Try a shorter message."
        seconds = max(1, math.ceil(self.wait))
        when = f"{seconds} seconds" if seconds < 120 else f"{math.ceil(seconds / 60)} minutes" if seconds < 7200 \
            else f"{round(seconds / 3600)} hours"
        return f"All AI models are busy right now. Try again in {when}."


_pending: set[asyncio.Task] = set()  # usage and event writes in flight


def _save_later(write, what: str) -> None:
    """Runs a database write in the background, so routing never waits on Mongo."""
    async def save():
        try:
            await write()
        except Exception:
            log.warning("couldn't save %s", what, exc_info=True)
    try:
        task = asyncio.get_running_loop().create_task(save())
    except RuntimeError:  # no event loop (scripts, tests)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _persist(model: str, day: str, requests: int, tokens: int) -> None:
    """Adds a finished request to the daily totals in Mongo."""
    _save_later(lambda: db.add_llm_usage(model, day, requests, tokens), f"LLM usage for {model}")


def record_event(kind: str, task: str | None, model: str | None, detail: str = "") -> None:
    """Logs a notable routing event for the usage page and the brief: rate_limited, failover,
    sidelined, busy (no model available) or interrupted (a reply cut off mid-stream)."""
    _save_later(lambda: db.add_llm_event(kind, task, model, detail), f"LLM event {kind}")


limiter: Limiter | None = None
if providers.config:
    limiter = Limiter(
        {key: m.limits for key, m in providers.config.models.items()},
        {key: m.provider.name for key, m in providers.config.models.items()},
        {name: p.rate_limit_headers for name, p in providers.config.providers.items() if p.rate_limit_headers},
        on_record=_persist,
    )


async def load_usage() -> None:
    """Restores today's daily totals from Mongo (at startup), so restarts don't reset them."""
    if not limiter:
        return
    today = day_of(limiter.clock.now())
    try:
        limiter.load_day(today, await db.llm_usage_for_day(today))
    except Exception:
        log.warning("couldn't load today's LLM usage; daily counts start from zero", exc_info=True)


def register(model: Model) -> None:
    """Adds a model that isn't in models.yaml (evals trying a new one) to the rate limiter, with no
    limits of its own; 429s still cool it down."""
    if model.key not in limiter.models:
        limiter.limits[model.key] = model.limits
        limiter.providers[model.key] = model.provider.name
        limiter.models[model.key] = ModelState()
        limiter.provider_state.setdefault(model.provider.name, ProviderState())


def candidates(task_name: str) -> list[Model]:
    """The task's models that could ever serve it (capabilities, data permission, API key)."""
    task = providers.config.tasks[task_name]
    usable = [m for m in task.models
              if all(m.can(need) for need in task.needs)
              and (m.provider.allow_personal_data or not task.personal_data)
              and m.provider.client is not None]
    if not usable:
        raise NoModelAvailable(f"no model can serve {task_name}: check its models' capabilities, "
                               "API keys and allow_personal_data in models.yaml")
    return usable


def estimate(task_name: str, messages: list[dict], tools: list[dict] | None = None) -> int:
    """Tokens a request may use: the prompt (about 4 characters per token, tool schemas included)
    plus the task's max_output."""
    prompt = len(json.dumps(messages, ensure_ascii=False)) + (len(json.dumps(tools)) if tools else 0)
    return prompt // 4 + providers.config.tasks[task_name].max_output


def available(task_name: str, tokens: int, exclude: set[str] = frozenset()) -> list[Model]:
    """The task's models that can take a request of about `tokens` now, in order of preference.
    Raises AllModelsBusy (with the shortest wait) if none can."""
    ready, waits, reasons = [], [], []
    for model in candidates(task_name):
        if model.key in exclude:  # already failed this request; it counts only for the wait
            waits.append(limiter.blocked_for(model.key) or 1.0)
            reasons.append(f"{model.label}: failed this request")
            continue
        ok, reason, wait = limiter.check(model.key, tokens)
        if ok:
            ready.append(model)
        else:
            waits.append(wait)
            reasons.append(f"{model.label}: {reason}")
            log.info("router: %s skips %s (%s; ~%s)", task_name, model.label, reason,
                     "never" if math.isinf(wait) else f"{wait:.0f}s")
    if not ready:
        raise AllModelsBusy(task_name, min(waits, default=math.inf), reasons)
    return ready


def reserve(model: Model, tokens: int) -> Reservation:
    return limiter.reserve(model.key, tokens)


def served(task_name: str, model: Model, purpose: str, usage, reservation: Reservation | None = None,
           headers=None) -> int:
    """A finished request: records its actual tokens, syncs budgets from the provider's headers, and
    logs which provider and model served it with the budget left. Returns its total tokens."""
    prompt, completion, total = _counts(usage)
    if reservation:
        limiter.finish(reservation, total)
    limiter.sync_headers(model.key, headers)
    log.info("router: %s (%s) served by %s: %d in + %d out = %d tokens | left %s",
             task_name, purpose, model.label, prompt, completion, total, _budget(model))
    return total


def unfinished(reservation: Reservation) -> None:
    """A request that ended without reporting usage (stream cut short): count its estimate."""
    limiter.finish(reservation, None)


def rate_limited(model: Model, error, reservation: Reservation | None = None, task: str | None = None) -> float:
    """A 429: undoes the reservation and cools the model down until its reset time."""
    if reservation:
        limiter.release(reservation)
    response = getattr(error, "response", None)
    seconds = limiter.rate_limited(model.key, response.headers if response is not None else None, str(error))
    log.warning("router: %s rate-limited (429); cooling down for %.0fs", model.label, seconds)
    record_event("rate_limited", task, model.label, f"cooling down for {seconds:.0f}s")
    return seconds


def failed(model: Model, error, reservation: Reservation | None = None, task: str | None = None) -> None:
    """A server or connection error: undoes the reservation; repeated errors sideline the provider."""
    if reservation:
        limiter.release(reservation)
    if limiter.provider_error(model.key):
        log.warning("router: provider %s sidelined for %.0f minutes after repeated errors (last: %s)",
                    model.provider.name, SIDELINE_SECONDS / 60, type(error).__name__)
        record_event("sidelined", task, model.provider.name,
                     f"{SIDELINE_SECONDS / 60:.0f} minutes after repeated errors (last: {type(error).__name__})")


# Errors that say something is wrong with the provider (outage, network, bad key), so they count
# toward sidelining it. Other errors (e.g. a request too long for one model) only skip that model.
PROVIDER_FAULT_STATUS = {401, 403}


class Failover:
    """Picks the model for one request, moving down the task's list when a model fails. A model
    that answers 429 is cooled down and skipped; one that errors is skipped for this request, but
    gets one second chance (after a second's pause) if nothing else is free, so a single-model task
    survives a brief blip. Raises AllModelsBusy when no model is left."""

    def __init__(self, task_name: str, tokens: int):
        self.task, self.tokens = task_name, tokens
        self.limited: set[str] = set()
        self.broken: set[str] = set()
        self.second_chance = False
        self.previous: Model | None = None
        self.why = ""

    async def next(self) -> Model:
        try:
            model = available(self.task, self.tokens, exclude=self.limited | self.broken)[0]
        except AllModelsBusy:
            if not self.broken or self.second_chance:
                raise
            self.second_chance = True
            self.broken.clear()
            log.info("router: %s retrying a failed model once", self.task)
            await asyncio.sleep(1)
            model = available(self.task, self.tokens, exclude=self.limited)[0]
        if self.previous is not None:
            log.warning("router: %s failover %s -> %s (%s)", self.task, self.previous.label, model.label, self.why)
            record_event("failover", self.task, model.label, f"from {self.previous.label} ({self.why})")
        self.previous = model
        return model

    def rate_limited(self, model: Model, error, reservation: Reservation) -> None:
        rate_limited(model, error, reservation, self.task)
        self.limited.add(model.key)
        self.why = "429"

    def failed(self, model: Model, error, reservation: Reservation) -> None:
        status = getattr(error, "status_code", None)
        provider_fault = status is None or status >= 500 or status in PROVIDER_FAULT_STATUS
        if provider_fault:
            failed(model, error, reservation, self.task)
        else:
            limiter.release(reservation)
        log.warning("router: %s request to %s failed: %s%s", self.task, model.label, type(error).__name__,
                    f" ({status})" if status else "")
        self.broken.add(model.key)
        self.why = type(error).__name__ + (f" {status}" if status else "")


def status() -> dict:
    """Live routing state for the usage page: each model's limits, budget left this minute and
    today (as this server counts it), cooldown, and each task's model order."""
    now = limiter.clock.now()
    models = []
    for key, model in providers.config.models.items():
        st = limiter.models[key]
        provider_st = limiter.provider_state[model.provider.name]
        models.append({
            "key": key, "label": model.label, "provider": model.provider.name,
            "personal_data": model.provider.allow_personal_data, "has_key": model.provider.client is not None,
            "limits": {k: model.limits.get(k) for k in ("rpm", "tpm", "rpd", "tpd")},
            "remaining": limiter.remaining(key),
            "cooldown_seconds": round(max(0.0, st.cooldown_until - now)),
            "cooldown_reason": st.cooldown_reason if st.cooldown_until > now else "",
            "sidelined_seconds": round(max(0.0, provider_st.sidelined_until - now)),
        })
    tasks = []
    for task in providers.config.tasks.values():
        try:
            usable = {m.key for m in candidates(task.name)}
        except NoModelAvailable:
            usable = set()
        tasks.append({"name": task.name, "personal_data": task.personal_data,
                      "models": [{"key": m.key, "usable": m.key in usable,
                                  "why_not": "" if m.key in usable else
                                  "not approved for personal data" if task.personal_data and not m.provider.allow_personal_data
                                  else "no API key" if m.provider.client is None else "lacks a needed capability"}
                                 for m in task.models]})
    return {"day": day_of(now), "models": models, "tasks": tasks}


def _budget(model: Model) -> str:
    left = limiter.remaining(model.key)
    part = lambda n, unit: "∞" if n is None else f"{n:,} {unit}"
    return (f"minute {part(left['minute_tokens'], 'tok')} / {part(left['minute_requests'], 'req')}, "
            f"day {part(left['day_tokens'], 'tok')} / {part(left['day_requests'], 'req')}")


def _counts(usage) -> tuple[int, int, int]:
    """Token counts from a usage object or dict (Groq's streamed x_groq.usage arrives as a dict)."""
    if not usage:
        return 0, 0, 0
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0))
    prompt, completion = get("prompt_tokens") or 0, get("completion_tokens") or 0
    return prompt, completion, get("total_tokens") or prompt + completion
