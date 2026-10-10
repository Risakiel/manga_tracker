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
    mangadex = "mangadex"
    suwayomi = "suwayomi"
    komga = "komga"
    library = "library"
    excel = "excel"
    # One row per run of a scheduled/manual job (see app/services/jobs.py).
    job = "job"


class SyncStatus(str, Enum):
    success = "success"
    error = "error"
    running = "running"


class ChapterSource(str, Enum):
    """Which source's chapter count counts as "the" latest chapter for the
    dashboard's "en retard" figure. AniList is deliberately not an option
    here -- its chapter count is only meaningful for completed series, so
    it's shown/linked like the other two sources but never authoritative."""

    mangaupdates = "mangaupdates"
    mangadex = "mangadex"


class Manga(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    category: Category = Field(default=Category.manga, index=True)
    title_en: str = Field(index=True)
    server_folder: str = Field(index=True)

    mangaupdates_url: str = ""
    mangaupdates_id: Optional[int] = Field(default=None, index=True)
    needs_manual_match: bool = Field(default=False, index=True)
    match_candidates_json: Optional[str] = Field(default=None, sa_column=Column(Text))

    # AniList and MangaDex links, resolved/refreshed the same way as the
    # MangaUpdates ones above (see sync_service.py) -- kept as their own
    # nullable columns (not required, unlike the original MangaUpdates
    # fields) since they're an optional add-on for existing rows.
    anilist_url: Optional[str] = None
    anilist_id: Optional[int] = Field(default=None, index=True)
    anilist_latest_chapter: Optional[float] = None
    anilist_needs_manual_match: Optional[bool] = Field(default=None, index=True)
    anilist_match_candidates_json: Optional[str] = Field(default=None, sa_column=Column(Text))

    mangadex_url: Optional[str] = None
    mangadex_id: Optional[str] = Field(default=None, index=True)
    mangadex_latest_chapter: Optional[float] = None
    mangadex_needs_manual_match: Optional[bool] = Field(default=None, index=True)
    mangadex_match_candidates_json: Optional[str] = Field(default=None, sa_column=Column(Text))

    # Which source's chapter count drives `chapters_behind` below. None
    # means "use the default order" (MangaUpdates, falling back to
    # MangaDex) -- see `authoritative_latest_chapter`.
    preferred_chapter_source: Optional[ChapterSource] = Field(default=None, index=True)

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
    # What Komga itself says about the linked series, refreshed on every
    # Komga sync -- shown in the detail page's "Komga" box so a wrong or
    # stale link is visible at a glance. All nullable: added after the
    # fact, existing rows just show "?" until their next Komga sync.
    komga_series_name: Optional[str] = None
    komga_library_name: Optional[str] = None
    komga_folder_path: Optional[str] = None
    komga_mangaupdates_url: Optional[str] = None
    komga_links_locked: Optional[bool] = None
    # How the series was linked ("id", "manuel", "mangaupdates", "dossier",
    # "titre") -- and whether the user pinned it by hand, in which case the
    # automatic matching never re-links this manga to another series.
    komga_match_method: Optional[str] = None
    komga_id_pinned: Optional[bool] = None
    komga_synced_at: Optional[datetime] = None
    komga_sync_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    # The cover_url last sent to Komga as the series poster (or deliberately
    # not sent because Komga already had a poster chosen by hand) -- lets the
    # routine sync skip the poster check once it's been handled, and re-run
    # it when our cover changes.
    komga_cover_pushed_url: Optional[str] = None
    komga_cover_kept: Optional[bool] = None

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
    def anilist_match_candidates(self) -> list[dict]:
        return json.loads(self.anilist_match_candidates_json or "[]")

    @anilist_match_candidates.setter
    def anilist_match_candidates(self, value: list[dict]) -> None:
        self.anilist_match_candidates_json = json.dumps(value)

    @property
    def mangadex_match_candidates(self) -> list[dict]:
        return json.loads(self.mangadex_match_candidates_json or "[]")

    @mangadex_match_candidates.setter
    def mangadex_match_candidates(self, value: list[dict]) -> None:
        self.mangadex_match_candidates_json = json.dumps(value)

    @property
    def authoritative_latest_chapter(self) -> Optional[float]:
        """The chapter count `chapters_behind` is computed from: the
        preferred source (MangaUpdates by default) if it has one, else
        whichever of MangaUpdates/MangaDex does -- AniList is excluded, see
        ChapterSource."""
        source = self.preferred_chapter_source or ChapterSource.mangaupdates
        preferred = self.mangadex_latest_chapter if source == ChapterSource.mangadex else self.mu_latest_chapter
        if preferred is not None:
            return preferred
        return self.mu_latest_chapter if self.mu_latest_chapter is not None else self.mangadex_latest_chapter

    @property
    def chapters_behind(self) -> Optional[float]:
        latest = self.authoritative_latest_chapter
        if self.suwayomi_chapter_count is None or latest is None:
            return None
        return max(latest - self.suwayomi_chapter_count, 0)

    @property
    def komga_mangaupdates_status(self) -> str:
        """Compares the MangaUpdates link Komga holds against ours:
        "same", "different", "missing_komga", "missing_ours" or "unknown"
        (never synced with Komga yet)."""
        if not self.komga_series_id or self.komga_synced_at is None:
            return "unknown"
        if not self.mangaupdates_url:
            return "missing_ours"
        if not self.komga_mangaupdates_url:
            return "missing_komga"
        from app.services.mangaupdates_client import same_series_url

        return "same" if same_series_url(self.mangaupdates_url, self.komga_mangaupdates_url) else "different"

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
    # Only set on SyncSource.job rows: which job ran, who triggered it
    # ("planifié" / "manuel") and when it ended (created_at = start).
    job_id: Optional[str] = Field(default=None, index=True)
    trigger: Optional[str] = None
    finished_at: Optional[datetime] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        start, end = self.created_at, self.finished_at
        if (start.tzinfo is None) != (end.tzinfo is None):
            start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        return max((end - start).total_seconds(), 0.0)


class ScheduledJob(SQLModel, table=True):
    """Per-job scheduling settings and last-run bookkeeping for the
    "Planification de tâches" page. One row per job id from
    app/services/jobs.py, created lazily the first time the job runs or its
    interval is changed. interval_minutes overrides the env-var default
    (NULL = use the default, 0 = disabled)."""

    job_id: str = Field(primary_key=True)
    interval_minutes: Optional[int] = None
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_status: str = ""
    last_message: str = Field(default="", sa_column=Column(Text))
