"""ItunesSource with canned API payloads; nothing reaches the network."""

from __future__ import annotations

import pytest
from beets import library

from beetsplug.crossref.cache import MISSING, Cache
from beetsplug.crossref.sources.itunes import ItunesSource

ALBUM_PAYLOAD = {
    "resultCount": 3,
    "results": [
        {
            "wrapperType": "collection",
            "collectionId": 55,
            "primaryGenreName": "Rock",
            "copyright": "(P) 2005 X",
            "collectionExplicitness": "cleaned",
        },
        {
            "wrapperType": "track",
            "trackId": 501,
            "discNumber": 1,
            "trackNumber": 1,
            "trackTimeMillis": 207679,
            "trackExplicitness": "explicit",
        },
        {
            "wrapperType": "track",
            "trackId": 502,
            "discNumber": 2,
            "trackNumber": 1,
            "trackTimeMillis": 100000,
            "trackExplicitness": "notExplicit",
        },
    ],
}


@pytest.fixture
def source(tmp_path):
    src = ItunesSource({}, Cache(tmp_path / "c.db"))
    src.calls = []
    return src


def stub(source, payload):
    def get(path, **params):
        source.calls.append(params)
        return payload

    source.client.get = get


@pytest.mark.parametrize(
    "url",
    [
        "https://music.apple.com/us/album/some-name/1440857781",
        "https://itunes.apple.com/us/album/id1440857781",
        "https://itunes.apple.com/us/album/name/id1440857781?uo=4",
    ],
)
def test_album_url_forms(source, url):
    assert source.album_id_from_url(url) == "1440857781"


def test_non_album_url(source):
    assert source.album_id_from_url("https://music.apple.com/us/artist/jack-johnson/909253") is None


def test_barcode_not_found_is_cached(source):
    stub(source, {"resultCount": 0, "results": []})
    assert source.album_by_barcode("602537868858") is None
    asked = len(source.calls)
    assert asked == 2  # as given, then the leading-zero variant
    assert source.album_by_barcode("602537868858") is None
    assert len(source.calls) == asked


def test_missing_is_not_cached(source):
    stub(source, MISSING)
    assert source.album_tracks("55") is None
    stub(source, ALBUM_PAYLOAD)
    assert source.album_tracks("55") is not None


def test_album_tracks_mapping(source):
    stub(source, ALBUM_PAYLOAD)
    hits = source.album_tracks("55")
    assert [(h.id, h.disc, h.position, h.duration) for h in hits] == [
        ("501", 1, 1, 207.679),
        ("502", 2, 1, 100.0),
    ]


def test_fetch_reuses_cached_lookup(source, tmp_path):
    stub(source, ALBUM_PAYLOAD)
    lib = library.Library(str(tmp_path / "lib.db"))
    item = library.Item(title="a", itunes_track_id="501")
    other = library.Item(title="b")
    lib.add(item)
    lib.add(other)
    album = lib.add_album([item, other])
    album["itunes_album_id"] = "55"
    source.album_tracks("55")
    updates = source.fetch(album, [item, other])
    assert len(source.calls) == 1
    assert updates.album == {
        "itunes_genre": "Rock",
        "itunes_copyright": "(P) 2005 X",
        "itunes_explicit": "cleaned",
    }
    assert updates.items == {item.id: {"itunes_explicit": "explicit"}}
