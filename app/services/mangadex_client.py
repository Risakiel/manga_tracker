"""Client for the public MangaDex API (https://api.mangadex.org).

No API key required. Used as the chapter-tracking fallback when
MangaUpdates has no unambiguous match -- MangaDex, like MangaUpdates,
tracks real scanlated chapter releases (unlike AniList's `chapters` field,
which is only meaningful once a series is complete, see anilist_client.py).
"""

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.services.matching import best_match_with_margin

BASE_URL = "https://api.mangadex.org"
_ID_RE = re.compile(r"/title/([0-9a-fA-F-]{36})")
_HEADERS = {"User-Agent": "MangaTracker/1.0 (+https://github.com/Risakiel/manga_tracker)"}
# Excluded by MangaDex's search by default -- most of this library is
# adult content, so every search must explicitly ask for every rating.
_ALL_CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]


def extract_id_from_url(url: str) -> Optional[str]:
    match = _ID_RE.search(url or "")
    return match.group(1) if match else None


@dataclass
class MangaDexManga:
    id: str
    title: str
    url: str
    description: str = ""
    genres: list[str] = field(default_factory=list)
    latest_chapter: Optional[float] = None


@dataclass
class MangaDexSearchCandidate:
    id: str
    title: str
    url: str


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0, headers=_HEADERS)


def _title_of(attributes: dict) -> str:
    titles = attributes.get("title") or {}
    if titles:
        return titles.get("en") or next(iter(titles.values()), "")
    for alt in attributes.get("altTitles") or []:
        if alt:
            return next(iter(alt.values()), "")
    return ""


def _series_url(manga_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "title"
    return f"https://mangadex.org/title/{manga_id}/{slug}"


def fetch_latest_chapter(manga_id: str, client: Optional[httpx.Client] = None) -> Optional[float]:
    owns_client = client is None
    client = client or _client()
    try:
        resp = client.get(f"{BASE_URL}/manga/{manga_id}/aggregate", params={"translatedLanguage[]": "en"})
        resp.raise_for_status()
        data = resp.json()
        numbers: list[float] = []
        for volume in (data.get("volumes") or {}).values():
            for chapter_key in (volume.get("chapters") or {}).keys():
                try:
                    numbers.append(float(chapter_key))
                except ValueError:
                    continue
        return max(numbers) if numbers else None
    finally:
        if owns_client:
            client.close()


def fetch_manga(manga_id: str, client: Optional[httpx.Client] = None) -> MangaDexManga:
    owns_client = client is None
    client = client or _client()
    try:
        resp = client.get(f"{BASE_URL}/manga/{manga_id}")
        resp.raise_for_status()
        attributes = resp.json()["data"]["attributes"]
        title = _title_of(attributes)
        description = (attributes.get("description") or {}).get("en", "")
        genres = [
            (tag.get("attributes", {}).get("name") or {}).get("en", "")
            for tag in attributes.get("tags", [])
            if tag.get("attributes", {}).get("group") == "genre"
        ]
        return MangaDexManga(
            id=manga_id,
            title=title,
            url=_series_url(manga_id, title),
            description=description,
            genres=[g for g in genres if g],
            latest_chapter=fetch_latest_chapter(manga_id, client=client),
        )
    finally:
        if owns_client:
            client.close()


def search_manga(title: str, limit: int = 5, client: Optional[httpx.Client] = None) -> list[MangaDexSearchCandidate]:
    owns_client = client is None
    client = client or _client()
    try:
        resp = client.get(
            f"{BASE_URL}/manga",
            params={"title": title, "limit": limit, "contentRating[]": _ALL_CONTENT_RATINGS},
        )
        resp.raise_for_status()
        candidates = []
        for entry in resp.json().get("data", []):
            manga_id = entry["id"]
            entry_title = _title_of(entry.get("attributes") or {})
            if not entry_title:
                continue
            candidates.append(
                MangaDexSearchCandidate(id=manga_id, title=entry_title, url=_series_url(manga_id, entry_title))
            )
        return candidates
    finally:
        if owns_client:
            client.close()


def resolve_series(
    mangadex_url: str, title_hint: str, client: Optional[httpx.Client] = None
) -> tuple[Optional[MangaDexManga], list[MangaDexSearchCandidate]]:
    """Try to resolve a series from its URL; fall back to a title search.

    Returns (manga, candidates). `manga` is set when resolution succeeded
    unambiguously. Otherwise `candidates` holds search results for manual review.
    """
    manga_id = extract_id_from_url(mangadex_url)
    if manga_id is not None:
        try:
            return fetch_manga(manga_id, client=client), []
        except httpx.HTTPStatusError:
            pass
        except httpx.HTTPError:
            raise

    candidates = search_manga(title_hint, client=client)
    if len(candidates) == 1:
        return fetch_manga(candidates[0].id, client=client), []
    match = best_match_with_margin(title_hint, {i: c.title for i, c in enumerate(candidates)})
    if match is not None:
        idx, _score = match
        return fetch_manga(candidates[idx].id, client=client), []
    return None, candidates
