"""Last.fm popularity: playcounts and listener counts for tracks, albums and artists.

Fetch-only, no IDs.  Tracks are grouped by song so every copy of a song costs
one lookup and gets the same numbers.  Field names and types are kept
stable, so stored data and smart playlists keep working.  Anything
updated within `max_age_days` is left alone.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import unicodedata
import uuid

from beets.dbcore import types

from ..cache import MISSING
from ..http import JsonClient, SourceUnavailable
from .base import Source, Updates

API_URL = "https://ws.audioscrobbler.com/2.0/"

ITEM_TYPES = {
    "lastfm_playcount": types.INTEGER,
    "lastfm_listeners": types.INTEGER,
    "lastfm_updated": types.FLOAT,
    "lastfm_match": types.STRING,
}
ALBUM_TYPES = {
    "lastfm_album_playcount": types.INTEGER,
    "lastfm_album_listeners": types.INTEGER,
    "lastfm_album_updated": types.FLOAT,
    "lastfm_artist_playcount": types.INTEGER,
    "lastfm_artist_listeners": types.INTEGER,
    "lastfm_artist_updated": types.FLOAT,
}

_PUNCT = str.maketrans("‘’“”‐‑‒–—", "''\"\"-----")
_BLOCK = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*$")
_FEAT = re.compile(r"\s+(?:feat(?:uring)?|ft|with)\.?\s+.*$", re.IGNORECASE)
_DASH = re.compile(r"\s+-\s+.+$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).translate(_PUNCT)).strip()


def _blocks(title: str) -> str:
    while (shorter := _BLOCK.sub("", title).strip()) != title and shorter:
        title = shorter
    return title


def _strip(title: str) -> str:
    """The base song title: no trailing (...)/[...] blocks, featured credit or ' - suffix'."""
    t = _blocks(_FEAT.sub("", _blocks(_norm(title))).strip())
    return _DASH.sub("", t).strip() or _norm(title)


def _song_key(item) -> str:
    mbid = str(item.get("mb_trackid") or "").strip().casefold()
    if mbid:
        return f"mbid:{mbid}"
    artist = item.get("albumartist") or item.get("artist") or ""
    return f"meta:{_norm(str(artist)).casefold()}|{_strip(str(item.get('title') or '')).casefold()}"


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _count(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _stats(node):
    """[playcount, listeners] from a Last.fm node, or None."""
    if not isinstance(node, dict):
        return None
    playcount, listeners = _count(node.get("playcount")), _count(node.get("listeners"))
    return None if playcount is None or listeners is None else [playcount, listeners]


class LastfmSource(Source):
    name = "lastfm"
    album_field = ""
    resolves = False
    fetches = True

    def __init__(self, config, cache):
        super().__init__(config, cache)
        self.config.add(
            {"apikey": "", "min_interval": 0.5, "match_ratio": 0.85, "prefer_mbid": True, "max_age_days": 30}
        )
        self.config["apikey"].redact = True
        self.apikey = str(self.config["apikey"].get() or os.environ.get("LASTFM_API_KEY", "")).strip()
        self.client = JsonClient("lastfm", API_URL, min_interval=self.config["min_interval"].get(float))

    @property
    def ready(self) -> bool:
        return bool(self.apikey) and not self.apikey.upper().startswith("YOUR_")

    # --- requests -------------------------------------------------------------

    def _request(self, method: str, **params):
        """The decoded answer, None when Last.fm does not know it, MISSING on a transient failure."""
        params = {"method": method, "api_key": self.apikey, "format": "json", **params}
        for attempt in range(4):
            payload = self.client.get("", **params)  # error 6 arrives as HTTP 400 with a JSON body
            if not isinstance(payload, dict) or "error" not in payload:
                return payload
            code = payload["error"]
            if code == 6:
                return None
            if code in (10, 26):
                raise SourceUnavailable(f"Last.fm error {code}: {payload.get('message')}")
            if code in (11, 16, 29) and attempt < 3:
                time.sleep(5 * 2**attempt)
                continue
            if code == 29:
                raise SourceUnavailable("Last.fm rate limit persists")
            return MISSING
        return MISSING

    def _cached(self, namespace: str, method: str, parse, **params):
        """Cached parse(answer); None for a definite miss; MISSING when the request failed."""
        key = json.dumps([method, params], sort_keys=True, ensure_ascii=False)

        def fetch():
            payload = self._request(method, **params)
            return payload if payload is None or payload is MISSING else parse(payload)

        return self.cache.remember(namespace, key, fetch)

    def _track_stats(self, **params):
        result = self._cached("lastfm-track", "track.getInfo", lambda p: _stats(p.get("track")), **params)
        return None if result is MISSING else result

    def _search(self, artist: str, title: str) -> list[dict]:
        def parse(payload):
            found = ((payload.get("results") or {}).get("trackmatches") or {}).get("track") or []
            return [t for t in ([found] if isinstance(found, dict) else found) if isinstance(t, dict)]

        result = self._cached("lastfm-track", "track.search", parse, artist=artist, track=title, limit=30)
        return result if isinstance(result, list) else []

    # --- track cascade ----------------------------------------------------------

    def _named(self, artist: str, title: str):
        """Strict, autocorrected, punctuation-folded and suffix-stripped lookups; (stats, how) or None."""
        n_artist, n_title, stripped = _norm(artist), _norm(title), _strip(title)
        attempts = [("exact", artist, title, 0), ("autocorrect", artist, title, 1)]
        if (n_artist, n_title) != (artist, title):
            attempts += [("normalized", n_artist, n_title, 0), ("normalized", n_artist, n_title, 1)]
        if stripped != n_title:
            attempts += [("stripped", n_artist, stripped, 0), ("stripped", n_artist, stripped, 1)]
        for how, a, t, autocorrect in attempts:
            if a and t and (stats := self._track_stats(artist=a, track=t, autocorrect=autocorrect)):
                return stats, how
        return None

    def _by_search(self, artist: str, title: str):
        wanted = _norm(title).casefold()
        ratio = self.config["match_ratio"].get(float)
        good = []
        for hit in self._search(artist, title):
            name = str(hit.get("name") or "").strip()
            if difflib.SequenceMatcher(None, wanted, _norm(name).casefold()).ratio() >= ratio:
                other = hit.get("artist")
                other = other.get("name") if isinstance(other, dict) else other
                listeners = _count(hit.get("listeners")) or 0
                good.append((listeners, name, str(other or artist), hit.get("mbid") or ""))
        if not good:
            return None
        _, name, other, mbid = max(good, key=lambda g: (g[0], g[1].casefold()))
        by_mbid = self._track_stats(mbid=mbid) if mbid else None
        return by_mbid or self._track_stats(artist=other, track=name, autocorrect=0)

    def _resolve_song(self, item):
        """(stats, how it was found); stats is None with how 'none' when Last.fm does not know it."""
        mbid = str(item.get("mb_trackid") or "").strip()
        artist = str(item.get("artist") or "").strip()
        albumartist = str(item.get("albumartist") or "").strip()
        title = str(item.get("title") or "").strip()
        if self.config["prefer_mbid"].get(bool) and mbid and (stats := self._track_stats(mbid=mbid)):
            return stats, "mbid"
        if found := self._named(artist, title):
            return found
        different = _norm(albumartist).casefold() != _norm(artist).casefold()
        if albumartist and different and (found := self._named(albumartist, title)):
            return found[0], "albumartist"
        if stats := self._by_search(artist or albumartist, title):
            return stats, "search"
        return None, "none"

    # --- fetch ----------------------------------------------------------------

    def _fresh(self, obj, field: str) -> bool:
        try:
            updated = float(obj.get(field))
        except (TypeError, ValueError):
            return False
        return updated > time.time() - self.config["max_age_days"].get(float) * 86400

    def fetch(self, album, items) -> Updates:
        out = Updates()
        now = time.time()
        songs: dict[str, list] = {}
        for item in sorted(items, key=lambda i: i.id or 0):
            songs.setdefault(_song_key(item), []).append(item)
        for group in songs.values():
            if all(self._fresh(i, "lastfm_updated") for i in group):
                continue
            stats, how = self._resolve_song(group[0])
            values = {"lastfm_match": how, "lastfm_updated": now}
            if stats:
                values["lastfm_playcount"], values["lastfm_listeners"] = stats
            for item in group:
                out.items[item.id] = dict(values)
        self._album_fields(album, now, out.album)
        return out

    def _album_fields(self, album, now: float, out: dict) -> None:
        use_mbid = self.config["prefer_mbid"].get(bool)
        artist = str(album.get("albumartist") or "").strip()
        title = str(album.get("album") or "").strip()
        if title and artist and not self._fresh(album, "lastfm_album_updated"):
            mbid = album.get("mb_albumid")
            who = {"mbid": mbid} if use_mbid and _is_uuid(mbid) else {"artist": artist, "album": title}
            stats = self._cached(
                "lastfm-album", "album.getInfo", lambda p: _stats(p.get("album")), autocorrect=1, **who
            )
            self._store(out, "lastfm_album", stats, now)
        if artist and not self._fresh(album, "lastfm_artist_updated"):
            mbid = album.get("mb_albumartistid")
            who = {"mbid": mbid} if use_mbid and _is_uuid(mbid) else {"artist": artist}
            stats = self._cached(
                "lastfm-artist", "artist.getInfo", lambda p: _stats((p.get("artist") or {}).get("stats")),
                autocorrect=1, **who,
            )
            self._store(out, "lastfm_artist", stats, now)

    @staticmethod
    def _store(out: dict, prefix: str, stats, now: float) -> None:
        if stats is MISSING:  # transient failure: ask again next run
            return
        out[f"{prefix}_updated"] = now
        if stats:
            out[f"{prefix}_playcount"], out[f"{prefix}_listeners"] = stats
