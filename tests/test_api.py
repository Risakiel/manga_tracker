import os

os.environ["ENABLE_SCHEDULER"] = "false"

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_session
from app.main import app
from app.models import Category, Manga, Status

# StaticPool is required so every connection (the test thread and the
# request's worker thread) shares the same in-memory SQLite database.
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _override_get_session():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _override_get_session


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Aucun manga" in resp.text


def test_dashboard_lists_manga(client):
    with Session(engine) as session:
        session.add(
            Manga(
                category=Category.manga,
                title_en="Test Manga",
                server_folder="Test Manga",
                mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/test",
                status=Status.ongoing,
            )
        )
        session.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Test Manga" in resp.text


def test_manga_detail_404(client):
    resp = client.get("/manga/999999")
    assert resp.status_code == 404


def test_dashboard_behind_filter_shows_only_behind_manga(client):
    with Session(engine) as session:
        session.add_all(
            [
                Manga(
                    category=Category.manga,
                    title_en="Behind Manga",
                    server_folder="Behind Manga",
                    mangaupdates_url="https://www.mangaupdates.com/series/0000001/behind",
                    status=Status.ongoing,
                    suwayomi_chapter_count=5,
                    mu_latest_chapter=10,
                ),
                Manga(
                    category=Category.manga,
                    title_en="Up To Date Manga",
                    server_folder="Up To Date Manga",
                    mangaupdates_url="https://www.mangaupdates.com/series/0000002/up-to-date",
                    status=Status.ongoing,
                    suwayomi_chapter_count=10,
                    mu_latest_chapter=10,
                ),
            ]
        )
        session.commit()

    resp = client.get("/?behind=true")
    assert resp.status_code == 200
    assert "Behind Manga" in resp.text
    assert "Up To Date Manga" not in resp.text


@respx.mock
def test_editing_mangaupdates_url_resyncs_off_the_new_link(client):
    with Session(engine) as session:
        manga = Manga(
            category=Category.manga,
            title_en="Wrong Match",
            server_folder="Wrong Match",
            mangaupdates_url="https://www.mangaupdates.com/series/0000000/wrong-series",
            status=Status.unknown,
        )
        session.add(manga)
        session.commit()
        manga_id = manga.id

    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(
            200,
            json={
                "series_id": 44838842124,
                "title": "The Correct Series",
                "url": "https://www.mangaupdates.com/series/kljw00c/the-correct-series",
                "status": "50 Chapters (Complete)",
                "completed": True,
                "latest_chapter": 50,
                "authors": [],
                "genres": [],
                "associated": [],
            },
        )
    )

    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(200, json={"data": {"Media": None}}))

    resp = client.post(
        f"/manga/{manga_id}/mangaupdates-url",
        data={"mangaupdates_url": "https://www.mangaupdates.com/series/kljw00c/the-correct-series"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with Session(engine) as session:
        manga = session.get(Manga, manga_id)
        assert manga.mangaupdates_url == "https://www.mangaupdates.com/series/kljw00c/the-correct-series"
        assert manga.mangaupdates_id == 44838842124
        assert manga.mu_latest_chapter == 50
        assert manga.status == Status.complete


@respx.mock
def test_identify_search_route_returns_candidates_from_all_sources(client):
    with Session(engine) as session:
        manga = Manga(category=Category.manga, title_en="Search Target", server_folder="Search Target")
        session.add(manga)
        session.commit()
        manga_id = manga.id

    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "record": {
                            "series_id": 1,
                            "title": "MU Found",
                            "url": "https://www.mangaupdates.com/series/1/mu-found",
                        }
                    }
                ]
            },
        )
    )
    respx.get("https://api.mangadex.org/manga").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post("https://graphql.anilist.co").mock(
        return_value=httpx.Response(200, json={"data": {"Page": {"media": []}}})
    )

    resp = client.post(f"/manga/{manga_id}/identify/search", data={"title": "Search Target"})

    assert resp.status_code == 200
    assert "MU Found" in resp.text
    assert "MangaUpdates" in resp.text


@respx.mock
def test_identify_confirm_route_applies_pick_and_shows_recap(client):
    with Session(engine) as session:
        manga = Manga(category=Category.manga, title_en="Confirm Target", server_folder="Confirm Target")
        session.add(manga)
        session.commit()
        manga_id = manga.id

    respx.get("https://api.mangaupdates.com/v1/series/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "series_id": 1,
                "title": "Confirmed Identity",
                "url": "https://www.mangaupdates.com/series/1/confirmed-identity",
            },
        )
    )

    resp = client.post(f"/manga/{manga_id}/identify/confirm", data={"candidate": "mangaupdates:1"})

    assert resp.status_code == 200
    assert "Confirmed Identity" in resp.text
    assert "pas encore lié" in resp.text

    with Session(engine) as session:
        manga = session.get(Manga, manga_id)
        assert manga.mangaupdates_id == 1


def test_identify_confirm_route_rejects_unknown_source(client):
    with Session(engine) as session:
        manga = Manga(category=Category.manga, title_en="Bad Candidate", server_folder="Bad Candidate")
        session.add(manga)
        session.commit()
        manga_id = manga.id

    resp = client.post(f"/manga/{manga_id}/identify/confirm", data={"candidate": "unknown:1"})

    assert resp.status_code == 400


