"""DeezerSource with canned API payloads; nothing reaches the network."""

from __future__ import annotations

import pytest
from beets import library

from beetsplug.crossref.cache import MISSING, Cache
from beetsplug.crossref.sources.deezer import DeezerSource
from beetsplug.crossref.tracks import TrackHit

NOT_FOUND = {"error": {"type": "DataException", "message": "no data", "code": 800}}
QUOTA = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}


@pytest.fixture
def source(tmp_path):
    return DeezerSource(None, Cache(tmp_path / "c.db"))


def canned(source, replies):
    """Make client.get answer from {path: payload}; returns the list of paths asked."""
    asked = []

    def get(path, **params):
        asked.append(path)
        return replies.get(path)

    source.client.get = get
    return asked


def test_album_url_with_and_without_locale(source):
    assert source.album_id_from_url("https://www.deezer.com/album/123") == "123"
    assert source.album_id_from_url("https://www.deezer.com/en/album/123") == "123"
    assert source.album_id_from_url("https://www.deezer.com/track/123") is None


def test_error_body_is_not_found_and_cached(source):
    asked = canned(source, {"album/upc:111": NOT_FOUND})
    assert source.album_by_barcode("111") is None
    assert source.album_by_barcode("111") is None
    assert asked == ["album/upc:111"]


def test_failed_request_is_not_cached(source):
    asked = canned(source, {"album/upc:111": QUOTA})
    assert source.album_by_barcode("111") is None
    assert source.album_by_barcode("111") is None
    assert len(asked) == 2
    assert source.cache.get("deezer-upc", "111") is MISSING


def test_barcode_retries_with_and_without_leading_zero(source):
    asked = canned(source, {"album/upc:0123456789012": {"id": 7}})
    assert source.album_by_barcode("123456789012") == "7"
    assert asked == ["album/upc:123456789012", "album/upc:0123456789012"]
    canned(source, {"album/upc:987654321098": {"id": 8}})
    assert source.album_by_barcode("0987654321098") == "8"


def test_album_tracks_maps_to_hits_across_pages(source):
    next_url = "https://api.deezer.com/album/5/tracks?index=1"
    page1 = {
        "data": [{"id": 1, "disk_number": 1, "track_position": 1, "duration": 320, "isrc": "GB1"}],
        "next": next_url,
    }
    page2 = {"data": [{"id": 2, "disk_number": 2, "track_position": 1, "duration": 212}]}
    canned(source, {"album/5/tracks": page1, next_url: page2})
    assert source.album_tracks("5") == [
        TrackHit("1", 1, 1, 320.0, "GB1"),
        TrackHit("2", 2, 1, 212.0, None),
    ]
    canned(source, {"album/9/tracks": NOT_FOUND})
    assert source.album_tracks("9") is None


def test_fetch_stores_real_rank_and_reads_own_ids_only(source, tmp_path):
    lib = library.Library(str(tmp_path / "lib.db"))
    item = library.Item(title="a", album="x", track=1)
    other = library.Item(title="b", album="x", track=2)
    lib.add(item)
    lib.add(other)
    album = lib.add_album([item, other])
    album["deezer_album_id"] = "5"
    album.store()
    item["deezer_track_id"] = "11"
    item.store()
    canned(
        source,
        {
            "album/5": {"record_type": "album", "explicit_lyrics": True, "label": "L", "upc": "0123"},
            "track/11": {"bpm": 120.5, "gain": -9.2, "rank": 930849, "explicit_lyrics": False},
        },
    )
    updates = source.fetch(album, [item, other])
    assert updates.album == {
        "deezer_record_type": "album",
        "deezer_explicit": 1,
        "label": "L",
        "barcode": "0123",
    }
    assert updates.items == {
        item.id: {"deezer_bpm": 120.5, "deezer_gain": -9.2, "deezer_rank": 930849, "deezer_explicit": 0}
    }
