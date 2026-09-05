from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models import Category, Status


class MangaRead(BaseModel):
    id: int
    category: Category
    title_en: str
    server_folder: str
    mangaupdates_url: str
    mangaupdates_id: Optional[int]
    needs_manual_match: bool
    match_candidates: list[dict]
    status_raw: str
    status: Status
    baseline_chapter_count: Optional[float]
    mu_latest_chapter: Optional[float]
    suwayomi_manga_id: Optional[int]
    suwayomi_chapter_count: Optional[int]
    chapters_behind: Optional[float]
    author: str
    artist: str
    genres: list[str]
    alt_titles: list[str]
    cover_url: str
    description: str
    last_synced_at: Optional[datetime]
    sync_error: Optional[str]

    @classmethod
    def from_manga(cls, manga) -> "MangaRead":
        return cls(
            id=manga.id,
            category=manga.category,
            title_en=manga.title_en,
            server_folder=manga.server_folder,
            mangaupdates_url=manga.mangaupdates_url,
            mangaupdates_id=manga.mangaupdates_id,
            needs_manual_match=manga.needs_manual_match,
            match_candidates=manga.match_candidates,
            status_raw=manga.status_raw,
            status=manga.status,
            baseline_chapter_count=manga.baseline_chapter_count,
            mu_latest_chapter=manga.mu_latest_chapter,
            suwayomi_manga_id=manga.suwayomi_manga_id,
            suwayomi_chapter_count=manga.suwayomi_chapter_count,
            chapters_behind=manga.chapters_behind,
            author=manga.author,
            artist=manga.artist,
            genres=manga.genres,
            alt_titles=manga.alt_titles,
            cover_url=manga.cover_url,
            description=manga.description,
            last_synced_at=manga.last_synced_at,
            sync_error=manga.sync_error,
        )


class ManualMatchRequest(BaseModel):
    series_id: int


class ImportSummaryResponse(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: list[str]


class SyncLogRead(BaseModel):
    id: int
    manga_id: Optional[int]
    source: str
    status: str
    message: str
    created_at: datetime
