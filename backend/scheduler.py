# Background jobs, run by APScheduler inside the FastAPI process (one uvicorn worker in production,
# so each job runs once). Started from main.py's lifespan when SCHEDULER_ENABLED is true. Jobs must
# never take the server down: each is wrapped so exceptions are logged and swallowed.
import functools
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import brief
import reminders
from config import MORNING_BRIEF_TIME, SCHEDULER_ENABLED
from logs import get_logger

log = get_logger("scheduler")

_scheduler: AsyncIOScheduler | None = None


def safe(job):
    @functools.wraps(job)
    async def wrapper():
        try:
            await job()
        except Exception:
            log.exception("job %s failed", job.__name__)
    return wrapper


def start() -> None:
    global _scheduler
    if not SCHEDULER_ENABLED:
        log.info("scheduler disabled (SCHEDULER_ENABLED=false)")
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    # Users can be in different timezones, so instead of one 7:00 cron job, check every 5 minutes
    # who is in their brief window. This also catches up after a restart; claims prevent repeats.
    _scheduler.add_job(safe(brief.run_due), IntervalTrigger(minutes=5), id="morning_brief",
                       max_instances=1, coalesce=True, next_run_time=datetime.now(timezone.utc))
    # Due reminders, every minute. max_instances=1 so a slow run never overlaps the next
    _scheduler.add_job(safe(reminders.send_due), IntervalTrigger(minutes=1), id="reminders",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("scheduler started; morning briefs at %s in each user's timezone, reminders every minute",
             MORNING_BRIEF_TIME)


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
