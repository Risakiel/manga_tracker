"""Orchestrates the external sync sources against the Manga table.

Called both by the APScheduler jobs (scheduler.py) and by the manual
"sync now" API endpoints -- always through the functions here so there is a
single code path (and a single place throttling/logging happens).
"""

import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx
from sqlmodel import Session, select

from app.config import settings
from app.models import Category, Manga, Status, SyncLog, SyncSource, SyncStatus
from app.services import anilist_client, komga_client, library_client, mangadex_client, mangaupdates_client, suwayomi_client
from app.services.matching import best_match, looks_related, normalize_title

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


def sync_all_manga_sources(
    session: Session, progress_callback: Optional[Callable[[int, int], None]] = None
) -> int:
    """Bulk version of sync_manga_all_sources: MangaUpdates first for every
    manga, falling through to MangaDex/AniList only for whichever ones
    still have no usable chapter count -- an already fully-linked manga
    costs nothing extra here beyond its MangaUpdates re-check. One shared
    client for the whole run (connection reuse matters more here than the
    per-source User-Agent mangadex_client's own client would otherwise set).
    """
    mangas = session.exec(select(Manga)).all()
    total = len(mangas)
    count = 0
    if progress_callback:
        progress_callback(0, total)
    with httpx.Client(timeout=15.0) as client:
        for manga in mangas:
            sync_manga_all_sources(session, manga, client=client)
            count += 1
            if progress_callback:
                progress_callback(count, total)
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


