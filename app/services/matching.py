import re

from rapidfuzz import fuzz, process

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

FUZZY_MATCH_THRESHOLD = 88


def normalize_title(title: str) -> str:
    return _NORMALIZE_RE.sub(" ", title.lower()).strip()


def best_match(target: str, candidates: dict[int, str]) -> tuple[int, int] | None:
    """Find the best fuzzy match for `target` among {key: title} candidates.

    Returns (key, score) if the score clears FUZZY_MATCH_THRESHOLD, else None.
    """
    if not candidates:
        return None
    normalized_target = normalize_title(target)
    normalized_candidates = {key: normalize_title(title) for key, title in candidates.items()}
    result = process.extractOne(
        normalized_target,
        normalized_candidates,
        scorer=fuzz.WRatio,
    )
    if result is None:
        return None
    _, score, key = result
    if score < FUZZY_MATCH_THRESHOLD:
        return None
    return key, int(score)
