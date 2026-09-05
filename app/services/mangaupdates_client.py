"""Client for the public MangaUpdates API (https://api.mangaupdates.com/v1).

No API key is required for the read-only endpoints used here. MangaUpdates asks
callers to space out requests and cache results, so this client throttles itself
and callers should only invoke it from the scheduler or an explicit manual sync,
never on every page load.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings

BASE_URL = "https://api.mangaupdates.com/v1"
_SLUG_RE = re.compile(r"/series/([0-9a-z]+)/")
_CHAPTER_COUNT_RE = re.compile(r"(\d+)\s*Chapters?", re.IGNORECASE)

_last_request_at = 0.0


def extract_series_id(mangaupdates_url: str) -> Optional[int]:
    """Decode a MangaUpdates URL slug (e.g. .../series/kljw00c/...) into a numeric series_id.

    The new-style MangaUpdates URLs encode the series_id as a base36 string.
    """
    match = _SLUG_RE.search(mangaupdates_url)
    if not match:
        return None
    slug = match.group(1)
    try:
        return int(slug, 36)
    except ValueError:
        return None


@dataclass
class MangaUpdatesSeries:
    series_id: int
    title: str
    url: str
    status_raw: str = ""
    completed: bool = False
    latest_chapter: Optional[float] = None
    description: str = ""
    genres: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    associated_titles: list[str] = field(default_factory=list)
    cover_url: str = ""
    type_: str = ""


@dataclass
class MangaUpdatesSearchCandidate:
    series_id: int
    title: str
    url: str
    year: Optional[str] = None


def _throttle(min_interval: float) -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_at = time.monotonic()


def _parse_series_payload(data: dict) -> MangaUpdatesSeries:
    authors = [a["name"] for a in data.get("authors", []) if a.get("type") == "Author"]
    artists = [a["name"] for a in data.get("authors", []) if a.get("type") == "Artist"]
    status_raw = (data.get("status") or "").strip()

    # For some series (adult-tagged ones especially) MangaUpdates leaves the
    # structured `latest_chapter`/`completed` fields at 0/false even though the
    # free-text status has the real figures -- fall back to parsing that text
    # when the structured field looks unset. Status text sometimes lists
    # several editions ("Original Comic: 30 Chapters ... Tatekomi: 91
    # Chapters ..."); take the largest count found, since that's consistently
    # the one matching what's actually been scanlated/downloaded.
    latest_chapter = data.get("latest_chapter") or None
    if not latest_chapter:
        counts = [int(m) for m in _CHAPTER_COUNT_RE.findall(status_raw)]
        if counts:
            latest_chapter = max(counts)

    completed = bool(data.get("completed")) or "complete" in status_raw.lower()

    return MangaUpdatesSeries(
        series_id=data["series_id"],
        title=data.get("title", ""),
        url=data.get("url", ""),
        status_raw=status_raw,
        completed=completed,
        latest_chapter=latest_chapter,
        description=data.get("description") or "",
        genres=[g["genre"] for g in data.get("genres", []) if g.get("genre")],
        authors=authors,
        artists=artists,
        associated_titles=[a["title"] for a in data.get("associated", []) if a.get("title")],
        cover_url=(data.get("image") or {}).get("url", {}).get("thumb", ""),
        type_=data.get("type", ""),
    )


def fetch_series(series_id: int, client: Optional[httpx.Client] = None) -> MangaUpdatesSeries:
    _throttle(settings.mangaupdates_min_request_interval_seconds)
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.get(f"{BASE_URL}/series/{series_id}")
        resp.raise_for_status()
        return _parse_series_payload(resp.json())
    finally:
        if owns_client:
            client.close()


def search_series(
    title: str, per_page: int = 5, client: Optional[httpx.Client] = None
) -> list[MangaUpdatesSearchCandidate]:
    _throttle(settings.mangaupdates_min_request_interval_seconds)
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(
            f"{BASE_URL}/series/search",
            json={"search": title, "perpage": per_page},
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = []
        for entry in data.get("results", []):
            record = entry.get("record", {})
            if not record.get("series_id"):
                continue
            candidates.append(
                MangaUpdatesSearchCandidate(
                    series_id=record["series_id"],
                    title=record.get("title", ""),
                    url=record.get("url", ""),
                    year=record.get("year"),
                )
            )
        return candidates
    finally:
        if owns_client:
            client.close()


def resolve_series(
    mangaupdates_url: str, title_hint: str, client: Optional[httpx.Client] = None
) -> tuple[Optional[MangaUpdatesSeries], list[MangaUpdatesSearchCandidate]]:
    """Try to resolve a series from its URL; fall back to a title search.

    Returns (series, candidates). `series` is set when resolution succeeded
    unambiguously. Otherwise `candidates` holds search results for manual review.
    """
    series_id = extract_series_id(mangaupdates_url)
    if series_id is not None:
        try:
            return fetch_series(series_id, client=client), []
        except httpx.HTTPStatusError:
            pass
        except httpx.HTTPError:
            raise

    candidates = search_series(title_hint, client=client)
    if len(candidates) == 1:
        return fetch_series(candidates[0].series_id, client=client), []
    return None, candidates
