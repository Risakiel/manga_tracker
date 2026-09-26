import httpx
import respx

from app.services.anilist_client import (
    AniListMedia,
    extract_id_from_url,
    fetch_media_by_id,
    resolve_series,
    search_candidates,
    search_media,
)

SAMPLE_MEDIA = {
    "id": 98347,
    "title": {"romaji": "Risou no Himo Seikatsu", "english": "The Ideal Sponger Life", "native": "理想のヒモ生活"},
    "synonyms": ["A Fantasy Lazy Life"],
    "status": "RELEASING",
    "chapters": None,
    "isAdult": False,
    "description": "A lazy salaryman is pulled into another world.",
    "coverImage": {"large": "https://s4.anilist.co/file/cover.jpg"},
    "staff": {"edges": [{"role": "Story", "node": {"name": {"full": "Tsunehiko Watanabe"}}}]},
}


def test_extract_id_from_url_decodes_known_url():
    assert extract_id_from_url("https://anilist.co/manga/98347/Risou-no-Himo-Seikatsu") == 98347


def test_extract_id_from_url_returns_none_for_garbage():
    assert extract_id_from_url("https://anilist.co/not-a-series") is None
    assert extract_id_from_url("") is None


@respx.mock
def test_search_media_parses_payload():
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(200, json={"data": {"Media": SAMPLE_MEDIA}}))
    media = search_media("The Ideal Sponger Life")
    assert media == AniListMedia(
        id=98347,
        title_romaji="Risou no Himo Seikatsu",
        title_english="The Ideal Sponger Life",
        title_native="理想のヒモ生活",
        synonyms=["A Fantasy Lazy Life"],
        status="RELEASING",
        chapters=None,
        is_adult=False,
        description="A lazy salaryman is pulled into another world.",
        cover_url="https://s4.anilist.co/file/cover.jpg",
        staff=[("Story", "Tsunehiko Watanabe")],
    )
    assert media.title == "The Ideal Sponger Life"
    assert media.url == "https://anilist.co/manga/98347"


@respx.mock
def test_fetch_media_by_id_parses_payload():
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(200, json={"data": {"Media": SAMPLE_MEDIA}}))
    media = fetch_media_by_id(98347)
    assert media is not None
    assert media.id == 98347


@respx.mock
def test_search_candidates_parses_payload():
    respx.post("https://graphql.anilist.co").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 98347,
                                "title": {"romaji": "Risou no Himo Seikatsu", "english": "The Ideal Sponger Life"},
                                "coverImage": {"medium": "https://img/al-thumb.jpg"},
                            },
                            {"id": 111, "title": {"romaji": "Something Else", "english": None}},
                        ]
                    }
                }
            },
        )
    )
    candidates = search_candidates("Sponger Life")
    assert len(candidates) == 2
    assert candidates[0].id == 98347
    assert candidates[0].title == "The Ideal Sponger Life"
    assert candidates[0].cover_url == "https://img/al-thumb.jpg"


@respx.mock
def test_resolve_series_uses_direct_id_when_available():
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(200, json={"data": {"Media": SAMPLE_MEDIA}}))
    media, candidates = resolve_series("https://anilist.co/manga/98347/x", "The Ideal Sponger Life")
    assert candidates == []
    assert media is not None
    assert media.id == 98347


@respx.mock
def test_resolve_series_stays_ambiguous_when_no_candidate_stands_out():
    respx.post("https://graphql.anilist.co").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {"id": 1, "title": {"romaji": "Totally Unrelated One", "english": None}},
                            {"id": 2, "title": {"romaji": "Totally Unrelated Two", "english": None}},
                        ]
                    }
                }
            },
        )
    )
    media, candidates = resolve_series("", "The Ideal Sponger Life")
    assert media is None
    assert len(candidates) == 2
