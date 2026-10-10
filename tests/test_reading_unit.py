from app.models import Manga, ReadingUnit
from app.services.reading_unit import classify_title, detect_unit


def test_classify_title_recognises_volume_and_chapter_names():
    assert classify_title("Volume 4") == ReadingUnit.volumes
    assert classify_title("A Fantasy Lazy Life T04") == ReadingUnit.volumes
    assert classify_title("Vol.12") == ReadingUnit.volumes
    assert classify_title("Tome 3") == ReadingUnit.volumes
    assert classify_title("Ch.5") == ReadingUnit.chapters
    assert classify_title("Chapitre 2") == ReadingUnit.chapters
    assert classify_title("Chapter 160.5") == ReadingUnit.chapters
    # A chapter filed under its volume is still a chapter.
    assert classify_title("Vol.1 Ch.3") == ReadingUnit.chapters
    assert classify_title("Prologue") is None
    assert classify_title("Volcano 3") is None


def test_detect_unit_follows_the_majority_of_books():
    # Real case: "A Fantasy Lazy Life" has three early "Chapitre" files,
    # then whole volumes.
    titles = ["Chapitre 1", "Chapitre 2", "Chapitre 3"] + [f"Volume {n}" for n in range(4, 18)]
    assert detect_unit(titles) == ReadingUnit.volumes
    assert detect_unit([f"Ch.{n}" for n in range(1, 161)]) == ReadingUnit.chapters
    assert detect_unit(["Prologue", "Extra"]) is None
    assert detect_unit([]) is None


def test_volume_based_manga_is_compared_against_the_volume_count():
    manga = Manga(
        title_en="A Fantasy Lazy Life",
        server_folder="A Fantasy Lazy Life",
        suwayomi_chapter_count=17,
        mu_latest_chapter=85,
        mu_latest_volume=26,
        reading_unit=ReadingUnit.volumes,
    )
    assert manga.authoritative_latest_chapter == 26
    assert manga.chapters_behind == 9


def test_chapter_based_manga_ignores_the_volume_count():
    manga = Manga(
        title_en="8th Circle Mage Reborn",
        server_folder="8th Circle Mage Reborn",
        suwayomi_chapter_count=160,
        mu_latest_chapter=160,
        mu_latest_volume=5,
    )
    assert manga.unit == ReadingUnit.chapters
    assert manga.authoritative_latest_chapter == 160
    assert manga.chapters_behind == 0


def test_volume_based_manga_without_a_volume_count_shows_unknown():
    manga = Manga(
        title_en="X", server_folder="X", suwayomi_chapter_count=3, mu_latest_chapter=40, reading_unit=ReadingUnit.volumes
    )
    assert manga.authoritative_latest_chapter is None
    assert manga.chapters_behind is None
