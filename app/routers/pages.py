import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import Category, Manga, Status, SyncLog
from app.services import komga_client, library_client, mangaupdates_client
from app.services.excel_importer import import_excel
from app.services.sync_service import (
    enrich_with_anilist,
    sync_komga_library,
    sync_manga_with_mangaupdates,
    sync_suwayomi_library,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# In-memory progress for the bulk background jobs -- all take several
# minutes (one HTTP request per manga, throttled), so the dashboard polls
# these instead of leaving the user staring at a button that looks like it
# did nothing. A single-process homelab app doesn't need this in the DB.
_mu_sync_progress = {"running": False, "total": 0, "done": 0}
_suwayomi_sync_progress = {"running": False, "total": 0, "done": 0, "matched": 0, "created": 0, "error": None}
_komga_sync_progress = {
    "running": False,
    "total": 0,
    "done": 0,
    "matched": 0,
    "unmatched": 0,
    "pushed": 0,
    "error": None,
}


def _filtered_mangas(
    session: Session,
    category: str,
    status: str,
    q: str,
    needs_manual_match: str,
    behind: str = "",
    suwayomi_category: str = "",
) -> list[Manga]:
    statement = select(Manga)
    if category:
        try:
            statement = statement.where(Manga.category == Category(category))
        except ValueError:
            pass
    if status:
        try:
            statement = statement.where(Manga.status == Status(status))
        except ValueError:
            pass
    if needs_manual_match == "true":
        statement = statement.where(Manga.needs_manual_match == True)  # noqa: E712
    mangas = session.exec(statement).all()
    if q:
        q_lower = q.lower()
        mangas = [m for m in mangas if q_lower in m.title_en.lower() or q_lower in m.server_folder.lower()]
    if behind == "true":
        # chapters_behind is a computed property, not a DB column -- filter in Python.
        mangas = [m for m in mangas if (m.chapters_behind or 0) > 0]
    if suwayomi_category:
        # Suwayomi categories are stored as a JSON list, not a DB column -- filter in Python.
        mangas = [m for m in mangas if suwayomi_category in m.suwayomi_categories]
    mangas.sort(key=lambda m: m.title_en.lower())
    return mangas


def _distinct_suwayomi_categories(session: Session) -> list[str]:
    names: set[str] = set()
    for manga in session.exec(select(Manga)).all():
        names.update(manga.suwayomi_categories)
    return sorted(names)


def _compute_stats(session: Session) -> dict:
    all_mangas = session.exec(select(Manga)).all()
    return {
        "total": len(all_mangas),
        "ongoing": sum(1 for m in all_mangas if m.status == Status.ongoing),
        "complete": sum(1 for m in all_mangas if m.status == Status.complete),
        "behind": sum(1 for m in all_mangas if (m.chapters_behind or 0) > 0),
        "needs_match": sum(1 for m in all_mangas if m.needs_manual_match),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    category: str = "",
    status: str = "",
    q: str = "",
    needs_manual_match: str = "",
    behind: str = "",
    suwayomi_category: str = "",
    session: Session = Depends(get_session),
):
    mangas = _filtered_mangas(session, category, status, q, needs_manual_match, behind, suwayomi_category)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "mangas": mangas,
            "stats": _compute_stats(session),
            "filters": {
                "category": category,
                "status": status,
                "q": q,
                "needs_manual_match": needs_manual_match,
                "behind": behind,
                "suwayomi_category": suwayomi_category,
            },
            "statuses": [s.value for s in Status],
            "suwayomi_categories": _distinct_suwayomi_categories(session),
            "flash": request.query_params.get("flash"),
            "progress": _mu_sync_progress,
            "suwayomi_progress": _suwayomi_sync_progress,
        },
    )


@router.get("/partials/mangas-table", response_class=HTMLResponse)
def mangas_table_partial(
    request: Request,
    category: str = "",
    status: str = "",
    q: str = "",
    needs_manual_match: str = "",
    behind: str = "",
    suwayomi_category: str = "",
    session: Session = Depends(get_session),
):
    mangas = _filtered_mangas(session, category, status, q, needs_manual_match, behind, suwayomi_category)
    return templates.TemplateResponse(request, "_mangas_table.html", {"mangas": mangas})


@router.get("/manga/{manga_id}", response_class=HTMLResponse)
def manga_detail(request: Request, manga_id: int, session: Session = Depends(get_session)):
    manga = session.get(Manga, manga_id)
    if manga is None:
        return HTMLResponse("Manga introuvable", status_code=404)
    folder_options = library_client.list_folders(manga.category)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "manga": manga,
            "library_mounted": library_client.is_mounted(),
            "library_root": str(settings.library_root),
            "folder_options": folder_options,
            "suggested_folder": (
                library_client.suggest_folder(manga.title_en, manga.category)
                if folder_options and manga.server_folder not in folder_options
                else None
            ),
            "komga_web_url": (
                komga_client.series_web_url(manga.komga_series_id) if manga.komga_series_id else None
            ),
        },
    )


