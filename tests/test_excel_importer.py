import openpyxl
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Manga
from app.services.excel_importer import import_excel


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_xlsx(tmp_path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "LISTS"
    ws.append(["Libraries", "Titre Anglais", "Titre Dossier Serveur", "MangaUpdates.com (URL)", "Last Scan", "Statut du Manga"])
    for row in rows:
        ws.append(row)
    path = tmp_path / "sample.xlsx"
    wb.save(path)
    return path


def test_import_creates_new_rows(tmp_path, session):
    path = _make_xlsx(
        tmp_path,
        [
            [
                "Manga",
                "8th Circle Mage Reborn",
                "The Rebirth of an 8th-Circled Wizard",
                "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn",
                160,
                "Terminé (Complete)",
            ],
            [
                "Pornhwa",
                "Some Adult Title",
                "Some Adult Title Folder",
                "https://www.mangaupdates.com/series/e401aci/some-adult-title",
                12,
                "En cours (Ongoing)",
            ],
        ],
    )

    summary = import_excel(path, session)

    assert summary.created == 2
    assert summary.updated == 0
    mangas = session.exec(select(Manga)).all()
    assert len(mangas) == 2
    complete = next(m for m in mangas if m.title_en == "8th Circle Mage Reborn")
    assert complete.category.value == "manga"
    assert complete.status.value == "complete"
    assert complete.baseline_chapter_count == 160
    assert complete.mangaupdates_id == 44838842124
    adult = next(m for m in mangas if m.title_en == "Some Adult Title")
    assert adult.category.value == "pornhwa"
    assert adult.status.value == "ongoing"


def test_reimport_updates_without_clobbering_synced_fields(tmp_path, session):
    path = _make_xlsx(
        tmp_path,
        [
            [
                "Manga",
                "8th Circle Mage Reborn",
                "The Rebirth of an 8th-Circled Wizard",
                "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn",
                160,
                "Terminé (Complete)",
            ]
        ],
    )
    import_excel(path, session)

    manga = session.exec(select(Manga)).one()
    manga.author = "Some Author From MangaUpdates Sync"
    session.add(manga)
    session.commit()

    # Re-import with an updated baseline chapter count.
    path2 = _make_xlsx(
        tmp_path,
        [
            [
                "Manga",
                "8th Circle Mage Reborn",
                "The Rebirth of an 8th-Circled Wizard",
                "https://www.mangaupdates.com/series/kljw00c/8th-circle-mage-reborn",
                165,
                "Terminé (Complete)",
            ]
        ],
    )
    summary = import_excel(path2, session)

    assert summary.created == 0
    assert summary.updated == 1
    manga = session.exec(select(Manga)).one()
    assert manga.baseline_chapter_count == 165
    assert manga.author == "Some Author From MangaUpdates Sync"


def test_import_skips_rows_missing_required_fields(tmp_path, session):
    path = _make_xlsx(tmp_path, [["Manga", "No URL Title", "Folder", None, 5, "En cours (Ongoing)"]])
    summary = import_excel(path, session)
    assert summary.created == 0
    assert summary.skipped == 1


def test_import_handles_volume_count_last_scan(tmp_path, session):
    # Some real rows hold e.g. "20 V" (20 volumes) instead of a plain chapter count.
    path = _make_xlsx(
        tmp_path,
        [
            [
                "Manga",
                "Volume Tracked Title",
                "Volume Tracked Title",
                "https://www.mangaupdates.com/series/kljw00c/volume-tracked-title",
                "20 V",
                "En cours (Ongoing)",
            ]
        ],
    )
    summary = import_excel(path, session)
    assert summary.created == 1
    manga = session.exec(select(Manga)).one()
    assert manga.baseline_chapter_count == 20.0
