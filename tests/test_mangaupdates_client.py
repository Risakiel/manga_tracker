import httpx
import respx

from app.services.mangaupdates_client import (
    extract_series_id,
    fetch_series,
    resolve_series,
    same_series_url,
    search_series,
)

# Real URLs from the user's Excel export, decoded and verified against the
# live API during planning -- kept here as regression fixtures.
KNOWN_SLUGS = {
    "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn": 44838842124,
    "https://www.mangaupdates.com/series/e401aci/a-flame-reborn": 30716877474,
    "https://www.mangaupdates.com/series/0rpf7qa/a-returner-s-magic-should-be-special": 1675287010,
}


def test_extract_series_id_decodes_known_urls():
    for url, expected_id in KNOWN_SLUGS.items():
        assert extract_series_id(url) == expected_id


def test_extract_series_id_returns_none_for_garbage():
    assert extract_series_id("https://www.mangaupdates.com/not-a-series-url") is None
    assert extract_series_id("") is None


SAMPLE_SERIES_PAYLOAD = {
    "series_id": 44838842124,
    "title": "8th Circle Mage Reborn",
    "url": "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn",
    "associated": [{"title": "The Rebirth of an 8th-Circled Wizard"}],
    "description": "A powerful mage is reborn.",
    "image": {"url": {"original": "https://cdn.mangaupdates.com/image/i437210.jpg", "thumb": "https://cdn.mangaupdates.com/image/thumb/i437210.jpg"}},
    "type": "Manhwa",
    "genres": [{"genre": "Action"}, {"genre": "Fantasy"}],
    "latest_chapter": 160,
    "status": "160 Chapters (Complete)",
    "completed": True,
    "authors": [
        {"name": "Inus studio", "type": "Author"},
        {"name": "chleo", "type": "Artist"},
    ],
}


@respx.mock
def test_fetch_series_parses_payload():
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=SAMPLE_SERIES_PAYLOAD)
    )
    series = fetch_series(44838842124)
    assert series.title == "8th Circle Mage Reborn"
    assert series.latest_chapter == 160
    assert series.completed is True
    assert series.authors == ["Inus studio"]
    assert series.artists == ["chleo"]
    assert series.genres == ["Action", "Fantasy"]
    assert series.cover_url.endswith("i437210.jpg")
    # Regression: the detail page displays this large -- must prefer the
    # full-resolution "original" over the small "thumb" variant.
    assert series.cover_url == "https://cdn.mangaupdates.com/image/i437210.jpg"


@respx.mock
def test_fetch_series_falls_back_to_thumb_when_no_original():
    payload = dict(SAMPLE_SERIES_PAYLOAD, image={"url": {"thumb": "https://cdn.mangaupdates.com/image/thumb/i1.jpg"}})
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=payload)
    )
    series = fetch_series(44838842124)
    assert series.cover_url == "https://cdn.mangaupdates.com/image/thumb/i1.jpg"


@respx.mock
def test_resolve_series_uses_direct_id_when_available():
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=SAMPLE_SERIES_PAYLOAD)
    )
    series, candidates = resolve_series(
        "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn", "8th Circle Mage Reborn"
    )
    assert series is not None
    assert candidates == []
    assert series.series_id == 44838842124


@respx.mock
def test_resolve_series_falls_back_to_search_when_url_404():
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(return_value=httpx.Response(404))
    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"record": {"series_id": 44838842124, "title": "8th Circle Mage Reborn", "url": "u"}},
                    {"record": {"series_id": 999, "title": "8th Circle Mage Reborn Season 2", "url": "u2"}},
                ]
            },
        )
    )
    series, candidates = resolve_series(
        "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn", "8th Circle Mage Reborn"
    )
    # Two candidates with close-enough scores (a real sequel/remake could
    # legitimately be the right one) -> no auto-resolution, manual review required.
    assert series is None
    assert len(candidates) == 2


