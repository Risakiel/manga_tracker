import os

os.environ["ENABLE_SCHEDULER"] = "false"

import pytest
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