def _fake_komga(monkeypatch, updates):
    from app.config import settings
    from app.services import komga_client

    monkeypatch.setattr(settings, "komga_url", "http://komga.local")

    def _fetch(series_id, client=None):
        if series_id != "0RHPX6TK8B7H9":
            raise komga_client.KomgaUnavailable("404 Not Found")
        return komga_client.KomgaSeries(
            id=series_id,
            library_id="lib-manga",
            name="A Fantasy Lazy Life",
            url="/data/Manga/A Fantasy Lazy Life",
            books_count=17,
            mangaupdates_url="https://www.mangaupdates.com/series/aaaaaaa/old",
            raw_links=[{"label": "MangaUpdates", "url": "https://www.mangaupdates.com/series/aaaaaaa/old"}],
        )

    monkeypatch.setattr(komga_client, "fetch_series_by_id", _fetch)
    monkeypatch.setattr(komga_client, "fetch_library_name", lambda library_id, client=None: "Manga")
    monkeypatch.setattr(
        komga_client, "update_metadata", lambda series_id, patch, client=None: updates.append((series_id, patch))
    )


def test_pinning_a_komga_series_from_the_detail_page(client, monkeypatch):
    updates = []
    _fake_komga(monkeypatch, updates)
    with Session(engine) as session:
        manga = Manga(
            category=Category.manga,
            title_en="A Fantasy Lazy Life",
            server_folder="A Fantasy Lazy Life",
            mangaupdates_url="https://www.mangaupdates.com/series/kljw00c/a-fantasy-lazy-life",
        )
        session.add(manga)
        session.commit()
        manga_id = manga.id

    resp = client.post(
        f"/manga/{manga_id}/komga-series",
        data={"komga_series": "http://192.168.1.90:25600/series/0RHPX6TK8B7H9"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with Session(engine) as session:
        manga = session.get(Manga, manga_id)
        assert manga.komga_series_id == "0RHPX6TK8B7H9"
        assert manga.komga_id_pinned is True
        assert manga.komga_match_method == "manuel"
        assert manga.komga_folder_path == "/data/Manga/A Fantasy Lazy Life"
        # The stale MangaUpdates link on the Komga side got replaced by ours.
        assert manga.komga_mangaupdates_url == "https://www.mangaupdates.com/series/kljw00c/a-fantasy-lazy-life"
    assert updates[-1][1]["links"] == [
        {"label": "MangaUpdates", "url": "https://www.mangaupdates.com/series/kljw00c/a-fantasy-lazy-life"}
    ]

    page = client.get(f"/manga/{manga_id}")
    assert 'id="komga"' in page.text
    assert "0RHPX6TK8B7H9" in page.text
    assert "http://komga.local/series/0RHPX6TK8B7H9" in page.text
    assert "/data/Manga/A Fantasy Lazy Life" in page.text
    assert "identique des deux côtés" in page.text


def test_pinning_an_unknown_komga_series_keeps_the_current_link(client, monkeypatch):
    _fake_komga(monkeypatch, [])
    with Session(engine) as session:
        manga = Manga(category=Category.manga, title_en="Keep Link", server_folder="Keep Link", komga_series_id="GOODID1234")
        session.add(manga)
        session.commit()
        manga_id = manga.id

    client.post(f"/manga/{manga_id}/komga-series", data={"komga_series": "BADID12345"}, follow_redirects=False)

    with Session(engine) as session:
        manga = session.get(Manga, manga_id)
        assert manga.komga_series_id == "GOODID1234"
        assert "BADID12345 introuvable" in manga.komga_sync_error


def test_unlinking_a_komga_series(client, monkeypatch):
    _fake_komga(monkeypatch, [])
    with Session(engine) as session:
        manga = Manga(
            category=Category.manga, title_en="Unlink", server_folder="Unlink", komga_series_id="0RHPX6TK8B7H9",
            komga_id_pinned=True,
        )
        session.add(manga)
        session.commit()
        manga_id = manga.id

    client.post(f"/manga/{manga_id}/komga-series", data={"komga_series": ""}, follow_redirects=False)

    with Session(engine) as session:
        manga = session.get(Manga, manga_id)
        assert manga.komga_series_id is None
        assert manga.komga_id_pinned is None


@respx.mock
def test_editing_mangaupdates_url_pushes_it_to_komga_right_away(client, monkeypatch):
    updates = []
    _fake_komga(monkeypatch, updates)
    with Session(engine) as session:
        manga = Manga(
            category=Category.manga,
            title_en="Push Link",
            server_folder="Push Link",
            mangaupdates_url="https://www.mangaupdates.com/series/aaaaaaa/old",
            komga_series_id="0RHPX6TK8B7H9",
        )
        session.add(manga)
        session.commit()
        manga_id = manga.id

    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(
            200,
            json={
                "series_id": 44838842124,
                "title": "Push Link",
                "url": "https://www.mangaupdates.com/series/kljw00c/push-link",
                "status": "10 Chapters (Ongoing)",
                "completed": False,
                "latest_chapter": 10,
                "authors": [],
                "genres": [],
                "associated": [],
            },
        )
    )
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(200, json={"data": {"Media": None}}))

    client.post(
        f"/manga/{manga_id}/mangaupdates-url",
        data={"mangaupdates_url": "https://www.mangaupdates.com/series/kljw00c/push-link"},
        follow_redirects=False,
    )

    assert updates and updates[-1][1]["links"] == [
        {"label": "MangaUpdates", "url": "https://www.mangaupdates.com/series/kljw00c/push-link"}
    ]