@respx.mock
def test_resolve_series_auto_resolves_a_clear_winner_among_several_candidates():
    # Real-world case that used to flood the sync log with "manual match
    # required": MangaUpdates' search almost never returns exactly one
    # result even for a clean, unambiguous title. One candidate obviously
    # matching and the rest scoring nowhere close must not force a manual
    # review.
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=SAMPLE_SERIES_PAYLOAD)
    )
    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"record": {"series_id": 44838842124, "title": "8th Circle Mage Reborn", "url": "u"}},
                    {"record": {"series_id": 999, "title": "Something Completely Unrelated", "url": "u2"}},
                    {"record": {"series_id": 998, "title": "Yet Another Unrelated Title", "url": "u3"}},
                ]
            },
        )
    )
    series, candidates = resolve_series("", "8th Circle Mage Reborn")
    assert series is not None
    assert candidates == []
    assert series.series_id == 44838842124


@respx.mock
def test_fetch_series_falls_back_to_status_text_when_structured_fields_unset():
    # Real-world case (adult-tagged series): MangaUpdates leaves latest_chapter=0
    # and completed=False even though the free-text status says otherwise.
    payload = dict(SAMPLE_SERIES_PAYLOAD)
    payload["status"] = "79 Chapters (Complete)"
    payload["latest_chapter"] = 0
    payload["completed"] = False
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=payload)
    )
    series = fetch_series(44838842124)
    assert series.latest_chapter == 79
    assert series.completed is True


@respx.mock
def test_fetch_series_takes_max_chapter_count_across_multiple_editions():
    # Real-world case: status text lists several editions, doesn't start with
    # a number, and the structured fields are still unset.
    payload = dict(SAMPLE_SERIES_PAYLOAD)
    payload["status"] = "Original Comic: 30 Chapters (Complete)  \nTatekomi: 91 Chapters (Complete)"
    payload["latest_chapter"] = 0
    payload["completed"] = False
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=payload)
    )
    series = fetch_series(44838842124)
    assert series.latest_chapter == 91
    assert series.completed is True


@respx.mock
def test_search_series_parses_results():
    respx.post("https://api.mangaupdates.com/v1/series/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "record": {
                            "series_id": 1,
                            "title": "Foo",
                            "url": "u",
                            "image": {"url": {"thumb": "https://img/foo-thumb.jpg"}},
                        },
                        "hit_title": "Foo",
                    }
                ]
            },
        )
    )
    candidates = search_series("Foo")
    assert len(candidates) == 1
    assert candidates[0].title == "Foo"
    assert candidates[0].cover_url == "https://img/foo-thumb.jpg"


def test_extract_series_id_without_trailing_slug():
    assert extract_series_id("https://www.mangaupdates.com/series/kljw00c") == int("kljw00c", 36)
    assert extract_series_id("https://www.mangaupdates.com/series.html?id=123") is None


def test_same_series_url_ignores_slug_differences():
    assert same_series_url(
        "https://www.mangaupdates.com/series/kljw00c/some-slug", "https://www.mangaupdates.com/series/kljw00c"
    )
    assert not same_series_url(
        "https://www.mangaupdates.com/series/kljw00c/a", "https://www.mangaupdates.com/series/aaaaaaa/a"
    )
    assert not same_series_url("https://www.mangaupdates.com/series/kljw00c/a", None)


@respx.mock
def test_fetch_series_parses_the_volume_count_from_the_status_text():
    # Real case: "A Fantasy Lazy Life" (Risou no Himo Seikatsu).
    payload = dict(SAMPLE_SERIES_PAYLOAD)
    payload["status"] = "26 Volumes (Ongoing)"
    payload["latest_chapter"] = 85
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=payload)
    )
    series = fetch_series(44838842124)
    assert series.latest_volume == 26
    assert series.latest_chapter == 85


@respx.mock
def test_fetch_series_has_no_volume_count_when_the_status_gives_chapters_only():
    respx.get("https://api.mangaupdates.com/v1/series/44838842124").mock(
        return_value=httpx.Response(200, json=SAMPLE_SERIES_PAYLOAD)
    )
    assert fetch_series(44838842124).latest_volume is None


@respx.mock
def test_fetch_latest_release_volume_takes_the_highest_volume():
    from app.services.mangaupdates_client import fetch_latest_release_volume

    route = respx.post("https://api.mangaupdates.com/v1/releases/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"record": {"volume": "20", "chapter": "81"}},
                    {"record": {"volume": "", "chapter": "85"}},
                    {"record": {"volume": "18-19", "chapter": "80"}},
                ]
            },
        )
    )
    assert fetch_latest_release_volume(44838842124) == 20
    assert route.calls.last.request.content and b'"search_type":"series"' in route.calls.last.request.content.replace(b" ", b"")
