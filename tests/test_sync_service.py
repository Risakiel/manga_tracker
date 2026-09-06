import httpx
import respx
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models import Category, Manga
from app.services import komga_client, suwayomi_client
from app.services.sync_service import sync_komga_library, sync_suwayomi_library


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


def _patch_komga(monkeypatch, library_ids, series_by_library, updates=None):
    monkeypatch.setattr(komga_client, "list_library_ids", lambda client=None: library_ids)
    monkeypatch.setattr(komga_client, "fetch_series", lambda library_id, client=None: series_by_library[library_id])
    if updates is not None:
        monkeypatch.setattr(
            komga_client, "update_metadata", lambda series_id, patch, client=None: updates.append((series_id, patch))
        )


def test_komga_sync_matches_by_mangaupdates_link(monkeypatch):
    session = _session()
    session.add(
        Manga(
            category=Category.pornhwa,
            title_en="100% personal",
            server_folder="totally different folder name",
            mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/100-personal",
        )
    )
    session.commit()

    series = komga_client.KomgaSeries(
        id="series-1",
        library_id="lib-pornhwa",
        name="100% Personal",
        books_count=32,
        books_read_count=10,
        mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/100-personal",
        summary_locked=True,
        genres_locked=True,
        alternate_titles_locked=True,
        links_locked=True,
    )
    _patch_komga(monkeypatch, {Category.pornhwa: "lib-pornhwa"}, {"lib-pornhwa": [series]})

    result = sync_komga_library(session)

    assert result["matched"] == 1
    assert result["unmatched"] == 0
    manga = session.exec(select(Manga)).one()
    assert manga.komga_series_id == "series-1"
    assert manga.komga_books_count == 32
    assert manga.komga_books_read_count == 10


def test_komga_sync_matches_by_folder_name_when_no_mu_link(monkeypatch):
    session = _session()
    session.add(
        Manga(
            category=Category.manga,
            title_en="Some Title",
            server_folder="Exact Folder Name",
            mangaupdates_url="",
        )
    )
    session.commit()

    series = komga_client.KomgaSeries(
        id="series-2", library_id="lib-manga", name="Exact Folder Name", books_count=10, books_read_count=2
    )
    _patch_komga(monkeypatch, {Category.manga: "lib-manga"}, {"lib-manga": [series]})

    result = sync_komga_library(session)
    assert result["matched"] == 1
    manga = session.exec(select(Manga)).one()
    assert manga.komga_series_id == "series-2"


def test_komga_sync_reports_unmatched_without_creating_rows(monkeypatch):
    session = _session()
    series = komga_client.KomgaSeries(
        id="series-3", library_id="lib-manga", name="Totally Unrelated Series", books_count=1, books_read_count=0
    )
    _patch_komga(monkeypatch, {Category.manga: "lib-manga"}, {"lib-manga": [series]})

    result = sync_komga_library(session)
    assert result == {"matched": 0, "unmatched": 1, "pushed": 0}
    assert session.exec(select(Manga)).all() == []


def test_komga_sync_pushes_only_missing_unlocked_fields(monkeypatch):
    session = _session()
    # genres/alt_titles are properties backed by a *_json column, not real
    # SQLModel fields -- passing them as constructor kwargs is silently
    # dropped, so they must be set post-construction.
    manga = Manga(
        category=Category.manga,
        title_en="Enriched Title",
        server_folder="Enriched Title",
        mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/enriched",
        description="Our description",
    )
    manga.genres = ["Action"]
    manga.alt_titles = ["Alt One"]
    session.add(manga)
    session.commit()

    updates = []
    series = komga_client.KomgaSeries(
        id="series-4",
        library_id="lib-manga",
        name="Enriched Title",
        books_count=5,
        books_read_count=1,
        summary="",  # blank + unlocked -> should be pushed
        summary_locked=False,
        genres=["Existing Genre"],  # already has genres -> must NOT be overwritten
        genres_locked=False,
        alternate_titles=[],
        alternate_titles_locked=True,  # locked -> must NOT be pushed even though empty
        mangaupdates_url=None,
        links_locked=False,
        raw_links=[{"label": "Other", "url": "https://example.com"}],
    )
    _patch_komga(monkeypatch, {Category.manga: "lib-manga"}, {"lib-manga": [series]}, updates=updates)

    result = sync_komga_library(session)

    assert result["matched"] == 1
    assert result["pushed"] == 1
    assert len(updates) == 1
    series_id, patch = updates[0]
    assert series_id == "series-4"
    assert patch["summary"] == "Our description"
    assert "genres" not in patch
    assert "alternateTitles" not in patch
    assert patch["links"] == [
        {"label": "Other", "url": "https://example.com"},
        {"label": "MangaUpdates", "url": "https://www.mangaupdates.com/series/kljw00c/enriched"},
    ]


