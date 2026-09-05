import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import Category, Manga, Status, SyncLog
from app.services import mangaupdates_client
from app.services.excel_importer import import_excel
from app.services.suwayomi_client import SuwayomiManga
from app.services.sync_service import (
    enrich_with_anilist,
    sync_all_mangaupdates,
    sync_manga_with_mangaupdates,
    sync_suwayomi_library,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# In-memory cache of the last Suwayomi reconciliation's unmatched entries, keyed
# by suwayomi manga id. A single-process homelab app doesn't need this in the DB.
_last_suwayomi_unmatched: dict[int, SuwayomiManga] = {}


def _filtered_mangas(session: Session, category: str, status: str, q: str, needs_manual_match: str) -> list[Manga]:
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
    mangas.sort(key=lambda m: m.title_en.lower())
    return mangas


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
    session: Session = Depends(get_session),
):
    mangas = _filtered_mangas(session, category, status, q, needs_manual_match)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "mangas": mangas,
            "stats": _compute_stats(session),
            "filters": {"category": category, "status": status, "q": q, "needs_manual_match": needs_manual_match},
            "statuses": [s.value for s in Status],
            "flash": request.query_params.get("flash"),
        },
    )


@router.get("/partials/mangas-table", response_class=HTMLResponse)
def mangas_table_partial(
    request: Request,
    category: str = "",
    status: str = "",
    q: str = "",
    needs_manual_match: str = "",
    session: Session = Depends(get_session),
):
    mangas = _filtered_mangas(session, category, status, q, needs_manual_match)
    return templates.TemplateResponse(request, "_mangas_table.html", {"mangas": mangas})


@router.get("/manga/{manga_id}", response_class=HTMLResponse)
def manga_detail(request: Request, manga_id: int, session: Session = Depends(get_session)):
    manga = session.get(Manga, manga_id)
    if manga is None:
        return HTMLResponse("Manga introuvable", status_code=404)
    return templates.TemplateResponse(request, "detail.html", {"manga": manga})


@router.post("/manga/{manga_id}/sync")
def manga_sync(manga_id: int, session: Session = Depends(get_session)):
    manga = session.get(Manga, manga_id)
    if manga is not None:
        sync_manga_with_mangaupdates(session, manga)
        enrich_with_anilist(session, manga)
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

    def _job():
        with session_scope() as session:
            sync_all_mangaupdates(session)

    background_tasks.add_task(_job)
    return RedirectResponse("/?flash=Synchronisation+MangaUpdates+lancée+en+arrière-plan...", status_code=303)


@router.post("/suwayomi/sync")
def suwayomi_sync(background_tasks: BackgroundTasks):
    from app.database import session_scope

    def _job():
        global _last_suwayomi_unmatched
        with session_scope() as session:
            result = sync_suwayomi_library(session)
            _last_suwayomi_unmatched = {e.id: e for e in result.get("unmatched", [])}

    background_tasks.add_task(_job)
    return RedirectResponse("/unlinked?flash=Synchronisation+Suwayomi+lancée+en+arrière-plan...", status_code=303)


@router.get("/unlinked", response_class=HTMLResponse)
def unlinked(request: Request, session: Session = Depends(get_session)):
    all_mangas = session.exec(select(Manga)).all()
    all_mangas_sorted = sorted(all_mangas, key=lambda m: m.title_en.lower())
    return templates.TemplateResponse(
        request,
        "unlinked.html",
        {
            "unmatched": list(_last_suwayomi_unmatched.values()),
            "all_mangas": all_mangas_sorted,
            "error": None,
            "flash": request.query_params.get("flash"),
        },
    )


@router.post("/unlinked/{suwayomi_id}/link")
def unlinked_link(suwayomi_id: int, manga_id: int = Form(...), session: Session = Depends(get_session)):
    entry = _last_suwayomi_unmatched.pop(suwayomi_id, None)
    manga = session.get(Manga, manga_id)
    if manga is not None and entry is not None:
        manga.suwayomi_manga_id = entry.id
        manga.suwayomi_chapter_count = entry.download_count
        manga.updated_at = datetime.now(timezone.utc)
        session.add(manga)
        session.commit()
    return RedirectResponse("/unlinked", status_code=303)


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
