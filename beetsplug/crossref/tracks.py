"""Match a service's tracklist to the album's items.

ISRC identifies a recording, not a release, so remasters and compilations
reuse it: an ISRC match must also agree on disc and position, or on duration
within DURATION_TOLERANCE.  Items without a usable ISRC fall back to disc,
position and duration together.
"""

from __future__ import annotations

from dataclasses import dataclass

DURATION_TOLERANCE = 3.0  # seconds


@dataclass(frozen=True)
class TrackHit:
    id: str
    disc: int
    position: int
    duration: float | None = None  # seconds
    isrc: str | None = None


def item_isrcs(item) -> set[str]:
    """beets may hold several ISRCs joined by ';'."""
    return {code.strip().upper() for code in (item.get("isrc") or "").split(";") if code.strip()}


def _close(item, hit: TrackHit) -> bool:
    return hit.duration is None or not item.length or abs(item.length - hit.duration) <= DURATION_TOLERANCE


def _same_slot(item, hit: TrackHit) -> bool:
    return (item.disc or 1) == hit.disc and item.track == hit.position


def match_tracks(items, hits: list[TrackHit]) -> dict[int, TrackHit]:
    """item.id -> the service track it is, for the items that could be matched."""
    matched: dict[int, TrackHit] = {}
    used: set[str] = set()

    by_isrc: dict[str, list[TrackHit]] = {}
    for hit in hits:
        if hit.isrc:
            by_isrc.setdefault(hit.isrc.upper(), []).append(hit)
    for item in items:
        candidates = [h for code in item_isrcs(item) for h in by_isrc.get(code, []) if h.id not in used]
        good = [h for h in candidates if _same_slot(item, h)] or [h for h in candidates if _close(item, h)]
        if good:
            matched[item.id] = good[0]
            used.add(good[0].id)

    for item in items:
        if item.id in matched:
            continue
        for hit in hits:
            if hit.id not in used and _same_slot(item, hit) and _close(item, hit):
                matched[item.id] = hit
                used.add(hit.id)
                break
    return matched
