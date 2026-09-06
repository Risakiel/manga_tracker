from app.config import settings
from app.models import Category
from app.services import library_client


def _make_library(tmp_path, manga_folders=(), pornhwa_folders=()):
    for name in manga_folders:
        (tmp_path / "Manga" / name).mkdir(parents=True)
    for name in pornhwa_folders:
        (tmp_path / "Pornhwa" / name).mkdir(parents=True)
    return tmp_path


def test_is_mounted_false_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "library_root", tmp_path / "does-not-exist")
    assert library_client.is_mounted() is False
    assert library_client.list_folders(Category.manga) == []


def test_list_folders_scoped_by_category(tmp_path, monkeypatch):
    _make_library(tmp_path, manga_folders=["Apotheosis", "Bakuman"], pornhwa_folders=["100% Personal"])
    monkeypatch.setattr(settings, "library_root", tmp_path)

    assert library_client.is_mounted() is True
    assert library_client.list_folders(Category.manga) == ["Apotheosis", "Bakuman"]
    assert library_client.list_folders(Category.pornhwa) == ["100% Personal"]


def test_suggest_folder_fuzzy_matches_real_names(tmp_path, monkeypatch):
    _make_library(tmp_path, manga_folders=["DICE_ The Cube that Changes Everything"])
    monkeypatch.setattr(settings, "library_root", tmp_path)

    suggestion = library_client.suggest_folder("DICE: The Cube that Changes Everything", Category.manga)
    assert suggestion == "DICE_ The Cube that Changes Everything"


def test_suggest_folder_returns_none_when_no_good_match(tmp_path, monkeypatch):
    _make_library(tmp_path, manga_folders=["Completely Unrelated Title"])
    monkeypatch.setattr(settings, "library_root", tmp_path)

    assert library_client.suggest_folder("Some Other Series Entirely", Category.manga) is None
