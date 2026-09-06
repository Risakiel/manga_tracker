"""Client for the Komga REST API (https://komga.org/docs/openapi/).

Auth via the `X-API-Key` header (create one in Komga: Settings > API Keys).
Editing series metadata requires an ADMIN-role account server-side.

Field names and the PATCH request/response shape below were verified live
against a real Komga instance during implementation, including a real
write-then-revert round trip on the metadata endpoint.
"""

from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings
from app.models import Category
from app.services.library_client import CATEGORY_DIR_NAMES

MANGAUPDATES_LINK_LABEL = "MangaUpdates"


class KomgaUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class KomgaSeries:
    id: str
    library_id: str
    name: str
    books_count: int = 0
    books_read_count: int = 0
    summary: str = ""
    genres: list[str] = field(default_factory=list)
    alternate_titles: list[str] = field(default_factory=list)
    mangaupdates_url: Optional[str] = None
    raw_links: list[dict] = field(default_factory=list)
    summary_locked: bool = False
    genres_locked: bool = False
    links_locked: bool = False
    alternate_titles_locked: bool = False


def _base_url() -> str:
    return settings.komga_url.rstrip("/")


def series_web_url(series_id: str) -> str:
    return f"{_base_url()}/series/{series_id}"


def open_client() -> httpx.Client:
    """An httpx.Client pre-authenticated with the Komga API key.

    Callers that need to make several Komga calls (e.g. a fetch loop
    followed by metadata pushes) should open one of these and pass it
    explicitly -- every function here defaults to opening (and closing) its
    own otherwise, which works but reopens a connection each time.
    """
    return httpx.Client(timeout=30.0, headers={"X-API-Key": settings.komga_api_key})


# Kept as an internal alias so existing call sites below don't need to change.
_client = open_client


def _get(client: httpx.Client, path: str, **kwargs) -> dict:
    resp = client.get(f"{_base_url()}{path}", **kwargs)
    resp.raise_for_status()
    return resp.json()


def list_library_ids(client: Optional[httpx.Client] = None) -> dict[Category, str]:
    """Maps our Category values to their Komga library ID, matched by name.

    Only "Manga" and "Pornhwa" libraries are considered -- any other Komga
    library (Hentai, Hentai_FR, ...) is ignored, matching the scope of the
    "Dossier serveur" NAS browsing feature.
    """
    if not settings.komga_url:
        raise KomgaUnavailable("KOMGA_URL is not configured")
    owns_client = client is None
    client = client or _client()
    try:
        libraries = _get(client, "/api/v1/libraries")
        by_name = {lib["name"]: lib["id"] for lib in libraries}
        return {
            category: by_name[dir_name] for category, dir_name in CATEGORY_DIR_NAMES.items() if dir_name in by_name
        }
    except httpx.HTTPError as exc:
        raise KomgaUnavailable(f"could not reach Komga at {settings.komga_url}: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def _parse_series(node: dict) -> KomgaSeries:
    metadata = node.get("metadata") or {}
    mu_url = None
    for link in metadata.get("links") or []:
        if (link.get("label") or "").strip().lower() == MANGAUPDATES_LINK_LABEL.lower():
            mu_url = link.get("url")
            break
    return KomgaSeries(
        id=node["id"],
        library_id=node["libraryId"],
        name=node["name"],
        books_count=node.get("booksCount") or 0,
        books_read_count=node.get("booksReadCount") or 0,
        summary=metadata.get("summary") or "",
        genres=metadata.get("genres") or [],
        alternate_titles=[t["title"] for t in (metadata.get("alternateTitles") or []) if t.get("title")],
        mangaupdates_url=mu_url,
        raw_links=metadata.get("links") or [],
        summary_locked=bool(metadata.get("summaryLock")),
        genres_locked=bool(metadata.get("genresLock")),
        links_locked=bool(metadata.get("linksLock")),
        alternate_titles_locked=bool(metadata.get("alternateTitlesLock")),
    )


def fetch_series(library_id: str, client: Optional[httpx.Client] = None) -> list[KomgaSeries]:
    if not settings.komga_url:
        raise KomgaUnavailable("KOMGA_URL is not configured")
    owns_client = client is None
    client = client or _client()
    series: list[KomgaSeries] = []
    page = 0
    try:
        while True:
            data = _get(client, "/api/v1/series", params={"library_id": library_id, "size": 500, "page": page})
            series.extend(_parse_series(n) for n in data["content"])
            if data.get("last", True):
                break
            page += 1
        return series
    except httpx.HTTPError as exc:
        raise KomgaUnavailable(f"could not reach Komga at {settings.komga_url}: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def update_metadata(series_id: str, patch: dict, client: Optional[httpx.Client] = None) -> None:
    """PATCH partial series metadata. Fields omitted from `patch` are left untouched."""
    owns_client = client is None
    client = client or _client()
    try:
        resp = client.patch(f"{_base_url()}/api/v1/series/{series_id}/metadata", json=patch)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise KomgaUnavailable(f"failed to update Komga series {series_id}: {exc}") from exc
    finally:
        if owns_client:
            client.close()
