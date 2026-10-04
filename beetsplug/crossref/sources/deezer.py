"""Deezer: public API, no credentials.

Deezer answers a missing object with HTTP 200 and an {"error": ...} body
instead of 404.  Code 800 (DataException) means "not found" and is cached as
None.  Any other error, notably code 4 (quota exceeded, a short window), is
treated as a failed request: MISSING, never cached, so the next run asks again.
"""

from __future__ import annotations

import re

from ..cache import MISSING
from ..http import JsonClient
from ..tracks import TrackHit
from .base import Source, Updates

_ALBUM_URL = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?album/(\d+)", re.I)
_NOT_FOUND = 800


class DeezerSource(Source):
    name = "deezer"
    album_field = "deezer_album_id"
    track_field = "deezer_track_id"

    def __init__(self, config, cache):
        super().__init__(config, cache)
        # Deezer allows about 50 requests per 5 s.
        self.client = JsonClient("deezer", "https://api.deezer.com", min_interval=0.11)

    def _get(self, path: str, **params):
        """Payload; None when Deezer has no such object; MISSING when the request failed."""
        data = self.client.get(path, **params)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return None if data["error"].get("code") == _NOT_FOUND else MISSING
        return data

    # --- resolve --------------------------------------------------------------

    def album_id_from_url(self, url: str) -> str | None:
        match = _ALBUM_URL.search(url or "")
        return match.group(1) if match else None

    def album_by_barcode(self, barcode: str) -> str | None:
        """Deezer stores some UPCs as 13-digit EANs, so retry with or without the leading zero."""
        barcode = barcode.strip()
        tries = [barcode]
        if len(barcode) == 12 and barcode.isdigit():
            tries.append("0" + barcode)
        elif len(barcode) == 13 and barcode.startswith("0"):
            tries.append(barcode[1:])
        for code in tries:
            found = self.cache.remember("deezer-upc", code, lambda code=code: self._album_id(code))
            if found not in (None, MISSING):
                return found
        return None

    def _album_id(self, barcode: str):
        data = self._get(f"album/upc:{barcode}")
        if data is None or data is MISSING:
            return data
        return str(data["id"]) if data.get("id") else None

    def album_ids_by_isrc(self, isrc: str) -> list[str]:
        def fetch():
            data = self._get(f"track/isrc:{isrc}")
            if data is None or data is MISSING:
                return data
            album = data.get("album") or {}
            return [str(album["id"])] if album.get("id") else None

        found = self.cache.remember("deezer-isrc", isrc, fetch)
        return found if isinstance(found, list) else []

    def album_tracks(self, album_id: str) -> list[TrackHit] | None:
        """album/{id}/tracks carries isrc, disk_number and track_position, so one call per page."""

        def fetch():
            rows: list[dict] = []
            data = self._get(f"album/{album_id}/tracks", limit=500)
            while isinstance(data, dict):
                rows += data.get("data", [])
                if not data.get("next"):
                    return rows
                data = self._get(data["next"])
            return data  # None or MISSING from the first or a later page

        rows = self.cache.remember("deezer-album-tracks", album_id, fetch)
        if not isinstance(rows, list):
            return None
        return [
            TrackHit(
                id=str(row["id"]),
                disc=row.get("disk_number") or 1,
                position=row.get("track_position") or 0,
                duration=float(row["duration"]) if row.get("duration") else None,
                isrc=row.get("isrc") or None,
            )
            for row in rows
        ]

    # --- fetch ----------------------------------------------------------------

    def fetch(self, album, items) -> Updates:
        updates = Updates()
        album_id = album._values_flex.get(self.album_field)
        if album_id:
            fields = self.cache.remember("deezer-album", str(album_id), lambda: self._album_fields(album_id))
            if isinstance(fields, dict):
                updates.album = fields
        for item in items:
            track_id = item._values_flex.get(self.track_field)
            if not track_id:
                continue
            fields = self.cache.remember(
                "deezer-track", str(track_id), lambda tid=track_id: self._track_fields(tid)
            )
            if isinstance(fields, dict) and fields:
                updates.items[item.id] = fields
        return updates

    def _album_fields(self, album_id):
        data = self._get(f"album/{album_id}")
        if data is None or data is MISSING:
            return data
        fields: dict[str, object] = {}
        if data.get("record_type"):
            fields["deezer_record_type"] = data["record_type"]
        if data.get("explicit_lyrics") is not None:
            fields["deezer_explicit"] = int(data["explicit_lyrics"])
        if data.get("label"):
            fields["label"] = data["label"]
        if data.get("upc"):
            fields["barcode"] = data["upc"]
        return fields

    def _track_fields(self, track_id):
        data = self._get(f"track/{track_id}")
        if data is None or data is MISSING:
            return data
        fields: dict[str, object] = {}
        if data.get("bpm"):
            fields["deezer_bpm"] = float(data["bpm"])
        if data.get("gain") is not None:
            fields["deezer_gain"] = float(data["gain"])
        if data.get("rank") is not None:
            fields["deezer_rank"] = int(data["rank"])
        if data.get("explicit_lyrics") is not None:
            fields["deezer_explicit"] = int(data["explicit_lyrics"])
        return fields