def sync_manga_with_anilist_link(session: Session, manga: Manga, client: httpx.Client | None = None) -> None:
    """Resolve/refresh this manga's own AniList entry (id/url/chapter count)
    via a proper search-with-candidates flow -- unlike enrich_with_anilist
    above, which only ever does a blind single-result search to fill
    metadata gaps and never links anything."""
    try:
        media, candidates = anilist_client.resolve_series(manga.anilist_url or "", manga.title_en, client=client)
    except httpx.HTTPError as exc:
        session.add(SyncLog(manga_id=manga.id, source=SyncSource.anilist, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return

    if media is None:
        manga.anilist_needs_manual_match = bool(candidates)
        manga.anilist_match_candidates = [{"id": c.id, "title": c.title} for c in candidates]
        session.add(manga)
        session.commit()
        return

    manga.anilist_id = media.id
    manga.anilist_url = media.url
    manga.anilist_latest_chapter = media.chapters
    manga.anilist_needs_manual_match = False
    manga.anilist_match_candidates = []
    manga.updated_at = datetime.now(timezone.utc)
    session.add(manga)
    session.add(SyncLog(manga_id=manga.id, source=SyncSource.anilist, status=SyncStatus.success, message=media.title))
    session.commit()


def sync_manga_with_mangadex(session: Session, manga: Manga, client: httpx.Client | None = None) -> None:
    """Resolve/refresh this manga's own MangaDex entry (id/url/chapter
    count), the second link in the MangaUpdates -> MangaDex -> AniList
    fallback chain (see sync_manga_all_sources)."""
    try:
        result, candidates = mangadex_client.resolve_series(manga.mangadex_url or "", manga.title_en, client=client)
    except httpx.HTTPError as exc:
        session.add(SyncLog(manga_id=manga.id, source=SyncSource.mangadex, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return

    if result is None:
        manga.mangadex_needs_manual_match = bool(candidates)
        manga.mangadex_match_candidates = [{"id": c.id, "title": c.title, "url": c.url} for c in candidates]
        session.add(manga)
        session.commit()
        return

    manga.mangadex_id = result.id
    manga.mangadex_url = result.url
    manga.mangadex_latest_chapter = result.latest_chapter
    manga.mangadex_needs_manual_match = False
    manga.mangadex_match_candidates = []
    manga.updated_at = datetime.now(timezone.utc)
    session.add(manga)
    session.add(SyncLog(manga_id=manga.id, source=SyncSource.mangadex, status=SyncStatus.success, message=result.title))
    session.commit()


def sync_manga_all_sources(session: Session, manga: Manga, client: httpx.Client | None = None) -> None:
    """Full per-manga resync: MangaUpdates first (the trusted default), then
    MangaDex (tried whenever there's still no usable chapter count -- it,
    like MangaUpdates, tracks real scanlated releases), then AniList as the
    last resort for linking (only if neither of the above linked at all --
    its own chapter count almost never helps, since `chapters` is only
    meaningful once a series is complete). AniList enrichment (cover/alt
    titles) always runs regardless of whether it got linked above.
    """
    sync_manga_with_mangaupdates(session, manga, client=client)
    if manga.mu_latest_chapter is None:
        sync_manga_with_mangadex(session, manga, client=client)
    if manga.mangaupdates_id is None and manga.mangadex_id is None:
        sync_manga_with_anilist_link(session, manga, client=client)
    enrich_with_anilist(session, manga, client=client)


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


def sync_server_folders(
    session: Session, progress_callback: Optional[Callable[[int, int], None]] = None
) -> dict:
    """Rescans the NAS library and auto-fixes any tracked manga whose
    server_folder no longer matches a real folder in its category -- the
    same fuzzy match already offered as a one-off suggestion on the manga
    detail page and the /server-folders review table, just applied in bulk.
    """
    if not library_client.is_mounted():
        message = f"partage NAS non monté ({settings.library_root})"
        session.add(SyncLog(manga_id=None, source=SyncSource.library, status=SyncStatus.error, message=message))
        session.commit()
        return {"checked": 0, "matched": 0, "unmatched": 0, "error": message}

    folders_by_category = {c: library_client.list_folders(c) for c in Category}
    mangas = session.exec(select(Manga)).all()
    checked = 0
    matched = 0
    unmatched = 0
    total = len(mangas)
    for i, manga in enumerate(mangas, start=1):
        folders = folders_by_category[manga.category]
        if manga.server_folder not in folders:
            checked += 1
            suggestion = library_client.suggest_folder(manga.title_en, manga.category, folders=folders)
            if suggestion:
                manga.server_folder = suggestion
                manga.updated_at = datetime.now(timezone.utc)
                session.add(manga)
                session.commit()
                matched += 1
            else:
                unmatched += 1
        if progress_callback:
            progress_callback(i, total)

    session.add(
        SyncLog(
            manga_id=None,
            source=SyncSource.library,
            status=SyncStatus.success,
            message=f"checked={checked} matched={matched} unmatched={unmatched}",
        )
    )
    session.commit()
    return {"checked": checked, "matched": matched, "unmatched": unmatched}


def _links_with_our_mangaupdates_url(manga: Manga, series: "komga_client.KomgaSeries") -> list[dict]:
    """The series' links with every MangaUpdates one (whatever its label)
    replaced by ours -- other links (user- or komf-added) are kept as-is."""
    other_links = [link for link in series.raw_links if not komga_client.is_mangaupdates_link(link)]
    return other_links + [{"label": komga_client.MANGAUPDATES_LINK_LABEL, "url": manga.mangaupdates_url}]


def _send_komga_patch(
    manga: Manga, series_id: str, patch: dict, labels: dict[str, str], client: httpx.Client
) -> list[str]:
    """Sends `patch` in one call; if Komga rejects it, retries field by
    field so one bad field (Komga 400s the *whole* patch on a single
    invalid value) can't silently keep the others -- the MangaUpdates link
    in particular -- from ever reaching Komga. Failures are recorded on
    `manga.komga_sync_error` for the detail page instead of only a log
    line. Returns the human-readable labels of the fields actually sent."""
    if not patch:
        return []
    try:
        komga_client.update_metadata(series_id, patch, client=client)
        return [labels[k] for k in patch]
    except komga_client.KomgaUnavailable as exc:
        if len(patch) == 1:
            manga.komga_sync_error = f"Échec de l'envoi vers Komga ({labels[next(iter(patch))]}) : {exc.reason}"
            return []
    sent: list[str] = []
    failed: list[str] = []
    for key, value in patch.items():
        try:
            komga_client.update_metadata(series_id, {key: value}, client=client)
            sent.append(labels[key])
        except komga_client.KomgaUnavailable as exc:
            logger.warning("Komga rejected %s for series %s: %s", key, series_id, exc)
            failed.append(labels[key])
    if failed:
        manga.komga_sync_error = f"Komga a refusé : {', '.join(failed)}"
    return sent


def _same_tags(ours: list[str], komga_tags: list[str]) -> bool:
    """Komga stores tags lowercased (its web UI only title-cases them for
    display), so "0-Suwayomi: Terminé" comes back as "0-suwayomi: terminé"
    -- a case-sensitive comparison made every sync re-send the tags of every
    series with a Suwayomi category, forever."""
    def norm(tags: list[str]) -> set[str]:
        return {unicodedata.normalize("NFC", t).strip().casefold() for t in tags}

    return norm(ours) == norm(komga_tags)


_KOMGA_FIELD_LABELS = {
    "summary": "résumé",
    "genres": "genres",
    "alternateTitles": "titres alternatifs",
    "links": "lien MangaUpdates",
    "tags": "tags",
}


def _push_metadata_to_komga(manga: Manga, series: "komga_client.KomgaSeries", client: httpx.Client) -> list[str]:
    """Non-destructive enrichment: only fills Komga fields that are genuinely
    empty and unlocked there -- never overwrites anything already set,
    whether by the user or another tool (e.g. komf).

    Two exceptions:
    - Tags: Suwayomi categories are a live status (e.g. "Terminé" / "scans
      manquants"), not static bio info, so the "0-Suwayomi: <category>"
      tags are actively kept in sync with Suwayomi's current assignment on
      every run -- replacing only that subset so any other tag (user- or
      tool-added) survives untouched.
    - The MangaUpdates link: it is the identity anchor between the two
      apps, so a Komga link pointing to a *different* MangaUpdates series
      than ours (stale, set by komf before a correction here, ...) is
      replaced instead of being kept forever. Only the MangaUpdates link is
      touched; other links stay. A lock set in Komga is still respected.

    Returns the labels of the fields actually sent (empty if nothing to do).
    """
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
    if (
        manga.mangaupdates_url
        and not series.links_locked
        and not mangaupdates_client.same_series_url(manga.mangaupdates_url, series.mangaupdates_url)
    ):
        patch["links"] = _links_with_our_mangaupdates_url(manga, series)
    if not series.tags_locked:
        other_tags = [t for t in series.tags if not komga_client.is_suwayomi_tag(t)]
        suwayomi_tags = [f"{komga_client.SUWAYOMI_TAG_PREFIX}{c}" for c in manga.suwayomi_categories]
        new_tags = other_tags + suwayomi_tags
        if not _same_tags(new_tags, series.tags):
            patch["tags"] = new_tags

    pushed = _send_komga_patch(manga, series.id, patch, _KOMGA_FIELD_LABELS, client)
    if _KOMGA_FIELD_LABELS["links"] in pushed:
        manga.komga_mangaupdates_url = manga.mangaupdates_url
    if _push_cover_if_komga_has_none(manga, series.id, client):
        pushed.append("couverture")
    return pushed


def _push_cover_if_komga_has_none(manga: Manga, series_id: str, client: httpx.Client) -> bool:
    """Routine-sync poster push. Komga with no poster of its own shows the
    first page of the first book (often a blank or text page for webtoons),
    so our cover is uploaded -- but a poster chosen in Komga (uploaded by
    hand, or a cover.jpg next to the files) is kept, same non-destructive
    rule as the other fields. Each cover_url is only handled once."""
    if not manga.cover_url or manga.komga_cover_pushed_url == manga.cover_url:
        return False
    try:
        thumbnails = komga_client.fetch_series_thumbnails(series_id, client=client)
    except komga_client.KomgaUnavailable as exc:
        manga.komga_sync_error = f"Couverture : {exc.reason}"
        return False
    if any(t.get("selected") for t in thumbnails):
        manga.komga_cover_pushed_url = manga.cover_url
        manga.komga_cover_kept = True
        return False
    return push_cover_to_komga(manga, series_id, client=client)


def _apply_komga_series(
    manga: Manga, series: "komga_client.KomgaSeries", library_name: Optional[str], method: str
) -> None:
    """Stores what Komga says about the linked series on the manga row."""
    now = datetime.now(timezone.utc)
    manga.komga_series_id = series.id
    manga.komga_books_count = series.books_count
    manga.komga_books_read_count = series.books_read_count
    manga.komga_series_name = series.name
    manga.komga_library_name = library_name
    manga.komga_folder_path = series.url or None
    manga.komga_mangaupdates_url = series.mangaupdates_url
    manga.komga_links_locked = series.links_locked
    manga.komga_match_method = method
    manga.komga_synced_at = now
    manga.komga_sync_error = None
    manga.updated_at = now


def sync_manga_with_komga(
    session: Session, manga: Manga, client: Optional[httpx.Client] = None, method: Optional[str] = None
) -> list[str]:
    """Refreshes one manga from its stored Komga series ID and pushes the
    routine (non-destructive) metadata to it -- used by the detail page's
    "Komga" box, and right after the MangaUpdates link changes here so the
    correction reaches Komga immediately instead of on the next full sync.
    Errors are stored on `manga.komga_sync_error`, never raised."""
    if not manga.komga_series_id:
        return []
    owns_client = client is None
    client = client or komga_client.open_client()
    pushed: list[str] = []
    try:
        series = komga_client.fetch_series_by_id(manga.komga_series_id, client=client)
        library_name = komga_client.fetch_library_name(series.library_id, client=client)
        _apply_komga_series(manga, series, library_name, method or manga.komga_match_method or "id")
        pushed = _push_metadata_to_komga(manga, series, client=client)
    except komga_client.KomgaUnavailable as exc:
        manga.komga_sync_error = f"Série Komga {manga.komga_series_id} inaccessible : {exc.reason}"
    finally:
        if owns_client:
            client.close()
    session.add(manga)
    session.commit()
    return pushed


def link_manga_to_komga_series(session: Session, manga: Manga, raw_value: str) -> Optional[str]:
    """Pins a manga to a Komga series entered by hand (bare ID or any Komga
    URL), or unlinks it when `raw_value` is blank. Returns an error message
    for the UI, or None on success. The series is checked to exist before
    anything is saved, so a typo can't break an existing correct link."""
    if not raw_value.strip():
        for attr in (
            "komga_series_id", "komga_books_count", "komga_books_read_count", "komga_series_name",
            "komga_library_name", "komga_folder_path", "komga_mangaupdates_url", "komga_links_locked",
            "komga_match_method", "komga_synced_at", "komga_sync_error", "komga_cover_pushed_url",
            "komga_cover_kept",
        ):
            setattr(manga, attr, None)
        manga.komga_id_pinned = None
        session.add(manga)
        session.commit()
        return None
    series_id = komga_client.parse_series_id(raw_value)
    if series_id is None:
        return "ID ou lien Komga invalide"
    try:
        with komga_client.open_client() as client:
            komga_client.fetch_series_by_id(series_id, client=client)
            manga.komga_series_id = series_id
            manga.komga_id_pinned = True
            sync_manga_with_komga(session, manga, client=client, method="manuel")
    except komga_client.KomgaUnavailable as exc:
        return f"Série Komga {series_id} introuvable : {exc.reason}"
    return None


def force_push_metadata_to_komga(manga: Manga, series: "komga_client.KomgaSeries", client: httpx.Client) -> list[str]:
    """Overwrite-on-purpose variant of _push_metadata_to_komga, used only by
    the explicit "Identify" action below: this app is meant to become the
    master copy of a manga's identity, replacing what used to be komf's job,
    so a freshly identified field replaces whatever Komga has -- unlike the
    routine sync above, which only ever fills a gap. Per-field locks set by
    hand in Komga are still respected either way; that's what a lock is for.

    Returns the human-readable list of fields actually sent, for the
    "Identify" recap -- empty if there was nothing to push (everything
    locked, or nothing changed).
    """
    patch: dict = {}
    if not series.summary_locked and manga.description:
        patch["summary"] = manga.description
    if not series.genres_locked and manga.genres:
        patch["genres"] = manga.genres
    if not series.alternate_titles_locked and manga.alt_titles:
        patch["alternateTitles"] = [
            {"label": komga_client.MANGAUPDATES_LINK_LABEL, "title": t} for t in manga.alt_titles
        ]
    if not series.links_locked and manga.mangaupdates_url:
        patch["links"] = _links_with_our_mangaupdates_url(manga, series)
    if not series.tags_locked:
        other_tags = [t for t in series.tags if not komga_client.is_suwayomi_tag(t)]
        suwayomi_tags = [f"{komga_client.SUWAYOMI_TAG_PREFIX}{c}" for c in manga.suwayomi_categories]
        new_tags = other_tags + suwayomi_tags
        if not _same_tags(new_tags, series.tags):
            patch["tags"] = new_tags

    sent = _send_komga_patch(manga, series.id, patch, _KOMGA_FIELD_LABELS, client)
    if _KOMGA_FIELD_LABELS["links"] in sent:
        manga.komga_mangaupdates_url = manga.mangaupdates_url
    return sent


# Some image CDNs answer a bare "python-httpx" user agent with 403.
_IMAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MangaTracker)"}


def push_cover_to_komga(manga: Manga, series_id: str, client: httpx.Client) -> bool:
    """Downloads the manga's current cover (from whichever source it came
    from -- MangaUpdates/MangaDex/AniList's own CDN, not Komga) and uploads
    it as the series' selected poster. Called unconditionally by Identify
    ("master copy" reasoning, see force_push_metadata_to_komga) and by the
    routine sync only when Komga has no poster of its own. Failures are
    recorded on `manga.komga_sync_error` so they show in the Komga box."""
    if not manga.cover_url:
        return False
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers=_IMAGE_HEADERS) as image_client:
            resp = image_client.get(manga.cover_url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("could not download cover %s for Komga push: %s", manga.cover_url, exc)
        manga.komga_sync_error = f"Couverture : téléchargement impossible ({exc})"
        return False
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        manga.komga_sync_error = f"Couverture : {manga.cover_url} n'est pas une image ({content_type})"
        return False
    try:
        komga_client.upload_series_thumbnail(series_id, resp.content, content_type, client=client)
    except komga_client.KomgaUnavailable as exc:
        logger.warning("could not upload cover to Komga series %s: %s", series_id, exc)
        manga.komga_sync_error = f"Couverture refusée par Komga : {exc.reason}"
        return False
    manga.komga_cover_pushed_url = manga.cover_url
    manga.komga_cover_kept = False
    return True


_KOMGA_AUTHOR_ROLE = "writer"
_KOMGA_ARTIST_ROLE = "penciller"


def _split_names(value: str) -> list[str]:
    return [n.strip() for n in value.split(",") if n.strip()]


def push_authors_to_komga(manga: Manga, series_id: str, client: httpx.Client) -> int:
    """Komga has no series-level author field -- the "Writers"/"Pencillers"
    shown on the series page are aggregated live from each book's own
    metadata, so replacing them means iterating every book. Only the
    writer/penciller roles are replaced; any other role a book already has
    (translator, letterer, ...) is left alone, and a book with its authors
    locked in Komga is skipped entirely. Returns how many books were
    actually updated, for the "Identify" recap."""
    if not manga.author and not manga.artist:
        return 0
    try:
        books = komga_client.fetch_books(series_id, client=client)
    except komga_client.KomgaUnavailable as exc:
        logger.warning("could not list Komga books for series %s: %s", series_id, exc)
        return 0

    updated = 0
    for book in books:
        if book.authors_locked:
            continue
        new_authors = [a for a in book.authors if a.get("role") not in (_KOMGA_AUTHOR_ROLE, _KOMGA_ARTIST_ROLE)]
        new_authors += [{"name": name, "role": _KOMGA_AUTHOR_ROLE} for name in _split_names(manga.author)]
        new_authors += [{"name": name, "role": _KOMGA_ARTIST_ROLE} for name in _split_names(manga.artist)]
        if new_authors == book.authors:
            continue
        try:
            komga_client.update_book_metadata(book.id, {"authors": new_authors}, client=client)
            updated += 1
        except komga_client.KomgaUnavailable as exc:
            logger.warning("could not update authors for Komga book %s: %s", book.id, exc)
    return updated


IDENTIFY_SOURCE_LABELS = {"mangaupdates": "MangaUpdates", "mangadex": "MangaDex", "anilist": "AniList"}


@dataclass
class IdentifyCandidate:
    source: str
    source_label: str
    external_id: str
    title: str
    cover_url: str
    url: str


def search_identify_candidates(title: str) -> list[IdentifyCandidate]:
    """Search all 3 integrated sources at once for the "Identify" modal --
    the same job Komf's own identify search does against Komga directly,
    just run from here instead. Each source is best-effort: one source
    being down/rate-limited shouldn't hide results from the others."""
    candidates: list[IdentifyCandidate] = []
    try:
        for c in mangaupdates_client.search_series(title):
            candidates.append(
                IdentifyCandidate("mangaupdates", IDENTIFY_SOURCE_LABELS["mangaupdates"], str(c.series_id), c.title, c.cover_url, c.url)
            )
    except httpx.HTTPError as exc:
        logger.warning("MangaUpdates identify search failed: %s", exc)
    try:
        for c in mangadex_client.search_manga(title):
            candidates.append(
                IdentifyCandidate("mangadex", IDENTIFY_SOURCE_LABELS["mangadex"], c.id, c.title, c.cover_url, c.url)
            )
    except httpx.HTTPError as exc:
        logger.warning("MangaDex identify search failed: %s", exc)
    try:
        for c in anilist_client.search_candidates(title):
            candidates.append(
                IdentifyCandidate(
                    "anilist", IDENTIFY_SOURCE_LABELS["anilist"], str(c.id), c.title, c.cover_url, f"https://anilist.co/manga/{c.id}"
                )
            )
    except httpx.HTTPError as exc:
        logger.warning("AniList identify search failed: %s", exc)
    return candidates


@dataclass
class IdentifyResult:
    source_label: str
    title: str
    komga_linked: bool
    komga_pushed: list[str]
    komga_error: Optional[str] = None


def apply_identify(session: Session, manga: Manga, source: str, external_id: str) -> IdentifyResult:
    """Apply the user's pick from the "Identify" modal: re-link the chosen
    source exactly like its dedicated manual-match route does, then -- since
    this app is meant to be the master copy now -- force-push the result to
    Komga if this manga is already linked there."""
    now = datetime.now(timezone.utc)
    if source == "mangaupdates":
        series = mangaupdates_client.fetch_series(int(external_id))
        manga.mangaupdates_id = series.series_id
        manga.mangaupdates_url = series.url
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
        title = series.title
    elif source == "mangadex":
        result = mangadex_client.fetch_manga(external_id)
        manga.mangadex_id = result.id
        manga.mangadex_url = result.url
        manga.mangadex_latest_chapter = result.latest_chapter
        manga.mangadex_needs_manual_match = False
        manga.mangadex_match_candidates = []
        title = result.title
    elif source == "anilist":
        media = anilist_client.fetch_media_by_id(int(external_id))
        manga.anilist_id = media.id
        manga.anilist_url = media.url
        manga.anilist_latest_chapter = media.chapters
        manga.anilist_needs_manual_match = False
        manga.anilist_match_candidates = []
        title = media.title
    else:
        raise ValueError(f"unknown identify source: {source}")

    manga.last_synced_at = now
    manga.updated_at = now
    session.add(manga)
    session.add(
        SyncLog(manga_id=manga.id, source=SyncSource(source), status=SyncStatus.success, message=f"identified via UI: {title}")
    )
    session.commit()

    komga_linked = bool(manga.komga_series_id)
    komga_pushed: list[str] = []
    komga_error = None
    if komga_linked:
        manga.komga_sync_error = None
        try:
            with komga_client.open_client() as client:
                series = komga_client.fetch_series_by_id(manga.komga_series_id, client=client)
                komga_pushed.extend(force_push_metadata_to_komga(manga, series, client=client))
                if push_cover_to_komga(manga, manga.komga_series_id, client=client):
                    komga_pushed.append("couverture")
                authors_updated = push_authors_to_komga(manga, manga.komga_series_id, client=client)
                if authors_updated:
                    komga_pushed.append(f"auteur/artiste ({authors_updated} tome(s))")
        except komga_client.KomgaUnavailable as exc:
            komga_error = str(exc)
        komga_error = komga_error or manga.komga_sync_error
        session.add(manga)
        session.commit()

    return IdentifyResult(
        source_label=IDENTIFY_SOURCE_LABELS[source],
        title=title,
        komga_linked=komga_linked,
        komga_pushed=komga_pushed,
        komga_error=komga_error,
    )


def link_new_komga_series(
    session: Session, progress_callback: Optional[Callable[[int, int], None]] = None
) -> dict:
    """Fast variant of sync_komga_library for frequent scheduling: only
    looks at Komga series not linked to any manga yet (typically a series
    Komga just picked up after a Suwayomi download landed on the NAS) and
    tries to pair each one with a manga that has no Komga ID yet, with the
    same matching rules. Newly linked series get the usual routine push
    (MangaUpdates link, missing metadata, Suwayomi tags) right away;
    already-linked series are left untouched (no progress refresh)."""
    return sync_komga_library(session, progress_callback=progress_callback, only_new=True)


def sync_komga_library(
    session: Session,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    only_new: bool = False,
) -> dict:
    """Reconcile tracked mangas against Komga (the actual reading library):
    pulls real read progress (booksReadCount vs booksCount) plus what Komga
    knows about each series (name, library, folder, MangaUpdates link) for
    the detail page's "Komga" box, pushes back MangaUpdates-sourced metadata
    Komga is missing (summary/genres/alt titles) -- only filling gaps, never
    overwriting -- keeps Komga's MangaUpdates link aligned with ours, and
    mirrors each manga's Suwayomi categories onto Komga as "0-Suwayomi:
    <category>" tags, filterable in Komga's own UI and any client reading
    the same API (e.g. Komelia).

    Matching priority:
    1. the Komga series ID already stored on the manga (from a previous
       sync, or pinned by hand on the detail page) -- the only fully
       reliable link, since titles/folders/MangaUpdates links can all
       drift; a stored ID whose series is missing from the Manga/Pornhwa
       libraries is looked up directly, so a series pinned in another
       library (or moved) isn't lost;
    2. MangaUpdates series_id (only trusted if the series still plausibly
       looks like the same manga -- see the sanity check below);
    3. exact folder-name match within the same category;
    4. fuzzy title match.
    Steps 2-4 only ever consider mangas not already linked to a series
    seen in this run, so two Komga series can't fight over one manga, and a
    manga pinned by hand is never re-linked automatically. Komga series
    with no match are only counted, not created -- Suwayomi (not Komga) is
    this app's source of truth for which manga exist.
    """
    try:
        library_ids = komga_client.list_library_ids()
    except komga_client.KomgaUnavailable as exc:
        session.add(SyncLog(manga_id=None, source=SyncSource.komga, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return {"matched": 0, "unmatched": 0, "pushed": 0, "error": str(exc)}
    library_names = {library_id: library_client.CATEGORY_DIR_NAMES[category] for category, library_id in library_ids.items()}

    # Best-effort: ask Komga to (re)scan now rather than wait on its own
    # internal schedule, so a Suwayomi download that just finished gets
    # picked up sooner. Async on Komga's side, so it may only be reflected
    # on the *next* sync run -- a failure here must not abort this one.
    with komga_client.open_client() as scan_client:
        for library_id in library_ids.values():
            try:
                komga_client.trigger_library_scan(library_id, client=scan_client)
            except komga_client.KomgaUnavailable as exc:
                logger.warning("could not trigger Komga library scan (%s): %s", library_id, exc)

    mangas = session.exec(select(Manga)).all()

    try:
        entries: list[tuple[Category, komga_client.KomgaSeries]] = []
        for category, library_id in library_ids.items():
            for series in komga_client.fetch_series(library_id):
                entries.append((category, series))
    except komga_client.KomgaUnavailable as exc:
        session.add(SyncLog(manga_id=None, source=SyncSource.komga, status=SyncStatus.error, message=str(exc)))
        session.commit()
        return {"matched": 0, "unmatched": 0, "pushed": 0, "error": str(exc)}

    if only_new:
        already_linked = {m.komga_series_id for m in mangas if m.komga_series_id}
        entries = [(category, series) for category, series in entries if series.id not in already_linked]

    matched = 0
    unmatched = 0
    pushed = 0
    total = len(entries)
    # Must be a Komga-authenticated client (X-API-Key) -- a bare httpx.Client
    # here previously caused every push to fail with 401, silently, since
    # update_metadata() only errors loudly when it opens its own client.
    with komga_client.open_client() as client:
        # Pass 1: stored IDs. A manga whose stored series wasn't listed is
        # looked up directly; if it's really gone (deleted, or re-created
        # under a new ID by a Komga rescan) an unpinned manga falls back to
        # the other strategies below, a pinned one keeps its ID and shows
        # the error instead.
        listed_ids = {series.id for _category, series in entries}
        linked: dict[str, Manga] = {}
        for manga in mangas:
            if not manga.komga_series_id:
                continue
            if only_new:
                # Any manga that already has an ID stays out of the
                # matching, whether or not its series was listed.
                linked[manga.komga_series_id] = manga
                continue
            if manga.komga_series_id in listed_ids:
                linked[manga.komga_series_id] = manga
                continue
            try:
                series = komga_client.fetch_series_by_id(manga.komga_series_id, client=client)
            except komga_client.KomgaUnavailable as exc:
                manga.komga_sync_error = f"Série Komga {manga.komga_series_id} introuvable : {exc.reason}"
                session.add(manga)
                if manga.komga_id_pinned:
                    linked[manga.komga_series_id] = manga
                continue
            library_name = library_names.get(series.library_id) or komga_client.fetch_library_name(
                series.library_id, client=client
            )
            _apply_komga_series(manga, series, library_name, manga.komga_match_method or "id")
            session.add(manga)
            session.commit()
            linked[series.id] = manga
            matched += 1
            if _push_metadata_to_komga(manga, series, client=client):
                pushed += 1
            session.add(manga)
            session.commit()

        claimed_manga_ids = {m.id for m in linked.values()}
        free = [m for m in mangas if m.id not in claimed_manga_ids]
        by_folder = {(m.category, normalize_title(m.server_folder)): m for m in free}
        by_mu_id: dict[int, Manga] = {}
        for m in free:
            mu_series_id = mangaupdates_client.extract_series_id(m.mangaupdates_url) if m.mangaupdates_url else None
            if mu_series_id is not None:
                by_mu_id[mu_series_id] = m

        for i, (category, series) in enumerate(entries, start=1):
            manga = linked.get(series.id)
            method = (manga.komga_match_method or "id") if manga is not None else None
            if manga is None and series.mangaupdates_url:
                mu_series_id = mangaupdates_client.extract_series_id(series.mangaupdates_url)
                if mu_series_id is not None:
                    candidate = by_mu_id.get(mu_series_id)
                    # The MangaUpdates link already stored on the Komga side
                    # can be stale or simply wrong (set by hand, or by
                    # another tool like komf) -- only trust it if the series
                    # still plausibly looks like the same manga, so a bad
                    # link doesn't silently pair this series with an
                    # unrelated tracked manga. A rejected candidate still
                    # gets a fair shot below via folder/fuzzy-title matching.
                    if candidate is not None and candidate.id not in claimed_manga_ids and (
                        normalize_title(candidate.server_folder) == normalize_title(series.name)
                        or looks_related(candidate.title_en, series.name)
                    ):
                        manga, method = candidate, "mangaupdates"
            if manga is None:
                candidate = by_folder.get((category, normalize_title(series.name)))
                if candidate is not None and candidate.id not in claimed_manga_ids:
                    manga, method = candidate, "dossier"
            if manga is None:
                candidates = {
                    m.id: m.title_en for m in free if m.category == category and m.id not in claimed_manga_ids
                }
                match = best_match(series.name, candidates)
                if match is not None:
                    manga_id, _score = match
                    manga = next((m for m in free if m.id == manga_id), None)
                    method = "titre"

            if manga is None:
                unmatched += 1
                if progress_callback:
                    progress_callback(i, total)
                continue

            claimed_manga_ids.add(manga.id)
            _apply_komga_series(manga, series, library_names.get(series.library_id), method)
            session.add(manga)
            session.commit()
            matched += 1

            if _push_metadata_to_komga(manga, series, client=client):
                pushed += 1
            session.add(manga)
            session.commit()

            if progress_callback:
                progress_callback(i, total)

    session.add(
        SyncLog(
            manga_id=None,
            source=SyncSource.komga,
            status=SyncStatus.success,
            message=f"{'new series only: ' if only_new else ''}matched={matched} unmatched={unmatched} pushed={pushed}",
        )
    )
    session.commit()
    return {"matched": matched, "unmatched": unmatched, "pushed": pushed}
