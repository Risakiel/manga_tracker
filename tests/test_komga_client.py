import httpx
import respx

from app.config import settings
from app.services import komga_client


@respx.mock
def test_list_library_ids_maps_by_name(monkeypatch):
    monkeypatch.setattr(settings, "komga_url", "http://komga.local:25600")
    respx.get("http://komga.local:25600/api/v1/libraries").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "lib-hentai", "name": "Hentai"},
                {"id": "lib-manga", "name": "Manga"},
                {"id": "lib-pornhwa", "name": "Pornhwa"},
            ],
        )
    )

    from app.models import Category

    ids = komga_client.list_library_ids()
    assert ids == {Category.manga: "lib-manga", Category.pornhwa: "lib-pornhwa"}


def test_list_library_ids_requires_configured_url(monkeypatch):
    monkeypatch.setattr(settings, "komga_url", "")
    try:
        komga_client.list_library_ids()
        assert False, "expected KomgaUnavailable"
    except komga_client.KomgaUnavailable:
        pass


SAMPLE_SERIES_NODE = {
    "id": "series-1",
    "libraryId": "lib-pornhwa",
    "name": "100% Personal",
    "booksCount": 32,
    "booksReadCount": 5,
    "metadata": {
        "summary": "A summary.",
        "summaryLock": False,
        "genres": ["romance"],
        "genresLock": False,
        "alternateTitles": [{"label": "", "title": "Alt Title"}],
        "alternateTitlesLock": False,
        "links": [{"label": "MangaUpdates", "url": "https://www.mangaupdates.com/series/0i8pz41/100-personal"}],
        "linksLock": False,
    },
}


@respx.mock
def test_fetch_series_parses_metadata_and_paginates(monkeypatch):
    monkeypatch.setattr(settings, "komga_url", "http://komga.local:25600")
    page0 = {"content": [SAMPLE_SERIES_NODE], "last": False}
    page1 = {"content": [{**SAMPLE_SERIES_NODE, "id": "series-2"}], "last": True}
    route = respx.get("http://komga.local:25600/api/v1/series")
    route.side_effect = [httpx.Response(200, json=page0), httpx.Response(200, json=page1)]

    series = komga_client.fetch_series("lib-pornhwa")

    assert [s.id for s in series] == ["series-1", "series-2"]
    first = series[0]
    assert first.name == "100% Personal"
    assert first.books_count == 32
    assert first.books_read_count == 5
    assert first.summary == "A summary."
    assert first.genres == ["romance"]
    assert first.alternate_titles == ["Alt Title"]
    assert first.mangaupdates_url == "https://www.mangaupdates.com/series/0i8pz41/100-personal"


@respx.mock
def test_update_metadata_patches_series(monkeypatch):
    monkeypatch.setattr(settings, "komga_url", "http://komga.local:25600")
    monkeypatch.setattr(settings, "komga_api_key", "test-api-key")
    route = respx.patch("http://komga.local:25600/api/v1/series/series-1/metadata").mock(
        return_value=httpx.Response(204)
    )

    komga_client.update_metadata("series-1", {"summary": "New summary"})

    assert route.called
    assert route.calls.last.request.content == b'{"summary":"New summary"}'
    # Regression: a real sync once passed an unauthenticated client here and
    # every push silently 401'd. Every write must carry the API key.
    assert route.calls.last.request.headers["X-API-Key"] == "test-api-key"


@respx.mock
def test_update_metadata_with_explicit_client_still_sends_api_key(monkeypatch):
    """The client `sync_komga_library` opens via open_client() must be the
    one actually used for the header -- this is the exact shape of the bug
    (passing some other httpx.Client here silently drops the API key)."""
    monkeypatch.setattr(settings, "komga_url", "http://komga.local:25600")
    monkeypatch.setattr(settings, "komga_api_key", "test-api-key")
    route = respx.patch("http://komga.local:25600/api/v1/series/series-1/metadata").mock(
        return_value=httpx.Response(204)
    )

    with komga_client.open_client() as client:
        komga_client.update_metadata("series-1", {"summary": "New summary"}, client=client)

    assert route.calls.last.request.headers["X-API-Key"] == "test-api-key"