def test_komga_sync_pushed_alternate_titles_have_a_non_blank_label(monkeypatch):
    # Regression: Komga rejects the *entire* patch (400, including summary
    # and genres) if any alternateTitles[].label is blank -- confirmed live.
    session = _session()
    manga = Manga(
        category=Category.manga,
        title_en="Alt Titles Title",
        server_folder="Alt Titles Title",
        mangaupdates_url="",
    )
    manga.alt_titles = ["First Alt", "Second Alt"]
    session.add(manga)
    session.commit()

    updates = []
    series = komga_client.KomgaSeries(
        id="series-6",
        library_id="lib-manga",
        name="Alt Titles Title",
        books_count=1,
        books_read_count=0,
        alternate_titles=[],
        alternate_titles_locked=False,
    )
    _patch_komga(monkeypatch, {Category.manga: "lib-manga"}, {"lib-manga": [series]}, updates=updates)

    sync_komga_library(session)

    assert len(updates) == 1
    patch = updates[0][1]
    assert all(entry["label"] for entry in patch["alternateTitles"])


@respx.mock
def test_komga_sync_push_goes_through_a_real_authenticated_client(monkeypatch):
    """End-to-end regression test for a real bug: sync_komga_library once
    passed a plain, unauthenticated httpx.Client into update_metadata, so
    every push silently 401'd against the real server. This test does NOT
    monkeypatch update_metadata -- it lets the real client/request path run,
    mocked only at the HTTP layer, so a missing auth header fails loudly."""
    monkeypatch.setattr(settings, "komga_url", "http://komga.local:25600")
    monkeypatch.setattr(settings, "komga_api_key", "test-api-key")

    session = _session()
    session.add(
        Manga(
            category=Category.manga,
            title_en="Real Push Path",
            server_folder="Real Push Path",
            mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/real-push-path",
            description="Our description",
        )
    )
    session.commit()

    monkeypatch.setattr(
        komga_client,
        "list_library_ids",
        lambda client=None: {Category.manga: "lib-manga"},
    )
    monkeypatch.setattr(
        komga_client,
        "fetch_series",
        lambda library_id, client=None: [
            komga_client.KomgaSeries(
                id="series-5", library_id="lib-manga", name="Real Push Path", books_count=1, books_read_count=0
            )
        ],
    )
    patch_route = respx.patch("http://komga.local:25600/api/v1/series/series-5/metadata").mock(
        return_value=httpx.Response(204)
    )

    result = sync_komga_library(session)

    assert result["pushed"] == 1
    assert patch_route.called
    assert patch_route.calls.last.request.headers["X-API-Key"] == "test-api-key"


def test_komga_sync_reports_unavailable_server(monkeypatch):
    session = _session()
    monkeypatch.setattr(
        komga_client,
        "list_library_ids",
        lambda client=None: (_ for _ in ()).throw(komga_client.KomgaUnavailable("KOMGA_URL is not configured")),
    )

    result = sync_komga_library(session)
    assert result["error"] == "KOMGA_URL is not configured"
