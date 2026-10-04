"""Discogs: release lookup by URL or barcode, tracklist, style/label/catalognum.

Discogs has no per-track IDs and no ISRCs, so tracks only support matching by
disc, position and duration.  It writes the flexible `discogs_album_id` field
(what beets' MusicBrainz external_ids stores), not the integer `discogs_albumid`.
"""

from __future__ import annotations

import re

from ..cache import MISSING
from ..http import JsonClient
from ..tracks import TrackHit
from .base import Source, Updates

_RELEASE_URL = re.compile(r"discogs\.com/(?:[a-z]{2}/)?release/(\d+)")
_DISC_TRACK = re.compile(r"^[A-Za-z]*\s*(\d+)\s*-\s*(\d+)$")  # "1-3", "CD2-1"
_DISAMBIGUATION = re.compile(r"\s+\(\d+\)$")  # Discogs names duplicates "Name (2)"
SEPARATOR = ", "  # beets' discogs plugin joins styles this way


def _seconds(text: str | None) -> float | None:
    """'3:32' or '1:02:03' -> seconds; None when absent or unparseable."""
    try:
        parts = [int(p) for p in (text or "").split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return float(total) or None


def _flat_tracks(tracklist: list[dict]) -> list[tuple[str, float | None]]:
    """(position, seconds) for real tracks; headings are skipped, and an
    index track is replaced by its sub_tracks."""
    flat = []
    for entry in tracklist:
        if entry.get("sub_tracks"):
            flat.extend(_flat_tracks(entry["sub_tracks"]))
        elif entry.get("type_", "track") == "track":
            flat.append((entry.get("position") or "", _seconds(entry.get("duration"))))
    return flat


def _slots(positions: list[str]) -> list[tuple[int, int]]:
    """(disc, position) per track.

    "1-3" / "CD2-1" carry the disc.  Vinyl sides ("A1", "B2"), plain numbers
    and anything unparseable count on from 1 on disc 1, as beets does when
    it has no disc information.
    """
    matches = [_DISC_TRACK.match(p.strip()) for p in positions]
    if positions and all(matches):
        return [(int(m.group(1)), int(m.group(2))) for m in matches]
    return [(1, n) for n in range(1, len(positions) + 1)]


def _label(name: str) -> str:
    return _DISAMBIGUATION.sub("", name).strip()


class DiscogsSource(Source):
    name = "discogs"
    album_field = "discogs_album_id"
    track_field = None

    def __init__(self, config, cache):
        super().__init__(config, cache)
        # Either a personal access token, or an app's consumer key and secret:
        # Discogs accepts both for database search at 60 requests a minute.
        config.add({"token": "", "key": "", "secret": ""})
        config["token"].redact = True
        config["secret"].redact = True
        token, key, secret = (str(config[k].get() or "").strip() for k in ("token", "key", "secret"))
        if token:
            self.auth = f"Discogs token={token}"
        elif key and secret:
            self.auth = f"Discogs key={key}, secret={secret}"
        else:
            self.auth = ""
        self.client = JsonClient(
            "discogs", "https://api.discogs.com", min_interval=1.0,  # 60 requests/min authenticated
            headers={"Authorization": self.auth} if self.auth else None,
        )

    @property
    def ready(self) -> bool:
        return bool(self.auth)

    def album_id_from_url(self, url: str) -> str | None:
        """Release URLs only: a master is a group of pressings, not one release."""
        match = _RELEASE_URL.search(url)
        return match.group(1) if match else None

    def album_by_barcode(self, barcode: str) -> str | None:
        """First hit; pressings sharing a barcode are interchangeable here."""
        stripped = re.sub(r"[\s-]", "", barcode)
        for code in dict.fromkeys([barcode, stripped]):
            found = self.cache.remember("discogs-barcode", code, lambda code=code: self._search(code))
            if found is not MISSING and found:
                return found
        return None

    def _search(self, code: str):
        data = self.client.get("database/search", barcode=code, type="release")
        if data is MISSING:
            return MISSING
        results = (data or {}).get("results") or []
        return str(results[0]["id"]) if results else ""

    def _release(self, release_id: str):
        """Distilled release for tracks and fetch; None if gone, MISSING on failure."""

        def load():
            data = self.client.get(f"releases/{release_id}")
            if data is None or data is MISSING:
                return data
            labels = data.get("labels") or []
            first = labels[0] if labels else {}
            return {
                "styles": data.get("styles") or [],
                "genres": data.get("genres") or [],
                "label": _label(first.get("name") or ""),
                "catno": (first.get("catno") or "").strip(),
                "tracks": _flat_tracks(data.get("tracklist") or []),
            }

        return self.cache.remember("discogs-release", release_id, load)

    def album_tracks(self, album_id: str) -> list[TrackHit] | None:
        release = self._release(album_id)
        if not release or release is MISSING:
            return None
        tracks = release["tracks"]
        slots = _slots([position for position, _ in tracks])
        return [
            TrackHit(id=f"{album_id}-{n}", disc=disc, position=pos, duration=seconds)
            for n, ((_, seconds), (disc, pos)) in enumerate(zip(tracks, slots, strict=True), 1)
        ]

    def fetch(self, album, items) -> Updates:
        updates = Updates()
        release_id = album._values_flex.get("discogs_album_id")
        if not release_id:
            return updates
        release = self._release(str(release_id))
        if not release or release is MISSING:
            return updates
        catno = release["catno"]
        updates.album = {
            "style": SEPARATOR.join(release["styles"]),
            "label": release["label"],
            "catalognum": "" if catno.lower() == "none" else catno,
            "discogs_genre": SEPARATOR.join(release["genres"]),  # never `genre`: another plugin owns it
        }
        return updates
