"""AniList GraphQL API client (https://graphql.anilist.co). Free, no API key,
~90 requests/minute. Two independent roles:

- `search_media` (blind single-result search) backs `enrich_with_anilist` in
  sync_service.py, filling metadata gaps MangaUpdates left empty (cover
  image, alternate titles, adult flag) -- never overriding a
  MangaUpdates-sourced field.
- `resolve_series` (fetch-by-id, or a proper multi-candidate search via
  `search_candidates`) links a manga's own AniList entry, the same
  fetch-or-search-with-candidates shape as mangaupdates_client/
  mangadex_client. Note `AniListMedia.chapters` is only meaningful once a
  series is complete -- see ChapterSource in models.py.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings
from app.services.matching import best_match_with_margin

API_URL = "https://graphql.anilist.co"
_ID_RE = re.compile(r"anilist\.co/manga/(\d+)")

_MEDIA_FIELDS = """
    id
    title { romaji english native }
    synonyms
    status
    chapters
    isAdult
    description(asHtml: false)
    coverImage { large }
    staff(perPage: 5) {
      edges { role node { name { full } } }
    }
"""

_SEARCH_QUERY = f"""
query ($search: String) {{
  Media(search: $search, type: MANGA) {{
    {_MEDIA_FIELDS}
  }}
}}
"""

_BY_ID_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: MANGA) {{
    {_MEDIA_FIELDS}
  }}
}}
"""

_CANDIDATES_QUERY = """
query ($search: String, $perPage: Int) {
  Page(perPage: $perPage) {
    media(search: $search, type: MANGA) {
      id
      title { romaji english native }
    }
  }
}
"""

_last_request_at = 0.0


@dataclass
class AniListMedia:
    id: Optional[int] = None
    title_romaji: str = ""
    title_english: str = ""
    title_native: str = ""
    synonyms: list[str] = field(default_factory=list)
    status: str = ""
    chapters: Optional[int] = None
    is_adult: bool = False
    description: str = ""
    cover_url: str = ""
    staff: list[tuple[str, str]] = field(default_factory=list)  # (role, name)

    @property
    def title(self) -> str:
        return self.title_english or self.title_romaji or self.title_native

    @property
    def url(self) -> str:
        return f"https://anilist.co/manga/{self.id}"


@dataclass
class AniListSearchCandidate:
    id: int
    title: str


def extract_id_from_url(url: str) -> Optional[int]:
    match = _ID_RE.search(url or "")
    return int(match.group(1)) if match else None


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    min_interval = settings.anilist_min_request_interval_seconds
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_at = time.monotonic()


def _parse_media(media: dict) -> AniListMedia:
    titles = media.get("title") or {}
    staff_edges = (media.get("staff") or {}).get("edges", [])
    return AniListMedia(
        id=media.get("id"),
        title_romaji=titles.get("romaji") or "",
        title_english=titles.get("english") or "",
        title_native=titles.get("native") or "",
        synonyms=media.get("synonyms") or [],
        status=media.get("status") or "",
        chapters=media.get("chapters"),
        is_adult=bool(media.get("isAdult")),
        description=media.get("description") or "",
        cover_url=(media.get("coverImage") or {}).get("large", ""),
        staff=[(e.get("role", ""), (e.get("node") or {}).get("name", {}).get("full", "")) for e in staff_edges],
    )


def search_media(title: str, client: Optional[httpx.Client] = None) -> Optional[AniListMedia]:
    _throttle()
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(API_URL, json={"query": _SEARCH_QUERY, "variables": {"search": title}})
        resp.raise_for_status()
        media = (resp.json().get("data") or {}).get("Media")
        return _parse_media(media) if media else None
    finally:
        if owns_client:
            client.close()


def fetch_media_by_id(anilist_id: int, client: Optional[httpx.Client] = None) -> Optional[AniListMedia]:
    _throttle()
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(API_URL, json={"query": _BY_ID_QUERY, "variables": {"id": anilist_id}})
        resp.raise_for_status()
        media = (resp.json().get("data") or {}).get("Media")
        return _parse_media(media) if media else None
    finally:
        if owns_client:
            client.close()


def search_candidates(
    title: str, per_page: int = 5, client: Optional[httpx.Client] = None
) -> list[AniListSearchCandidate]:
    _throttle()
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(API_URL, json={"query": _CANDIDATES_QUERY, "variables": {"search": title, "perPage": per_page}})
        resp.raise_for_status()
        entries = ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []
        candidates = []
        for entry in entries:
            titles = entry.get("title") or {}
            title_text = titles.get("english") or titles.get("romaji") or titles.get("native") or ""
            if entry.get("id") is None or not title_text:
                continue
            candidates.append(AniListSearchCandidate(id=entry["id"], title=title_text))
        return candidates
    finally:
        if owns_client:
            client.close()


def resolve_series(
    anilist_url: str, title_hint: str, client: Optional[httpx.Client] = None
) -> tuple[Optional[AniListMedia], list[AniListSearchCandidate]]:
    """Try to resolve a series from its URL; fall back to a title search.

    Returns (media, candidates). `media` is set when resolution succeeded
    unambiguously. Otherwise `candidates` holds search results for manual review.
    """
    anilist_id = extract_id_from_url(anilist_url)
    if anilist_id is not None:
        media = fetch_media_by_id(anilist_id, client=client)
        if media is not None:
            return media, []

    candidates = search_candidates(title_hint, client=client)
    if len(candidates) == 1:
        return fetch_media_by_id(candidates[0].id, client=client), []
    match = best_match_with_margin(title_hint, {i: c.title for i, c in enumerate(candidates)})
    if match is not None:
        idx, _score = match
        return fetch_media_by_id(candidates[idx].id, client=client), []
    return None, candidates
