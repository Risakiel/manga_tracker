"""Client for the Suwayomi Server GraphQL API (`{SUWAYOMI_URL}/api/graphql`).

Field names below were confirmed against the Suwayomi-Server source
(MangaType.kt / MangaQuery.kt) at implementation time, not against a live
instance -- if the deployed server version differs, adjust this query first
(GraphQL introspection against the real instance is the fastest way to check).
"""

from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings

_LIBRARY_QUERY = """
query LibraryMangas($after: Cursor) {
  mangas(condition: { inLibrary: true }, first: 500, after: $after) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      author
      artist
      status
      genre
      thumbnailUrl
      downloadCount
      chapters { totalCount }
      categories { nodes { name } }
    }
  }
}
"""

# Suwayomi genre tags (copied from the source site) that reliably signal adult
# content -- used to auto-classify newly-imported manga as Manga vs Pornhwa.
# Confirmed against this user's real library: "pornhwa"/"pornwha" (137
# occurrences), "hentai", "adulte"/"adult", "hardcore", "smut" all show up
# exclusively on titles already tracked as Pornhwa.
_ADULT_GENRE_KEYWORDS = {"pornhwa", "pornwha", "hentai", "adulte", "adult", "hardcore", "smut"}


@dataclass
class SuwayomiManga:
    id: int
    title: str
    author: Optional[str] = None
    artist: Optional[str] = None
    status: str = ""
    genres: list[str] = field(default_factory=list)
    thumbnail_url: Optional[str] = None
    download_count: int = 0
    chapter_total_count: int = 0
    categories: list[str] = field(default_factory=list)

    @property
    def looks_adult(self) -> bool:
        return any(g.strip().lower() in _ADULT_GENRE_KEYWORDS for g in self.genres)


@dataclass
class SuwayomiUnavailable(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _client() -> httpx.Client:
    auth = None
    if settings.suwayomi_username:
        auth = (settings.suwayomi_username, settings.suwayomi_password)
    return httpx.Client(timeout=30.0, auth=auth)


def fetch_library(client: Optional[httpx.Client] = None) -> list[SuwayomiManga]:
    if not settings.suwayomi_url:
        raise SuwayomiUnavailable("SUWAYOMI_URL is not configured")

    endpoint = settings.suwayomi_url.rstrip("/") + "/api/graphql"
    owns_client = client is None
    client = client or _client()
    mangas: list[SuwayomiManga] = []
    after: Optional[str] = None
    try:
        while True:
            resp = client.post(endpoint, json={"query": _LIBRARY_QUERY, "variables": {"after": after}})
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise SuwayomiUnavailable(str(payload["errors"]))
            connection = payload["data"]["mangas"]
            for node in connection["nodes"]:
                # thumbnailUrl comes back as a server-relative path (e.g.
                # "/api/v1/manga/20/thumbnail"), not a full URL.
                thumbnail_url = node.get("thumbnailUrl")
                if thumbnail_url and thumbnail_url.startswith("/"):
                    thumbnail_url = settings.suwayomi_url.rstrip("/") + thumbnail_url
                mangas.append(
                    SuwayomiManga(
                        id=node["id"],
                        title=node["title"],
                        author=node.get("author"),
                        artist=node.get("artist"),
                        status=node.get("status", ""),
                        genres=node.get("genre") or [],
                        thumbnail_url=thumbnail_url,
                        download_count=node.get("downloadCount") or 0,
                        chapter_total_count=(node.get("chapters") or {}).get("totalCount", 0),
                        categories=[c["name"] for c in (node.get("categories") or {}).get("nodes", [])],
                    )
                )
            page_info = connection["pageInfo"]
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return mangas
    except httpx.HTTPError as exc:
        raise SuwayomiUnavailable(f"could not reach Suwayomi at {endpoint}: {exc}") from exc
    finally:
        if owns_client:
            client.close()
