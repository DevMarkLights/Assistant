# Plain-text summaries of LLM usage (from db.llm_usage and db.llm_events), written by code rather
# than a model so the numbers are exact and cost nothing. Used by the owner's morning brief.
from datetime import datetime, timedelta, timezone

from app import db
from app.llm import providers


def _tokens(n: int) -> str:
    return f"{n / 1000:.0f}K" if n >= 10_000 else f"{n / 1000:.1f}K" if n >= 1000 else str(n)


async def yesterday_line(now: datetime | None = None) -> str:
    """One line about the previous UTC day: requests and tokens per model, and fallbacks. "" if
    there's nothing to report (LOCAL=true, or no usage)."""
    if providers.config is None:
        return ""
    now = now or datetime.now(timezone.utc)
    end = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    usage = await db.llm_usage_for_day(start.strftime("%Y-%m-%d"))
    if not any(requests for requests, _ in usage.values()):
        return ""
    label = lambda key: providers.config.models[key].id.rsplit("/", 1)[-1] if key in providers.config.models else key
    busiest = sorted(usage.items(), key=lambda kv: -kv[1][0])
    per_model = ", ".join(f"{label(k)} {r}" for k, (r, _) in busiest if r)
    total_requests = sum(r for r, _ in usage.values())
    total_tokens = sum(t for _, t in usage.values())
    events = await db.llm_events_between(start, end)
    count = lambda kind: sum(1 for e in events if e["kind"] == kind)
    trouble = [f"{n} {what}" for n, what in ((count("failover"), "fallback(s)"), (count("rate_limited"), "rate limit(s)"),
                                             (count("busy"), "time(s) all models were busy"),
                                             (count("interrupted"), "cut-off reply(ies)")) if n]
    return (f"AI usage yesterday: {total_requests} requests, {_tokens(total_tokens)} tokens ({per_model}); "
            + (", ".join(trouble) if trouble else "no fallbacks") + ".")
