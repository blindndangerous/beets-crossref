"""iTunes Search/Lookup API: album IDs from barcodes, genre, copyright, explicit flags.

No auth, but Apple allows only about 20 requests a minute, hence the 3 s
spacing.  The API has no ISRC lookup, so album_ids_by_isrc stays empty.
One album lookup carries the tracklist and every field fetch() needs, so it
is cached once and shared by album_tracks() and fetch().
"""

from __future__ import annotations

import re

from ..cache import MISSING
from ..http import JsonClient
from ..tracks import TrackHit
from .base import Source, Updates

# /album/<name>/<digits> or /album/id<digits>; artist and song-only URLs do not match.
_ALBUM_URL = re.compile(
    r"^https?://(?:music|itunes)\.apple\.com/(?:[a-z]{2}/)?album/(?:[^/?#]+/)?(?:id)?(\d+)/?(?:[?#]|$)"
)


class ItunesSource(Source):
    name = "itunes"
    album_field = "itunes_album_id"
    track_field = "itunes_track_id"

    def __init__(self, config, cache):
        super().__init__(config, cache)
        if hasattr(config, "add"):
            config.add({"country": "US"})
        self.client = JsonClient("itunes", "https://itunes.apple.com", min_interval=3.0)

    @property
    def country(self) -> str:
        try:
            return str(self.config["country"].get() or "US")
        except Exception:  # missing or empty subview
            return "US"

    def album_id_from_url(self, url: str) -> str | None:
        match = _ALBUM_URL.match(url.strip())
        return match.group(1) if match else None

    def album_by_barcode(self, barcode: str) -> str | None:
        digits = "".join(c for c in barcode if c.isdigit())
        if not digits:
            return None
        variants = [digits]
        if len(digits) == 12:
            variants.append("0" + digits)
        elif len(digits) == 13 and digits.startswith("0"):
            variants.append(digits[1:])
        for code in variants:
            key = f"{self.country}:{code}"
            found = self.cache.remember("itunes-barcode", key, lambda c=code: self._barcode(c))
            if found not in (None, MISSING):
                return found
        return None

    def _barcode(self, code: str):
        """The collection for a barcode.  Asking for its songs in the same call
        fills the album cache too, so the tracklist check costs no second
        request at Apple's 20-a-minute pace."""
        data = self.client.get("lookup", upc=code, entity="song", country=self.country, limit=200)
        if data is MISSING:
            return MISSING
        rows = (data or {}).get("results", [])
        collection = next(
            (r for r in rows if r.get("wrapperType") == "collection" and r.get("collectionId")), None
        )
        if collection is None:
            return None
        album_id = str(collection["collectionId"])
        mine = [r for r in rows if str(r.get("collectionId")) == album_id]
        key = f"{self.country}:{album_id}"
        if self.cache.get("itunes-album", key) is MISSING:
            self.cache.put("itunes-album", key, self._distil(mine))
        return album_id

    def _album(self, album_id: str):
        """Distilled album lookup: collection fields plus tracks; None if unknown."""
        key = f"{self.country}:{album_id}"
        return self.cache.remember("itunes-album", key, lambda: self._load(album_id))

    def _load(self, album_id: str):
        data = self.client.get("lookup", id=album_id, entity="song", country=self.country, limit=200)
        if data is MISSING:
            return MISSING
        return self._distil((data or {}).get("results", []))

    @staticmethod
    def _distil(rows):
        collection = next((r for r in rows if r.get("wrapperType") == "collection"), None)
        if collection is None:
            return None
        tracks = [
            {
                "id": str(r["trackId"]),
                "disc": r.get("discNumber") or 1,
                "position": r.get("trackNumber") or 0,
                "duration": r["trackTimeMillis"] / 1000 if r.get("trackTimeMillis") else None,
                "explicit": r.get("trackExplicitness"),
            }
            for r in rows
            if r.get("wrapperType") == "track" and r.get("trackId")
        ]
        return {
            "genre": collection.get("primaryGenreName"),
            "copyright": collection.get("copyright"),
            "explicit": collection.get("collectionExplicitness"),
            "tracks": tracks,
        }

    def album_tracks(self, album_id: str) -> list[TrackHit] | None:
        data = self._album(album_id)
        if data is None or data is MISSING:
            return None
        return [TrackHit(t["id"], t["disc"], t["position"], t["duration"]) for t in data["tracks"]]

    def fetch(self, album, items) -> Updates:
        updates = Updates()
        album_id = album._values_flex.get(self.album_field)
        if not album_id:
            return updates
        data = self._album(str(album_id))
        if data is None or data is MISSING:
            return updates
        for key, name in (
            ("genre", "itunes_genre"),
            ("copyright", "itunes_copyright"),
            ("explicit", "itunes_explicit"),
        ):
            if data.get(key):
                updates.album[name] = data[key]
        explicit = {t["id"]: t["explicit"] for t in data["tracks"] if t.get("explicit")}
        for item in items:
            track_id = item._values_flex.get(self.track_field)
            if track_id and str(track_id) in explicit:
                updates.items[item.id] = {"itunes_explicit": explicit[str(track_id)]}
        return updates