@router.post("/manga/{manga_id}/sync")
def manga_sync(manga_id: int, session: Session = Depends(get_session)):
    manga = session.get(Manga, manga_id)
    if manga is not None:
        sync_manga_with_mangaupdates(session, manga)
        enrich_with_anilist(session, manga)
    return RedirectResponse(f"/manga/{manga_id}", status_code=303)


@router.post("/manga/{manga_id}/mangaupdates-url")
def manga_set_mangaupdates_url(
    manga_id: int, mangaupdates_url: str = Form(...), session: Session = Depends(get_session)
):
    manga = session.get(Manga, manga_id)
    if manga is not None and mangaupdates_url.strip():
        manga.mangaupdates_url = mangaupdates_url.strip()
        manga.needs_manual_match = False
        manga.match_candidates = []
        manga.sync_error = None
        session.add(manga)
        session.commit()
        # Sync immediately off the new link so the corrected data shows up
        # right away instead of waiting for the next scheduled/manual sync.
        sync_manga_with_mangaupdates(session, manga)
        enrich_with_anilist(session, manga)
    return RedirectResponse(f"/manga/{manga_id}", status_code=303)


@router.post("/manga/{manga_id}/server-folder")
def manga_set_server_folder(
    manga_id: int, server_folder: str = Form(...), session: Session = Depends(get_session)
):
    manga = session.get(Manga, manga_id)
    if manga is not None and server_folder.strip():
        manga.server_folder = server_folder.strip()
        manga.updated_at = datetime.now(timezone.utc)
        session.add(manga)
        session.commit()
    return RedirectResponse(f"/manga/{manga_id}", status_code=303)


@router.post("/manga/{manga_id}/category")
def manga_set_category(manga_id: int, category: str = Form(...), session: Session = Depends(get_session)):
    manga = session.get(Manga, manga_id)
    if manga is not None:
        try:
            manga.category = Category(category)
            manga.updated_at = datetime.now(timezone.utc)
            session.add(manga)
            session.commit()
        except ValueError:
            pass
    return RedirectResponse(f"/manga/{manga_id}", status_code=303)


@router.post("/manga/{manga_id}/manual-match")
def manga_manual_match(
    manga_id: int,
    series_id: Optional[int] = Form(None),
    series_id_custom: Optional[int] = Form(None),
    session: Session = Depends(get_session),
):
    manga = session.get(Manga, manga_id)
    chosen_id = series_id_custom or series_id
    if manga is not None and chosen_id:
        series = mangaupdates_client.fetch_series(chosen_id)
        manga.mangaupdates_id = series.series_id
        manga.mangaupdates_url = series.url
        manga.needs_manual_match = False
        manga.match_candidates = []
        manga.status_raw = series.status_raw
        manga.mu_latest_chapter = series.latest_chapter
        manga.author = ", ".join(series.authors) or manga.author
        manga.artist = ", ".join(series.artists) or manga.artist
        manga.genres = series.genres or manga.genres
        manga.alt_titles = series.associated_titles or manga.alt_titles
        manga.cover_url = series.cover_url or manga.cover_url
        manga.description = series.description or manga.description
        manga.sync_error = None
        manga.last_synced_at = datetime.now(timezone.utc)
        manga.updated_at = datetime.now(timezone.utc)
        session.add(manga)
        session.commit()
    return RedirectResponse(f"/manga/{manga_id}", status_code=303)


@router.post("/sync-all")
def sync_all(background_tasks: BackgroundTasks):
    from app.database import session_scope

    if _mu_sync_progress["running"]:
        return RedirectResponse("/?flash=Une+synchronisation+est+déjà+en+cours...", status_code=303)

    def _job():
        with session_scope() as session:
            mangas = session.exec(select(Manga)).all()
            _mu_sync_progress.update(running=True, total=len(mangas), done=0)
            try:
                with httpx.Client(timeout=15.0) as client:
                    for manga in mangas:
                        sync_manga_with_mangaupdates(session, manga, client=client)
                        _mu_sync_progress["done"] += 1
            finally:
                _mu_sync_progress["running"] = False

    background_tasks.add_task(_job)
    return RedirectResponse("/?flash=Synchronisation+MangaUpdates+lancée+en+arrière-plan...", status_code=303)


