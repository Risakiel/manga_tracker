import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import session_scope
from app.models import ScheduledJob
from app.services import jobs

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

# Delay before a job that is already overdue at startup (e.g. a daily job
# whose last run was more than a day ago) fires, so a container restart
# doesn't hammer every source in the very first seconds.
_STARTUP_DELAY = timedelta(minutes=1)


def _first_run_time(job_id: str, minutes: int) -> datetime:
    """Resume each job's cadence from its last run instead of restarting the
    countdown on every container restart -- Unraid restarts the container on
    every image update, which would otherwise keep pushing a daily job back
    forever."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.get(ScheduledJob, job_id)
        last = row.last_finished_at if row else None
    if last is None:
        return now + _STARTUP_DELAY
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(last + timedelta(minutes=minutes), now + _STARTUP_DELAY)


def _schedule(scheduler: BackgroundScheduler, job_id: str) -> None:
    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)
    job = jobs.get(job_id)
    if job is None or not job.is_available():
        return
    minutes = jobs.interval_minutes(job_id)
    if minutes <= 0:
        return
    scheduler.add_job(
        jobs.run_job,
        "interval",
        minutes=minutes,
        args=[job_id, "planifié"],
        id=job_id,
        next_run_time=_first_run_time(job_id, minutes),
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def reschedule(job_id: str) -> None:
    """Apply an interval change made from the UI to the running scheduler."""
    if _scheduler is not None:
        _schedule(_scheduler, job_id)


def next_run_time(job_id: str) -> Optional[datetime]:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None


def is_running() -> bool:
    return _scheduler is not None


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.enable_scheduler:
        logger.info("scheduler disabled via ENABLE_SCHEDULER=false")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    for job_id in jobs.JOBS:
        _schedule(scheduler, job_id)
    scheduler.start()
    _scheduler = scheduler
    for job_id, job in jobs.JOBS.items():
        scheduled = scheduler.get_job(job_id)
        logger.info(
            "scheduler: %s -> %s",
            job_id,
            f"every {jobs.interval_minutes(job_id)} min, next run {scheduled.next_run_time}"
            if scheduled
            else ("disabled" if job.is_available() else f"unavailable ({job.unavailable_reason})"),
        )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
