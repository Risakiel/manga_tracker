"""Orchestrates the external sync sources against the Manga table.

Called both by the APScheduler jobs (scheduler.py) and by the manual
"sync now" API endpoints -- always through the functions here so there is a
single code path (and a single place throttling/logging happens).
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx
from sqlmodel import Session, select

from app.models import Category, Manga, Status, SyncLog, SyncSource, SyncStatus
from app.services import anilist_client, komga_client, library_client, mangaupdates_client, suwayomi_client
from app.services.matching import best_match, normalize_title

logger = logging.getLogger(__name__)

_KEYWORD_STATUS = {
    "hiatus": Status.hiatus,
    "dropped": Status.dropped,
    "cancelled": Status.cancelled,
    "canceled": Status.cancelled,
}


def _derive_status(status_raw: str, completed: bool) -> Status:
    lowered = status_raw.lower()
    for keyword, status in _KEYWORD_STATUS.items():
        if keyword in lowered:
            return status
    return Status.complete if completed else Status.ongoing


def sync_manga_with_mangaupdates(session: Session, manga: Manga, client: httpx.Client | None = None) -> None:
    try:
        series, candidates = mangaupdates_client.resolve_series(
            manga.mangaupdates_url, manga.title_en, client=client
        )
    except httpx.HTTPError as exc:
        manga.sync_error = str(exc)
        session.add(SyncLog(manga_id=manga.id, source=SyncSource.mangaupdates, status=SyncStatus.error, message=str(exc)))
        session.add(manga)
        session.commit()
        return

    if series is None:
        manga.needs_manual_match = True
        manga.match_candidates = [
            {"series_id": c.series_id, "title": c.title, "url": c.url, "year": c.year} for c in candidates
        ]
        manga.sync_error = "no unambiguous MangaUpdates match; manual review needed"
        session.add(
            SyncLog(
                manga_id=manga.id,
                source=SyncSource.mangaupdates,
                status=SyncStatus.error,
                message=f"{len(candidates)} candidate(s) found, manual match required",
            )
        )
        session.add(manga)
        session.commit()
        return

    manga.mangaupdates_id = series.series_id
    manga.needs_manual_match = False
    manga.match_candidates = []
    manga.status_raw = series.status_raw
    manga.status = _derive_status(series.status_raw, series.completed)
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
    session.add(SyncLog(manga_id=manga.id, source=SyncSource.mangaupdates, status=SyncStatus.success, message=series.title))
    session.commit()


def sync_all_mangaupdates(session: Session) -> int:
    mangas = session.exec(select(Manga)).all()
    count = 0
    with httpx.Client(timeout=15.0) as client:
        for manga in mangas:
            sync_manga_with_mangaupdates(session, manga, client=client)
            count += 1
    return count


def enrich_with_anilist(session: Session, manga: Manga, client: httpx.Client | None = None) -> None:
    """Fill gaps left by MangaUpdates (cover/alt titles) -- never overrides existing values."""
    if manga.cover_url and manga.alt_titles:
        return
    try:
        media = anilist_client.search_media(manga.title_en, client=client)
    except httpx.HTTPError as exc:
        session.add(SyncLog(manga_id=manga.id, source=SyncSource.anilist, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return

    if media is None:
        return

    if not manga.cover_url:
        manga.cover_url = media.cover_url
    if not manga.alt_titles:
        manga.alt_titles = [s for s in [media.title_romaji, media.title_native, *media.synonyms] if s]
    manga.updated_at = datetime.now(timezone.utc)
    session.add(manga)
    session.add(SyncLog(manga_id=manga.id, source=SyncSource.anilist, status=SyncStatus.success, message="enriched"))
    session.commit()


def sync_suwayomi_library(
    session: Session, progress_callback: Optional[Callable[[int, int], None]] = None
) -> dict:
    """Reconcile the live Suwayomi library against tracked mangas.

    Suwayomi is the source of truth for *which* manga exist: anything in the
    library that isn't already tracked gets created automatically (type
    guessed from Suwayomi's genre tags, corrigible later), then given an
    immediate best-effort MangaUpdates sync so it isn't left completely
    blank. Suwayomi's own categories (its user-organized library groupings)
    are copied onto every matched/created manga either way.
    """
    try:
        library = suwayomi_client.fetch_library()
    except suwayomi_client.SuwayomiUnavailable as exc:
        session.add(SyncLog(manga_id=None, source=SyncSource.suwayomi, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return {"matched": 0, "created": 0, "error": str(exc)}

    mangas = session.exec(select(Manga)).all()
    by_folder = {normalize_title(m.server_folder): m for m in mangas}
    by_title = {m.id: m.title_en for m in mangas if m.id is not None}

    matched = 0
    created = 0
    total = len(library)
    # Some libraries have the exact same title added twice under different
    # source IDs (confirmed on a real library). Without this, both entries
    # would match the same tracked row and the second silently overwrites the
    # first's suwayomi_manga_id -- track claims so a duplicate falls through
    # to auto-create instead, since it's a genuinely distinct library item.
    claimed_ids: set[int] = set()
    with httpx.Client(timeout=15.0) as mu_client:
        for i, entry in enumerate(library, start=1):
            manga = by_folder.get(normalize_title(entry.title))
            if manga is not None and manga.id in claimed_ids:
                manga = None
            if manga is None:
                match = best_match(entry.title, by_title)
                if match is not None:
                    manga_id, _score = match
                    if manga_id not in claimed_ids:
                        manga = next((m for m in mangas if m.id == manga_id), None)

            if manga is not None:
                claimed_ids.add(manga.id)
                manga.suwayomi_manga_id = entry.id
                manga.suwayomi_chapter_count = entry.download_count
                manga.suwayomi_categories = entry.categories
                manga.updated_at = datetime.now(timezone.utc)
                session.add(manga)
                session.commit()
                matched += 1
            else:
                guessed_category = Category.pornhwa if entry.looks_adult else Category.manga
                server_folder = library_client.suggest_folder(entry.title, guessed_category) or entry.title
                manga = Manga(
                    category=guessed_category,
                    title_en=entry.title,
                    server_folder=server_folder,
                    mangaupdates_url="",
                    suwayomi_manga_id=entry.id,
                    suwayomi_chapter_count=entry.download_count,
                    cover_url=entry.thumbnail_url or "",
                    author=entry.author or "",
                    artist=entry.artist or "",
                )
                manga.suwayomi_categories = entry.categories
                session.add(manga)
                session.commit()
                session.refresh(manga)
                mangas.append(manga)
                by_folder[normalize_title(manga.server_folder)] = manga
                by_title[manga.id] = manga.title_en
                created += 1

                # Best-effort auto-match against MangaUpdates by title so a
                # freshly-imported manga isn't left completely blank; falls
                # back to needs_manual_match with candidates if ambiguous.
                sync_manga_with_mangaupdates(session, manga, client=mu_client)

            if progress_callback:
                progress_callback(i, total)

    session.add(
        SyncLog(
            manga_id=None,
            source=SyncSource.suwayomi,
            status=SyncStatus.success,
            message=f"matched={matched} created={created}",
        )
    )
    session.commit()
    return {"matched": matched, "created": created}


def _push_metadata_to_komga(manga: Manga, series: "komga_client.KomgaSeries", client: httpx.Client) -> bool:
    """Non-destructive enrichment: only fills Komga fields that are genuinely
    empty and unlocked there -- never overwrites anything already set,
    whether by the user or another tool (e.g. komf)."""
    patch: dict = {}
    if manga.description and not series.summary and not series.summary_locked:
        patch["summary"] = manga.description
    if manga.genres and not series.genres and not series.genres_locked:
        patch["genres"] = manga.genres
    if manga.alt_titles and not series.alternate_titles and not series.alternate_titles_locked:
        # Komga rejects a blank label (400 on the whole patch, not just this
        # field) -- confirmed live: {"fieldName":"alternateTitles[0].label",
        # "message":"must not be blank"}.
        patch["alternateTitles"] = [
            {"label": komga_client.MANGAUPDATES_LINK_LABEL, "title": t} for t in manga.alt_titles
        ]
    if manga.mangaupdates_url and not series.mangaupdates_url and not series.links_locked:
        patch["links"] = series.raw_links + [
            {"label": komga_client.MANGAUPDATES_LINK_LABEL, "url": manga.mangaupdates_url}
        ]

    if not patch:
        return False
    try:
        komga_client.update_metadata(series.id, patch, client=client)
        return True
    except komga_client.KomgaUnavailable:
        return False


def sync_komga_library(
    session: Session, progress_callback: Optional[Callable[[int, int], None]] = None
) -> dict:
    """Reconcile tracked mangas against Komga (the actual reading library):
    pulls real read progress (booksReadCount vs booksCount), and pushes back
    MangaUpdates-sourced metadata Komga is missing (summary/genres/alt
    titles/MangaUpdates link) -- only filling gaps, never overwriting.

    Matching priority: MangaUpdates series_id (both sides usually already
    link to it) > exact folder-name match within the same category > fuzzy
    title match. Komga series with no match are only counted, not created --
    Suwayomi (not Komga) is this app's source of truth for which manga exist.
    """
    try:
        library_ids = komga_client.list_library_ids()
    except komga_client.KomgaUnavailable as exc:
        session.add(SyncLog(manga_id=None, source=SyncSource.komga, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return {"matched": 0, "unmatched": 0, "pushed": 0, "error": str(exc)}

    mangas = session.exec(select(Manga)).all()
    by_folder = {(m.category, normalize_title(m.server_folder)): m for m in mangas}
    by_mu_id: dict[int, Manga] = {}
    for m in mangas:
        series_id = mangaupdates_client.extract_series_id(m.mangaupdates_url) if m.mangaupdates_url else None
        if series_id is not None:
            by_mu_id[series_id] = m

    try:
        entries: list[tuple[Category, komga_client.KomgaSeries]] = []
        for category, library_id in library_ids.items():
            for series in komga_client.fetch_series(library_id):
                entries.append((category, series))
    except komga_client.KomgaUnavailable as exc:
        session.add(SyncLog(manga_id=None, source=SyncSource.komga, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return {"matched": 0, "unmatched": 0, "pushed": 0, "error": str(exc)}

    matched = 0
    unmatched = 0
    pushed = 0
    total = len(entries)
    # Must be a Komga-authenticated client (X-API-Key) -- a bare httpx.Client
    # here previously caused every push to fail with 401, silently, since
    # update_metadata() only errors loudly when it opens its own client.
    with komga_client.open_client() as client:
        for i, (category, series) in enumerate(entries, start=1):
            manga = None
            if series.mangaupdates_url:
                mu_series_id = mangaupdates_client.extract_series_id(series.mangaupdates_url)
                if mu_series_id is not None:
                    manga = by_mu_id.get(mu_series_id)
            if manga is None:
                manga = by_folder.get((category, normalize_title(series.name)))
            if manga is None:
                candidates = {m.id: m.title_en for m in mangas if m.category == category}
                match = best_match(series.name, candidates)
                if match is not None:
                    manga_id, _score = match
                    manga = next((m for m in mangas if m.id == manga_id), None)

            if manga is None:
                unmatched += 1
                if progress_callback:
                    progress_callback(i, total)
                continue

            manga.komga_series_id = series.id
            manga.komga_books_count = series.books_count
            manga.komga_books_read_count = series.books_read_count
            manga.updated_at = datetime.now(timezone.utc)
            session.add(manga)
            session.commit()
            matched += 1

            if _push_metadata_to_komga(manga, series, client=client):
                pushed += 1

            if progress_callback:
                progress_callback(i, total)

    session.add(
        SyncLog(
            manga_id=None,
            source=SyncSource.komga,
            status=SyncStatus.success,
            message=f"matched={matched} unmatched={unmatched} pushed={pushed}",
        )
    )
    session.commit()
    return {"matched": matched, "unmatched": unmatched, "pushed": pushed}
