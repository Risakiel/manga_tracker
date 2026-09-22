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


def best_match_with_margin(
    target: str, candidates: dict[int, str], threshold: int = FUZZY_MATCH_THRESHOLD, margin: int = 10
) -> tuple[int, int] | None:
    """Like best_match, but additionally requires the winner to beat the
    runner-up by `margin` points before it's trusted.

    Used where accepting a fuzzy match auto-commits to a specific external
    identity (e.g. a MangaUpdates series_id) rather than just reconciling
    data already known to be the same manga -- a close call between two
    near-identical titles (sequels, remakes, the same title picked up from
    two different scanlation groups) is left for manual review instead of
    silently picking one.
    """
    if not candidates:
        return None
    normalized_target = normalize_title(target)
    normalized_candidates = {key: normalize_title(title) for key, title in candidates.items()}
    results = process.extract(normalized_target, normalized_candidates, scorer=fuzz.WRatio, limit=2)
    if not results:
        return None
    _, best_score, best_key = results[0]
    if best_score < threshold:
        return None
    if len(results) > 1 and best_score - results[1][1] < margin:
        return None
    return best_key, int(best_score)


def looks_related(a: str, b: str, threshold: int = 55) -> bool:
    """Loose sanity check (well below FUZZY_MATCH_THRESHOLD) for validating
    a match already made through a different signal -- not for picking one
    among candidates. Two genuinely unrelated titles should score well
    under this."""
    return fuzz.WRatio(normalize_title(a), normalize_title(b)) >= threshold
