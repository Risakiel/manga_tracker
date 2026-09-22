import httpx
import respx

from app.services.mangadex_client import (
    extract_id_from_url,
    fetch_latest_chapter,
    fetch_manga,
    resolve_series,
    search_manga,
)

MANGA_ID = "fac7bdc7-c1f3-4595-82f5-b5bfb002b933"

SAMPLE_MANGA_PAYLOAD = {
    "data": {
        "id": MANGA_ID,
        "attributes": {
            "title": {"en": "The Ideal Sponger Life"},
            "altTitles": [{"ja": "理想のヒモ生活"}],
            "description": {"en": "A lazy salaryman is pulled into another world."},
            "tags": [
                {"attributes": {"name": {"en": "Comedy"}, "group": "genre"}},
                {"attributes": {"name": {"en": "Isekai"}, "group": "theme"}},
            ],
        },
    }
}

SAMPLE_AGGREGATE_PAYLOAD = {
    "result": "ok",
    "volumes": {
        "20": {"volume": "20", "count": 1, "chapters": {"81": {"chapter": "81", "count": 1}}},
        "19": {"volume": "19", "count": 2, "chapters": {"80": {"chapter": "80"}, "79": {"chapter": "79"}}},
        "none": {"volume": "none", "count": 1, "chapters": {"85": {"chapter": "85"}}},
    },
}


def test_extract_id_from_url_decodes_known_url():
    url = "https://mangadex.org/title/fac7bdc7-c1f3-4595-82f5-b5bfb002b933/risou-no-himo-seikatsu"
    assert extract_id_from_url(url) == MANGA_ID


def test_extract_id_from_url_returns_none_for_garbage():
    assert extract_id_from_url("https://mangadex.org/title/not-a-uuid") is None
    assert extract_id_from_url("") is None


@respx.mock
def test_fetch_latest_chapter_takes_max_across_all_volumes():
    # Real-world shape: the highest chapter number isn't necessarily in the
    # highest-numbered volume (here it's under "none", an unassigned chapter).
    respx.get(f"https://api.mangadex.org/manga/{MANGA_ID}/aggregate").mock(
        return_value=httpx.Response(200, json=SAMPLE_AGGREGATE_PAYLOAD)
    )
    assert fetch_latest_chapter(MANGA_ID) == 85.0


@respx.mock
def test_fetch_manga_parses_payload():
    respx.get(f"https://api.mangadex.org/manga/{MANGA_ID}").mock(
        return_value=httpx.Response(200, json=SAMPLE_MANGA_PAYLOAD)
    )
    respx.get(f"https://api.mangadex.org/manga/{MANGA_ID}/aggregate").mock(
        return_value=httpx.Response(200, json=SAMPLE_AGGREGATE_PAYLOAD)
    )
    manga = fetch_manga(MANGA_ID)
    assert manga.id == MANGA_ID
    assert manga.title == "The Ideal Sponger Life"
    assert manga.genres == ["Comedy"]
    assert manga.latest_chapter == 85.0
    assert manga.url == f"https://mangadex.org/title/{MANGA_ID}/the-ideal-sponger-life"


@respx.mock
def test_search_manga_requests_every_content_rating():
    # Regression: MangaDex excludes adult content from search results
    # unless every rating is explicitly requested -- most of this library
    # is adult content, so an unfiltered search would find almost nothing.
    route = respx.get("https://api.mangadex.org/manga").mock(
        return_value=httpx.Response(200, json={"data": [{"id": MANGA_ID, "attributes": SAMPLE_MANGA_PAYLOAD["data"]["attributes"]}]})
    )
    candidates = search_manga("Ideal Sponger Life")
    assert len(candidates) == 1
    assert candidates[0].id == MANGA_ID
    requested_ratings = route.calls.last.request.url.params.get_list("contentRating[]")
    assert set(requested_ratings) == {"safe", "suggestive", "erotica", "pornographic"}


@respx.mock
def test_resolve_series_uses_direct_id_when_available():
    respx.get(f"https://api.mangadex.org/manga/{MANGA_ID}").mock(
        return_value=httpx.Response(200, json=SAMPLE_MANGA_PAYLOAD)
    )
    respx.get(f"https://api.mangadex.org/manga/{MANGA_ID}/aggregate").mock(
        return_value=httpx.Response(200, json=SAMPLE_AGGREGATE_PAYLOAD)
    )
    manga, candidates = resolve_series(
        f"https://mangadex.org/title/{MANGA_ID}/risou-no-himo-seikatsu", "The Ideal Sponger Life"
    )
    assert candidates == []
    assert manga is not None
    assert manga.id == MANGA_ID


@respx.mock
def test_resolve_series_stays_ambiguous_when_no_candidate_stands_out():
    respx.get("https://api.mangadex.org/manga").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "id-1", "attributes": {"title": {"en": "Totally Unrelated One"}}},
                    {"id": "id-2", "attributes": {"title": {"en": "Totally Unrelated Two"}}},
                ]
            },
        )
    )
    manga, candidates = resolve_series("", "The Ideal Sponger Life")
    assert manga is None
    assert len(candidates) == 2
