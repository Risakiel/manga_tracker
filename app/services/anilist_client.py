"""Secondary enrichment source: AniList GraphQL API (https://graphql.anilist.co).

Free, no API key, ~90 requests/minute. Used only to fill gaps MangaUpdates left
empty (cover image, alternate titles, adult flag) -- never to override a
MangaUpdates-sourced field, and never applied automatically when the title
search is ambiguous.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings

API_URL = "https://graphql.anilist.co"

_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
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
  }
}
"""

_last_request_at = 0.0


@dataclass
class AniListMedia:
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


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    min_interval = settings.anilist_min_request_interval_seconds
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_at = time.monotonic()


def search_media(title: str, client: Optional[httpx.Client] = None) -> Optional[AniListMedia]:
    _throttle()
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(API_URL, json={"query": _QUERY, "variables": {"search": title}})
        resp.raise_for_status()
        payload = resp.json()
        media = (payload.get("data") or {}).get("Media")
        if not media:
            return None
        titles = media.get("title") or {}
        staff_edges = (media.get("staff") or {}).get("edges", [])
        return AniListMedia(
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
    finally:
        if owns_client:
            client.close()
