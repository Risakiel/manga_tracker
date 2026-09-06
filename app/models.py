import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Column, Field, SQLModel, Text


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Category(str, Enum):
    manga = "manga"
    pornhwa = "pornhwa"


class Status(str, Enum):
    ongoing = "ongoing"
    complete = "complete"
    hiatus = "hiatus"
    cancelled = "cancelled"
    dropped = "dropped"
    unknown = "unknown"


class SyncSource(str, Enum):
    mangaupdates = "mangaupdates"
    anilist = "anilist"
    suwayomi = "suwayomi"
    komga = "komga"
    excel = "excel"


class SyncStatus(str, Enum):
    success = "success"
    error = "error"


class Manga(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    category: Category = Field(default=Category.manga, index=True)
    title_en: str = Field(index=True)
    server_folder: str = Field(index=True)

    mangaupdates_url: str = ""
    mangaupdates_id: Optional[int] = Field(default=None, index=True)
    needs_manual_match: bool = Field(default=False, index=True)
    match_candidates_json: Optional[str] = Field(default=None, sa_column=Column(Text))

    status_raw: str = ""
    status: Status = Field(default=Status.unknown, index=True)

    baseline_chapter_count: Optional[float] = None
    mu_latest_chapter: Optional[float] = None

    suwayomi_manga_id: Optional[int] = Field(default=None, index=True)
    suwayomi_chapter_count: Optional[int] = None
    suwayomi_categories_json: str = Field(default="[]", sa_column=Column(Text))

    komga_series_id: Optional[str] = Field(default=None, index=True)
    komga_books_count: Optional[int] = None
    komga_books_read_count: Optional[int] = None

    author: str = ""
    artist: str = ""
    genres_json: str = Field(default="[]", sa_column=Column(Text))
    alt_titles_json: str = Field(default="[]", sa_column=Column(Text))
    cover_url: str = ""
    description: str = Field(default="", sa_column=Column(Text))

    last_synced_at: Optional[datetime] = None
    sync_error: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def genres(self) -> list[str]:
        return json.loads(self.genres_json or "[]")

    @genres.setter
    def genres(self, value: list[str]) -> None:
        self.genres_json = json.dumps(value)

    @property
    def suwayomi_categories(self) -> list[str]:
        return json.loads(self.suwayomi_categories_json or "[]")

    @suwayomi_categories.setter
    def suwayomi_categories(self, value: list[str]) -> None:
        self.suwayomi_categories_json = json.dumps(value)

    @property
    def alt_titles(self) -> list[str]:
        return json.loads(self.alt_titles_json or "[]")

    @alt_titles.setter
    def alt_titles(self, value: list[str]) -> None:
        self.alt_titles_json = json.dumps(value)

    @property
    def match_candidates(self) -> list[dict]:
        return json.loads(self.match_candidates_json or "[]")

    @match_candidates.setter
    def match_candidates(self, value: list[dict]) -> None:
        self.match_candidates_json = json.dumps(value)

    @property
    def chapters_behind(self) -> Optional[float]:
        if self.suwayomi_chapter_count is None or self.mu_latest_chapter is None:
            return None
        return max(self.mu_latest_chapter - self.suwayomi_chapter_count, 0)

    @property
    def komga_unread_count(self) -> Optional[int]:
        if self.komga_books_count is None or self.komga_books_read_count is None:
            return None
        return max(self.komga_books_count - self.komga_books_read_count, 0)


class SyncLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    manga_id: Optional[int] = Field(default=None, foreign_key="manga.id", index=True)
    source: SyncSource
    status: SyncStatus
    message: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, index=True)
