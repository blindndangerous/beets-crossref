"""SpotifySource with canned HTTP replies; nothing reaches the network."""

from __future__ import annotations

from types import SimpleNamespace

import confuse
import pytest

from beetsplug.crossref.cache import Cache
from beetsplug.crossref.sources.spotify import SpotifySource
from beetsplug.crossref.tracks import TrackHit

A = "1" * 22
B = "2" * 22
C = "3" * 22


class Reply:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.headers = payload, status, {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        assert self.status_code < 400


@pytest.fixture
def source(tmp_path):
    creds = {"spotify": {"client_id": "i", "client_secret": "s"}}
    config = confuse.RootView([confuse.ConfigSource.of(creds)])
    src = SpotifySource(config["spotify"], Cache(tmp_path / "c.db"))
    src.client.min_interval = 0
    src.tokens = []

    def token():
        src.tokens.append(1)
        return {"access_token": f"t{len(src.tokens)}", "expires_in": 3600}

    src._token_request = token
    return src


def serve(source, handler):
    """Route session.get through handler(path, params) -> Reply; returns the log of (path, params, auth)."""
    log = []

    def get(url, params=None, timeout=None):
        path = url.removeprefix(source.client.base_url + "/")
        log.append((path, params, source.client.session.headers.get("Authorization")))
        return handler(path, params)

    source.client.session.get = get
    return log


def album_item(id_, **extra):
    return {"id": id_, "name": extra.pop("name", "x"), "artists": [{"name": "Band"}], **extra}


def test_not_ready_without_credentials(tmp_path):
    config = confuse.RootView([confuse.ConfigSource.of({"spotify": {"client_id": "i"}})])
    assert not SpotifySource(config["spotify"], Cache(tmp_path / "c.db")).ready


def test_url_ids_are_validated(source):
    assert source.album_id_from_url(f"https://open.spotify.com/album/{A}?si=abc") == A
    assert source.album_id_from_url("https://open.spotify.com/album/short") is None
    assert source.album_id_from_url(f"https://open.spotify.com/album/{A}x") is None
    assert source.album_id_from_url(f"https://open.spotify.com/track/{A}") is None


def test_malformed_ids_from_api_are_dropped(source):
    serve(source, lambda path, params: Reply({
        "albums": {"items": [album_item("bad id"), album_item(None), album_item(B)]},
        "tracks": {"items": [{"album": {"id": "nope"}}, {"album": {"id": C}}, {"album": {"id": C}}]},
    }))
    assert source.album_by_barcode("5012345678900") == B
    assert source.album_ids_by_isrc("GBAAA0000001") == [C]
    assert source.album_tracks("not-an-id") is None


def test_barcode_retries_with_leading_zero_and_caches(source):
    def handler(path, params):
        found = params["q"] == "upc:0123456789012"
        return Reply({"albums": {"items": [album_item(A)] if found else []}})

    log = serve(source, handler)
    assert source.album_by_barcode("123456789012") == A
    assert [p["q"] for _, p, _ in log] == ["upc:123456789012", "upc:0123456789012"]
    assert source.album_by_barcode("123456789012") == A
    assert len(log) == 2


def test_isrc_returns_distinct_album_ids(source):
    serve(source, lambda path, params: Reply({"tracks": {"items": [
        {"album": {"id": A}}, {"album": {"id": B}}, {"album": {"id": A}},
    ]}}))
    assert source.album_ids_by_isrc("GBAAA0000001") == [A, B]


def test_album_tracks_pages_and_maps(source):
    def track(id_, n):
        return {"id": id_, "disc_number": 2, "track_number": n, "duration_ms": 180500}

    def handler(path, params):
        if path == f"albums/{A}/tracks":
            if params["offset"] == 0:
                return Reply({"items": [track(B, 1)], "next": "more"})
            return Reply({"items": [track(C, 2)], "next": None})
        return Reply(None, 404)

    log = serve(source, handler)
    assert source.album_tracks(A) == [TrackHit(B, 2, 1, 180.5), TrackHit(C, 2, 2, 180.5)]
    assert [p["offset"] for _, p, _ in log] == [0, 1]
    assert source.album_tracks(C) is None  # 404


def test_album_extras_gives_first_artist(source):
    serve(source, lambda path, params: Reply({"artists": [{"id": B}, {"id": C}]}))
    assert source.album_extras(A) == {"spotify_artist_id": B}


def test_token_refreshes_once_on_401(source):
    state = {"calls": 0}

    def handler(path, params):
        state["calls"] += 1
        return Reply({}, 401) if state["calls"] == 1 else Reply({"artists": [{"id": B}]})

    log = serve(source, handler)
    assert source.album_extras(A) == {"spotify_artist_id": B}
    assert len(source.tokens) == 2
    assert [auth for _, _, auth in log] == ["Bearer t1", "Bearer t2"]


def test_fuzzy_picks_best_candidate_or_none(source):
    results = {"albums": {"items": [
        album_item(A, name="Other Record", total_tracks=3, release_date="1999-01-01"),
        album_item(B, name="Night Songs (Deluxe Edition)", total_tracks=10, release_date="2001-05-05"),
    ]}}
    serve(source, lambda path, params: Reply(results))
    album = SimpleNamespace(album="Night Songs", albumartist="Band", year=2001)
    assert source.album_fuzzy(album, [object()] * 10) == B

    source.cache.conn.execute("DELETE FROM entries")
    wrong = SimpleNamespace(album="Completely Different", albumartist="Someone Else", year=1980)
    assert source.album_fuzzy(wrong, [object()] * 4) is None
