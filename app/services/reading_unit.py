"""Tells volume-based mangas from chapter-based ones by their file/book
titles ("Volume 4", "T04" vs "Ch.5", "Chapitre 12")."""

import re
from typing import Iterable, Optional

from app.models import ReadingUnit

_CHAPTER_RE = re.compile(r"(?<![a-z])(?:chapters?|chapitres?|chap|ch|c|episodes?|ep)\s*\.?\s*\d", re.IGNORECASE)
_VOLUME_RE = re.compile(r"(?<![a-z])(?:volumes?|vol|tomes?|t|v)\s*\.?\s*\d", re.IGNORECASE)


def classify_title(title: str) -> Optional[ReadingUnit]:
    """A chapter marker wins over a volume one: "Vol.1 Ch.3" is a chapter
    filed under its volume, not a whole volume."""
    if _CHAPTER_RE.search(title):
        return ReadingUnit.chapters
    if _VOLUME_RE.search(title):
        return ReadingUnit.volumes
    return None


def detect_unit(titles: Iterable[str]) -> Optional[ReadingUnit]:
    """Majority vote over the titles that say something; None when none
    do. A series that switched format midway (a few chapters, then whole
    volumes) goes with whatever most of its books are."""
    chapters = volumes = 0
    for title in titles:
        unit = classify_title(title or "")
        if unit == ReadingUnit.chapters:
            chapters += 1
        elif unit == ReadingUnit.volumes:
            volumes += 1
    if chapters == volumes == 0:
        return None
    return ReadingUnit.volumes if volumes > chapters else ReadingUnit.chapters
