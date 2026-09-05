import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import session_scope
from app.services.sync_service import sync_all_mangaupdates, sync_suwayomi_library

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _run_mangaupdates_sync() -> None:
    with session_scope() as session:
        count = sync_all_mangaupdates(session)
        logger.info("scheduled MangaUpdates sync done: %s manga(s)", count)


def _run_suwayomi_sync() -> None:
    with session_scope() as session:
        result = sync_suwayomi_library(session)
        logger.info("scheduled Suwayomi sync done: %s", result)


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.enable_scheduler:
        logger.info("scheduler disabled via ENABLE_SCHEDULER=false")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_mangaupdates_sync,
        "interval",
        hours=settings.sync_interval_hours,
        id="mangaupdates_sync",
        next_run_time=None,
    )
    scheduler.add_job(
        _run_suwayomi_sync,
        "interval",
        hours=settings.suwayomi_sync_interval_hours,
        id="suwayomi_sync",
        next_run_time=None,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "scheduler started: mangaupdates every %sh, suwayomi every %sh",
        settings.sync_interval_hours,
        settings.suwayomi_sync_interval_hours,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
