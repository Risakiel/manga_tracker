"""Lists real folder names on the mounted NAS library share.

The share (e.g. Unraid's `/mnt/remotes/<host>_Komga`) is expected to be
bind-mounted read-only into this container at `settings.library_root`, with
exactly two subdirectories the app cares about: "Manga" and "Pornhwa" --
matching this app's own `Category` values and the layout the user's existing
download/organize scripts already use.

Everything here degrades gracefully to "nothing found" when the mount isn't
present, so the feature is entirely optional (server_folder just stays a
free-text field in the UI when unmounted).
"""

from pathlib import Path
from typing import Optional

from app.config import settings
from app.models import Category
from app.services.matching import best_match

_CATEGORY_DIRS = {
    Category.manga: "Manga",
    Category.pornhwa: "Pornhwa",
}


def is_mounted() -> bool:
    return settings.library_root.is_dir()


def category_root(category: Category) -> Path:
    return settings.library_root / _CATEGORY_DIRS[category]


def list_folders(category: Category) -> list[str]:
    root = category_root(category)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def suggest_folder(title: str, category: Category) -> Optional[str]:
    """Best-effort fuzzy match of a title against real folder names."""
    folders = list_folders(category)
    if not folders:
        return None
    candidates = dict(enumerate(folders))
    match = best_match(title, candidates)
    if match is None:
        return None
    idx, _score = match
    return candidates[idx]
