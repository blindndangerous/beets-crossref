"""Spotify: album IDs, track IDs and the album's artist ID.

Uses the client-credentials flow, so no user login.  Only single-resource
endpoints and search are used (the batch endpoints are being withdrawn for
development-mode apps).  Popularity and audio features are left to beets'
own spotifysync, so fetches is False.
"""

from __future__ import annotations

import re
import time

import requests

from ..cache import MISSING
from ..helpers import (
    artist_set_score,
    build_album_search_queries,
    clean_spotify_id,
    fuzzy_title_score,
)
from ..http import JsonClient, SourceUnavailable
from ..tracks import TrackHit
from .base import Source

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
ALBUM_URL = re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z]+/)?album/([A-Za-z0-9]+)(?:[/?#]|$)")
FUZZY_QUERIES = 3
FUZZY_THRESHOLD = 0.75  # title 0.5 + artist 0.35 + track count 0.10 + year 0.05


def _year(release_date) -> int | None:
    try:
        return int(str(release_date).split("-")[0])
    except ValueError:
        return None


def _items(data, key: str) -> list:
    return ((data or {}).get(key) or {}).get("items") or []


class SpotifySource(Source):
    name = "spotify"
    album_field = "spotify_album_id"
    track_field = "spotify_track_id"
    fetches = False

    def __init__(self, config, cache):
        super().__init__(config, cache)
        config["client_secret"].redact = True
        self.client_id = config["client_id"].get(None)
        self.client_secret = config["client_secret"].get(None)
        self.client = JsonClient("spotify", API_URL, min_interval=0.5)
        self._expires = 0.0

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # --- HTTP ---------------------------------------------------------------

    def _token_request(self) -> dict:
        response = requests.post(
            TOKEN_URL, data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret), timeout=30,
        )
        if response.status_code in (400, 401):
            raise SourceUnavailable(f"spotify rejected the client credentials: HTTP {response.status_code}")
        response.raise_for_status()
        return response.json()

    def _refresh(self) -> None:
        payload = self._token_request()
        self.client.session.headers["Authorization"] = f"Bearer {payload['access_token']}"
        self._expires = time.monotonic() + float(payload.get("expires_in", 3600)) - 60

    def _get(self, path: str, **params):
        if time.monotonic() >= self._expires:
            self._refresh()
        try:
            return self.client.get(path, **params)
        except SourceUnavailable as exc:
            if exc.status != 401:
                raise
        self._refresh()  # the token died early; retry once with a fresh one
        return self.client.get(path, **params)

    # --- resolve ------------------------------------------------------------

    def album_id_from_url(self, url: str) -> str | None:
        match = ALBUM_URL.match(url or "")
        return clean_spotify_id(match.group(1)) if match else None

    def album_by_barcode(self, barcode: str) -> str | None:
        digits = re.sub(r"\D", "", barcode or "")
        if not digits:
            return None
        variants = [digits]
        if len(digits) == 12:
            variants.append("0" + digits)
        elif len(digits) == 13 and digits.startswith("0"):
            variants.append(digits[1:])
        for code in variants:
            def fetch(code=code):
                data = self._get("search", q=f"upc:{code}", type="album", limit=5)
                if data is MISSING:
                    return MISSING
                for album in _items(data, "albums"):
                    album_id = clean_spotify_id((album or {}).get("id"))
                    if album_id:
                        return album_id
                return None
            found = self.cache.remember("spotify-upc", code, fetch)
            if found and found is not MISSING:
                return found
        return None

    def album_ids_by_isrc(self, isrc: str) -> list[str]:
        def fetch():
            data = self._get("search", q=f"isrc:{isrc}", type="track", limit=10)
            if data is MISSING:
                return MISSING
            ids = []
            for track in _items(data, "tracks"):
                album_id = clean_spotify_id(((track or {}).get("album") or {}).get("id"))
                if album_id and album_id not in ids:
                    ids.append(album_id)
            return ids
        found = self.cache.remember("spotify-isrc", isrc.upper(), fetch)
        return [] if found is MISSING else found

    def _search_albums(self, query: str) -> list[dict]:
        """Distilled album results for a text query."""
        def fetch():
            data = self._get("search", q=query, type="album", limit=5)
            if data is MISSING:
                return MISSING
            out = []
            for album in _items(data, "albums"):
                album_id = clean_spotify_id((album or {}).get("id"))
                if album_id:
                    out.append({
                        "id": album_id,
                        "name": album.get("name") or "",
                        "artists": [a.get("name") or "" for a in album.get("artists") or []],
                        "total_tracks": album.get("total_tracks") or 0,
                        "year": _year(album.get("release_date")),
                    })
            return out
        found = self.cache.remember("spotify-search", query, fetch)
        return [] if found is MISSING else found

    def album_fuzzy(self, album, items) -> str | None:
        best_id, best = None, 0.0
        for query in build_album_search_queries(album.album, album.albumartist)[:FUZZY_QUERIES]:
            for cand in self._search_albums(query):
                score = self._score(album, len(items), cand)
                if score > best:
                    best_id, best = cand["id"], score
        return best_id if best >= FUZZY_THRESHOLD else None

    @staticmethod
    def _score(album, count: int, cand: dict) -> float:
        title = fuzzy_title_score(album.album, cand["name"])
        artist = artist_set_score(album.albumartist, cand["artists"])
        total = cand["total_tracks"]
        same_count = 1.0 - abs(count - total) / max(count, total) if count and total else 0.0
        year = 0.0
        if album.year and cand["year"]:
            year = {0: 1.0, 1: 0.5}.get(abs(album.year - cand["year"]), 0.0)
        return title * 0.5 + artist * 0.35 + same_count * 0.10 + year * 0.05

    # --- album contents -----------------------------------------------------

    def album_extras(self, album_id: str) -> dict[str, object]:
        album_id = clean_spotify_id(album_id)
        if not album_id:
            return {}

        def fetch():
            data = self._get(f"albums/{album_id}")
            if not isinstance(data, dict):
                return data  # None for 404, MISSING for a failed request
            artists = data.get("artists") or []
            return {"artist_id": clean_spotify_id((artists[0] or {}).get("id")) if artists else None}
        info = self.cache.remember("spotify-album", album_id, fetch)
        artist_id = None if info is MISSING else (info or {}).get("artist_id")
        return {"spotify_artist_id": artist_id} if artist_id else {}

    def album_tracks(self, album_id: str) -> list[TrackHit] | None:
        album_id = clean_spotify_id(album_id)
        if not album_id:
            return None

        def fetch():
            rows, offset = [], 0
            while True:
                data = self._get(f"albums/{album_id}/tracks", limit=50, offset=offset)
                if data is MISSING or data is None:
                    return data
                page = data.get("items") or []
                for track in page:
                    track_id = clean_spotify_id((track or {}).get("id"))
                    if track_id:
                        rows.append([track_id, track.get("disc_number") or 1,
                                     track.get("track_number") or 0,
                                     (track.get("duration_ms") or 0) / 1000 or None])
                offset += len(page)
                if not page or not data.get("next"):
                    return rows

        found = self.cache.remember("spotify-tracks", album_id, fetch)
        if found is None or found is MISSING:
            return None
        return [TrackHit(id=i, disc=d, position=p, duration=dur, isrc=None) for i, d, p, dur in found]
