"""Registry of the background jobs shown on the "Planification de tâches"
page. Each job is run the same way whether the scheduler fires it or the
user clicks a "sync now" button, so both share one in-memory progress dict
and one lock -- a manual run and a scheduled run of the same job can never
overlap.

Intervals and last-run info live in the ScheduledJob table so a choice made
in the UI survives container restarts; the env vars in config.py are only
the defaults used until the user picks something else.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlmodel import Session

from app.config import settings
from app.database import session_scope
from app.models import ScheduledJob
from app.services import library_client
from app.services.sync_service import (
    sync_all_manga_sources,
    link_new_komga_series,
    sync_komga_library,
    sync_server_folders,
    sync_suwayomi_library,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

# (minutes, label) offered in the interval dropdown; 0 = disabled.
INTERVAL_CHOICES: list[tuple[int, str]] = [
    (15, "Toutes les 15 min"),
    (30, "Toutes les 30 min"),
    (60, "Toutes les heures"),
    (120, "Toutes les 2 h"),
    (180, "Toutes les 3 h"),
    (360, "Toutes les 6 h"),
    (720, "Toutes les 12 h"),
    (1440, "Tous les jours"),
    (10080, "Toutes les semaines"),
    (0, "Désactivé"),
]


def interval_label(minutes: int) -> str:
    for value, label in INTERVAL_CHOICES:
        if value == minutes:
            return label
    if minutes % 60 == 0:
        return f"Toutes les {minutes // 60} h"
    return f"Toutes les {minutes} min"


@dataclass
class JobDef:
    id: str
    label: str
    description: str
    run: Callable[[Session, ProgressCallback], dict]
    default_interval_minutes: Callable[[], int]
    # Keys (besides running/total/done/error) the progress partials read --
    # reset to 0 at the start of every run.
    result_keys: tuple[str, ...] = ()
    summary: Callable[[dict], str] = lambda result: ""
    is_available: Callable[[], bool] = lambda: True
    unavailable_reason: str = ""
    page_url: str = ""
    state: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _hours(value: float) -> int:
    return max(int(round(value * 60)), 1)


def _run_manga_sources(session: Session, progress: ProgressCallback) -> dict:
    return {"count": sync_all_manga_sources(session, progress_callback=progress)}


JOBS: dict[str, JobDef] = {}


def _register(job: JobDef) -> None:
    job.state.update(running=False, total=0, done=0, error=None, **{k: 0 for k in job.result_keys})
    JOBS[job.id] = job


_register(
    JobDef(
        id="suwayomi_sync",
        label="🔗 Synchronisation Suwayomi",
        description="Importe les nouveaux mangas de la bibliothèque Suwayomi et met à jour leurs catégories.",
        run=lambda session, progress: sync_suwayomi_library(session, progress_callback=progress),
        default_interval_minutes=lambda: _hours(settings.suwayomi_sync_interval_hours),
        result_keys=("matched", "created"),
        summary=lambda r: f"{r.get('matched', 0)} lié(s), {r.get('created', 0)} créé(s)",
        is_available=lambda: bool(settings.suwayomi_url),
        unavailable_reason="SUWAYOMI_URL non configuré",
        page_url="/suwayomi",
    )
)
_register(
    JobDef(
        id="komga_sync",
        label="📖 Synchronisation Komga",
        description="Relie les mangas à leur série Komga, récupère la progression de lecture et complète les métadonnées Komga.",
        run=lambda session, progress: sync_komga_library(session, progress_callback=progress),
        default_interval_minutes=lambda: _hours(settings.komga_sync_interval_hours),
        result_keys=("matched", "unmatched", "pushed"),
        summary=lambda r: f"{r.get('matched', 0)} lié(s), {r.get('unmatched', 0)} non trouvé(s), {r.get('pushed', 0)} enrichi(s)",
        is_available=lambda: bool(settings.komga_url),
        unavailable_reason="KOMGA_URL non configuré",
        page_url="/komga",
    )
)
_register(
    JobDef(
        id="komga_new_series",
        label="🆕 Liaison des nouvelles séries Komga",
        description="Passe rapide : associe uniquement les séries Komga pas encore liées (ex. un manga Suwayomi qui vient d'arriver sur le NAS) au bon manga.",
        run=lambda session, progress: link_new_komga_series(session, progress_callback=progress),
        default_interval_minutes=lambda: _hours(settings.komga_new_series_interval_hours),
        result_keys=("matched", "unmatched", "pushed"),
        summary=lambda r: f"{r.get('matched', 0)} nouvelle(s) liaison(s), {r.get('unmatched', 0)} série(s) sans manga",
        is_available=lambda: bool(settings.komga_url),
        unavailable_reason="KOMGA_URL non configuré",
        page_url="/komga",
    )
)
_register(
    JobDef(
        id="library_sync",
        label="📁 Dossiers serveur",
        description="Vérifie le champ « Dossier serveur » de chaque manga par rapport aux vrais dossiers du NAS.",
        run=lambda session, progress: sync_server_folders(session, progress_callback=progress),
        default_interval_minutes=lambda: _hours(settings.library_sync_interval_hours),
        result_keys=("checked", "matched", "unmatched"),
        summary=lambda r: f"{r.get('checked', 0)} vérifié(s), {r.get('matched', 0)} corrigé(s), {r.get('unmatched', 0)} sans correspondance",
        is_available=library_client.is_mounted,
        unavailable_reason="dossier NAS non monté",
        page_url="/server-folders",
    )
)
_register(
    JobDef(
        id="manga_sources_sync",
        label="🌐 Sources (MangaUpdates, MangaDex, AniList)",
        description="Met à jour le statut et le nombre de chapitres de chaque manga depuis MangaUpdates, puis MangaDex/AniList si besoin.",
        run=_run_manga_sources,
        default_interval_minutes=lambda: _hours(settings.sync_interval_hours),
        result_keys=("count",),
        summary=lambda r: f"{r.get('count', 0)} manga(s) mis à jour",
        page_url="/",
    )
)


def get(job_id: str) -> Optional[JobDef]:
    return JOBS.get(job_id)


def state(job_id: str) -> dict:
    return JOBS[job_id].state


def _row(session: Session, job_id: str) -> ScheduledJob:
    row = session.get(ScheduledJob, job_id)
    if row is None:
        row = ScheduledJob(job_id=job_id)
    return row


def interval_minutes(job_id: str, session: Optional[Session] = None) -> int:
    job = JOBS[job_id]
    if session is None:
        with session_scope() as own_session:
            return interval_minutes(job_id, own_session)
    row = session.get(ScheduledJob, job_id)
    if row is not None and row.interval_minutes is not None:
        return row.interval_minutes
    return job.default_interval_minutes()


def set_interval(session: Session, job_id: str, minutes: int) -> None:
    row = _row(session, job_id)
    row.interval_minutes = max(minutes, 0)
    session.add(row)
    session.commit()


def run_job(job_id: str, trigger: str = "manuel") -> bool:
    """Run a job synchronously in the current thread. Returns False without
    doing anything if that job is already running."""
    job = JOBS[job_id]
    if not job.lock.acquire(blocking=False):
        logger.info("job %s already running, skipping %s run", job_id, trigger)
        return False
    started_at = datetime.now(timezone.utc)
    job.state.update(running=True, total=0, done=0, error=None, **{k: 0 for k in job.result_keys})
    status, message = "success", ""
    try:
        with session_scope() as session:
            row = _row(session, job_id)
            row.last_started_at = started_at
            session.add(row)
            session.commit()

        def _on_progress(done: int, total: int) -> None:
            job.state.update(done=done, total=total)

        with session_scope() as session:
            result = job.run(session, _on_progress) or {}
        job.state.update(result)
        if result.get("error"):
            status, message = "error", str(result["error"])
        else:
            message = job.summary(result)
    except Exception as exc:  # noqa: BLE001 -- surface any failure on the jobs page
        logger.exception("job %s failed", job_id)
        status, message = "error", str(exc) or exc.__class__.__name__
        job.state["error"] = message
    finally:
        job.state["running"] = False
        try:
            with session_scope() as session:
                row = _row(session, job_id)
                row.last_started_at = started_at
                row.last_finished_at = datetime.now(timezone.utc)
                row.last_status = status
                row.last_message = f"[{trigger}] {message}".strip()
                session.add(row)
                session.commit()
        finally:
            job.lock.release()
    logger.info("job %s (%s) done: %s %s", job_id, trigger, status, message)
    return True


def start_in_background(job_id: str) -> bool:
    """Start a manual run in a daemon thread. Returns False if the job is
    already running (scheduled or manual)."""
    job = JOBS[job_id]
    if job.lock.locked():
        return False
    # Flip the flag right away so the page we redirect to already shows the
    # progress bar, before the thread has had a chance to take the lock.
    job.state["running"] = True
    threading.Thread(target=run_job, args=(job_id, "manuel"), daemon=True, name=f"job-{job_id}").start()
    return True
