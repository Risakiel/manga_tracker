import httpx
import respx
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models import Category, Manga
from app.services import suwayomi_client
from app.services.sync_service import sync_suwayomi_library


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


@respx.mock
def test_suwayomi_sync_updates_matched_manga_categories_and_chapter_count(monkeypatch):
    session = _session()
    session.add(
        Manga(
            category=Category.manga,
            title_en="Existing Manga",
            server_folder="Existing Manga",
            mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/existing-manga",
        )
    )
    session.commit()

    monkeypatch.setattr(
        suwayomi_client,
        "fetch_library",
        lambda client=None: [
            suwayomi_client.SuwayomiManga(
                id=1,
                title="Existing Manga",
                download_count=42,
                categories=["À suivre"],
            )
        ],
    )

    result = sync_suwayomi_library(session)

    assert result == {"matched": 1, "created": 0}
    manga = session.exec(select(Manga)).one()
    assert manga.suwayomi_manga_id == 1
    assert manga.suwayomi_chapter_count == 42
    assert manga.suwayomi_categories == ["À suivre"]


@respx.mock
def test_suwayomi_sync_auto_creates_unmatched_entries_with_type_guessed_from_genre(monkeypatch):
    session = _session()

    monkeypatch.setattr(
        suwayomi_client,
        "fetch_library",
        lambda client=None: [
            suwayomi_client.SuwayomiManga(
                id=10,
                title="Brand New Adult Title",
                genres=["Romance", "Hentai"],
                download_count=5,
                categories=["Terminé"],
                thumbnail_url="http://suwayomi.local/thumb/10",
            ),
            suwayomi_client.SuwayomiManga(
                id=11,
                title="Brand New Regular Title",
                genres=["Action", "Shonen"],
                download_count=3,
                categories=[],
            ),
        ],
    )
    # Neither title matches anything on MangaUpdates -- new manga should end
    # up with an empty link and flagged for manual review, not crash.
    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    result = sync_suwayomi_library(session)

    assert result == {"matched": 0, "created": 2}
    mangas = {m.title_en: m for m in session.exec(select(Manga)).all()}

    adult = mangas["Brand New Adult Title"]
    assert adult.category == Category.pornhwa
    assert adult.suwayomi_manga_id == 10
    assert adult.suwayomi_chapter_count == 5
    assert adult.suwayomi_categories == ["Terminé"]
    assert adult.cover_url == "http://suwayomi.local/thumb/10"
    assert adult.mangaupdates_url == ""
    assert adult.needs_manual_match is True

    regular = mangas["Brand New Regular Title"]
    assert regular.category == Category.manga


@respx.mock
def test_suwayomi_sync_handles_duplicate_titles_as_separate_entries(monkeypatch):
    # Real-world case: the same title added to Suwayomi twice under different
    # source IDs. Both should end up tracked, not collide onto one row.
    session = _session()
    session.add(
        Manga(
            category=Category.manga,
            title_en="Duplicated Title",
            server_folder="Duplicated Title",
            mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/duplicated-title",
        )
    )
    session.commit()

    monkeypatch.setattr(
        suwayomi_client,
        "fetch_library",
        lambda client=None: [
            suwayomi_client.SuwayomiManga(id=627, title="Duplicated Title", download_count=10),
            suwayomi_client.SuwayomiManga(id=628, title="Duplicated Title", download_count=20),
        ],
    )
    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    result = sync_suwayomi_library(session)

    assert result == {"matched": 1, "created": 1}
    rows = session.exec(select(Manga).where(Manga.title_en == "Duplicated Title")).all()
    assert len(rows) == 2
    suwayomi_ids = {m.suwayomi_manga_id for m in rows}
    assert suwayomi_ids == {627, 628}


@respx.mock
def test_suwayomi_sync_uses_real_folder_name_when_library_mounted(tmp_path, monkeypatch):
    (tmp_path / "Manga" / "DICE_ The Cube that Changes Everything").mkdir(parents=True)
    monkeypatch.setattr(settings, "library_root", tmp_path)

    session = _session()
    monkeypatch.setattr(
        suwayomi_client,
        "fetch_library",
        lambda client=None: [
            suwayomi_client.SuwayomiManga(id=1, title="DICE: The Cube that Changes Everything", download_count=1)
        ],
    )
    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    sync_suwayomi_library(session)

    manga = session.exec(select(Manga)).one()
    assert manga.server_folder == "DICE_ The Cube that Changes Everything"


def test_suwayomi_sync_reports_unavailable_server(monkeypatch):
    session = _session()

    def _raise(client=None):
        raise suwayomi_client.SuwayomiUnavailable("SUWAYOMI_URL is not configured")

    monkeypatch.setattr(suwayomi_client, "fetch_library", _raise)

    result = sync_suwayomi_library(session)
    assert result["error"] == "SUWAYOMI_URL is not configured"
