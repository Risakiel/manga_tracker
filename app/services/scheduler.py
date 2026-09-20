import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import session_scope
from app.services import library_client
from app.services.sync_service import (
    sync_all_mangaupdates,
    sync_komga_library,
    sync_server_folders,
    sync_suwayomi_library,
)

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


def _run_komga_sync() -> None:
    with session_scope() as session:
        result = sync_komga_library(session)
        logger.info("scheduled Komga sync done: %s", result)


def _run_library_sync() -> None:
    with session_scope() as session:
        result = sync_server_folders(session)
        logger.info("scheduled server-folders sync done: %s", result)


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
    if settings.komga_url:
        scheduler.add_job(
            _run_komga_sync,
            "interval",
            hours=settings.komga_sync_interval_hours,
            id="komga_sync",
            next_run_time=None,
        )
    if library_client.is_mounted():
        scheduler.add_job(
            _run_library_sync,
            "interval",
            hours=settings.library_sync_interval_hours,
            id="library_sync",
            next_run_time=None,
        )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "scheduler started: mangaupdates every %sh, suwayomi every %sh, komga %s, server-folders %s",
        settings.sync_interval_hours,
        settings.suwayomi_sync_interval_hours,
        f"every {settings.komga_sync_interval_hours}h" if settings.komga_url else "disabled (no KOMGA_URL)",
        f"every {settings.library_sync_interval_hours}h"
        if library_client.is_mounted()
        else "disabled (NAS not mounted)",
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
