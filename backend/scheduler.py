# Background jobs, run by APScheduler inside the FastAPI process (one uvicorn worker in production,
# so each job runs once). Started from main.py's lifespan when SCHEDULER_ENABLED is true. Jobs must
# never take the server down: each is wrapped so exceptions are logged and swallowed.
import functools
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import brief
from config import MORNING_BRIEF_TIME, SCHEDULER_ENABLED, USER_TIMEZONE
from logs import get_logger

log = get_logger("scheduler")

CATCH_UP = timedelta(hours=3)  # after a restart, still send today's brief if it's this late at most

_scheduler: AsyncIOScheduler | None = None


def safe(job):
    @functools.wraps(job)
    async def wrapper():
        try:
            await job()
        except Exception:
            log.exception("job %s failed", job.__name__)
    return wrapper


def brief_time() -> tuple[int, int]:
    hour, minute = MORNING_BRIEF_TIME.split(":")
    return int(hour), int(minute)


def brief_is_due_for_catch_up(now) -> bool:
    """True if now is past today's brief time but within CATCH_UP of it."""
    hour, minute = brief_time()
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return scheduled <= now <= scheduled + CATCH_UP


def start() -> None:
    global _scheduler
    if not SCHEDULER_ENABLED:
        log.info("scheduler disabled (SCHEDULER_ENABLED=false)")
        return
    hour, minute = brief_time()
    _scheduler = AsyncIOScheduler(timezone=USER_TIMEZONE)
    _scheduler.add_job(safe(brief.run_scheduled), CronTrigger(hour=hour, minute=minute, timezone=USER_TIMEZONE),
                       id="morning_brief", misfire_grace_time=int(CATCH_UP.total_seconds()), coalesce=True)
    # Restarted after the brief time (e.g. a deploy at 7:05)? Send today's now; claims prevent repeats
    if brief_is_due_for_catch_up(brief.local_now()):
        log.info("catching up on today's morning brief")
        _scheduler.add_job(safe(brief.run_scheduled), id="morning_brief_catch_up")
    _scheduler.start()
    log.info("scheduler started; morning brief daily at %s %s (next: %s)", MORNING_BRIEF_TIME, USER_TIMEZONE,
             _scheduler.get_job("morning_brief").next_run_time)


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
