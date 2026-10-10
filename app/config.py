from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")
    database_filename: str = "manga_tracker.db"

    suwayomi_url: str = ""
    suwayomi_username: str = ""
    suwayomi_password: str = ""

    komga_url: str = ""
    komga_api_key: str = ""

    # Read-only bind-mount of the NAS share holding the actual manga/pornhwa
    # folders (e.g. Unraid's /mnt/remotes/<host>_Komga), expected to contain
    # exactly two subdirectories: "Manga" and "Pornhwa".
    library_root: Path = Path("/library")

    # How often every tracked manga is re-checked against MangaUpdates,
    # falling through to MangaDex/AniList for whichever ones still have no
    # usable chapter count (see sync_all_manga_sources) -- an already
    # fully-linked manga costs nothing extra beyond its MangaUpdates re-check.
    sync_interval_hours: float = 24.0
    # Suwayomi is where manga actually get added/downloaded -- a short
    # interval here is what makes a Suwayomi addition show up in
    # MangaTracker without the user ever needing to click "sync now".
    suwayomi_sync_interval_hours: float = 0.25
    # Komga linking/metadata push. Only defaults: every interval can be
    # changed from the "Planification de tâches" page, which stores the
    # override in the database (see app/services/jobs.py).
    komga_sync_interval_hours: float = 1.0
    # Fast pass that only links Komga series no manga points to yet (a
    # series Komga just picked up after a Suwayomi download).
    komga_new_series_interval_hours: float = 0.25
    library_sync_interval_hours: float = 24.0
    enable_scheduler: bool = True
    # Timezone used to display job run times in the UI.
    display_timezone: str = "Europe/Paris"

    mangaupdates_min_request_interval_seconds: float = 1.0
    anilist_min_request_interval_seconds: float = 0.7

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_filename

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
