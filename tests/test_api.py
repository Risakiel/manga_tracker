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