@router.get("/partials/sync-progress", response_class=HTMLResponse)
def sync_progress(request: Request):
    return templates.TemplateResponse(request, "_sync_progress.html", {"progress": _mu_sync_progress})


@router.post("/suwayomi/sync")
def suwayomi_sync(background_tasks: BackgroundTasks):
    from app.database import session_scope

    if _suwayomi_sync_progress["running"]:
        return RedirectResponse("/suwayomi?flash=Une+synchronisation+Suwayomi+est+déjà+en+cours...", status_code=303)

    def _job():
        _suwayomi_sync_progress.update(running=True, total=0, done=0, matched=0, created=0, error=None)

        def _on_progress(done: int, total: int) -> None:
            _suwayomi_sync_progress.update(done=done, total=total)

        try:
            with session_scope() as session:
                result = sync_suwayomi_library(session, progress_callback=_on_progress)
                _suwayomi_sync_progress["matched"] = result.get("matched", 0)
                _suwayomi_sync_progress["created"] = result.get("created", 0)
                _suwayomi_sync_progress["error"] = result.get("error")
        finally:
            _suwayomi_sync_progress["running"] = False

    background_tasks.add_task(_job)
    return RedirectResponse("/suwayomi?flash=Synchronisation+Suwayomi+lancée+en+arrière-plan...", status_code=303)


@router.get("/partials/suwayomi-sync-progress", response_class=HTMLResponse)
def suwayomi_sync_progress_partial(request: Request):
    return templates.TemplateResponse(request, "_suwayomi_progress.html", {"progress": _suwayomi_sync_progress})


@router.get("/suwayomi", response_class=HTMLResponse)
def suwayomi_page(request: Request, session: Session = Depends(get_session)):
    unresolved = session.exec(
        select(Manga).where(Manga.mangaupdates_url == "").order_by(Manga.created_at.desc()).limit(50)
    ).all()
    return templates.TemplateResponse(
        request,
        "suwayomi.html",
        {
            "unresolved": unresolved,
            "progress": _suwayomi_sync_progress,
            "flash": request.query_params.get("flash"),
        },
    )


@router.post("/komga/sync")
def komga_sync(background_tasks: BackgroundTasks):
    from app.database import session_scope

    if _komga_sync_progress["running"]:
        return RedirectResponse("/komga?flash=Une+synchronisation+Komga+est+déjà+en+cours...", status_code=303)

    def _job():
        _komga_sync_progress.update(running=True, total=0, done=0, matched=0, unmatched=0, pushed=0, error=None)

        def _on_progress(done: int, total: int) -> None:
            _komga_sync_progress.update(done=done, total=total)

        try:
            with session_scope() as session:
                result = sync_komga_library(session, progress_callback=_on_progress)
                _komga_sync_progress["matched"] = result.get("matched", 0)
                _komga_sync_progress["unmatched"] = result.get("unmatched", 0)
                _komga_sync_progress["pushed"] = result.get("pushed", 0)
                _komga_sync_progress["error"] = result.get("error")
        finally:
            _komga_sync_progress["running"] = False

    background_tasks.add_task(_job)
    return RedirectResponse("/komga?flash=Synchronisation+Komga+lancée+en+arrière-plan...", status_code=303)


@router.get("/partials/komga-sync-progress", response_class=HTMLResponse)
def komga_sync_progress_partial(request: Request):
    return templates.TemplateResponse(request, "_komga_progress.html", {"progress": _komga_sync_progress})


@router.get("/komga", response_class=HTMLResponse)
def komga_page(request: Request, session: Session = Depends(get_session)):
    unmatched_manga = session.exec(
        select(Manga).where(Manga.komga_series_id == None).order_by(Manga.title_en)  # noqa: E711
    ).all()
    return templates.TemplateResponse(
        request,
        "komga.html",
        {
            "komga_configured": bool(settings.komga_url),
            "unmatched_manga": unmatched_manga,
            "progress": _komga_sync_progress,
            "flash": request.query_params.get("flash"),
        },
    )


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return templates.TemplateResponse(request, "import.html", {"summary": None})


@router.post("/import", response_class=HTMLResponse)
def import_post(request: Request, file: UploadFile = File(...), session: Session = Depends(get_session)):
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        summary = import_excel(tmp_path, session)
    finally:
        tmp_path.unlink(missing_ok=True)
    return templates.TemplateResponse(request, "import.html", {"summary": summary})


@router.get("/logs", response_class=HTMLResponse)
def logs(request: Request, session: Session = Depends(get_session)):
    entries = session.exec(select(SyncLog).order_by(SyncLog.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request, "logs.html", {"logs": entries})
