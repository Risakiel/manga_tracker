from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")
    database_filename: str = "manga_tracker.db"

    suwayomi_url: str = ""
    suwayomi_username: str = ""
    suwayomi_password: str = ""

    sync_interval_hours: float = 24.0
    suwayomi_sync_interval_hours: float = 6.0
    enable_scheduler: bool = True

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
