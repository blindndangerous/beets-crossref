"""LastfmSource: lookups, grouping, staleness and caching, with no network."""

import time

import confuse
import pytest
from beets import library

from beetsplug.crossref.cache import Cache
from beetsplug.crossref.sources.lastfm import LastfmSource

TRACK_MBID = "11111111-1111-1111-1111-111111111111"
ALBUM_MBID = "22222222-2222-2222-2222-222222222222"
ARTIST_MBID = "33333333-3333-3333-3333-333333333333"


def make_source(tmp_path, monkeypatch, answers, apikey="key", **extra):
    """A source whose client answers from `answers(params)`; also returns the call log."""
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    config = confuse.RootView([confuse.ConfigSource.of({"apikey": apikey, "min_interval": 0, **extra})])
    source = LastfmSource(config, Cache(tmp_path / "cache.db"))
    calls = []

    def get(path, **params):
        calls.append(params)
        return answers(params)

    monkeypatch.setattr(source.client, "get", get)
    return source, calls


def track(playcount=10, listeners=4):
    return {"track": {"playcount": str(playcount), "listeners": str(listeners)}}


NOT_FOUND = {"error": 6, "message": "Track not found"}


def add_album(lib, titles=("Song",), **album_fields):
    album = lib.add_album(
        [
            library.Item(title=t, artist="Artist", albumartist="Artist", album="Album", path=f"/x/{n}.mp3")
            for n, t in enumerate(titles)
        ]
    )
    for key, value in album_fields.items():
        album[key] = value
    album.store()
    return album


@pytest.fixture
def lib(tmp_path):
    # ":memory:" trips beets 2.14's migration backup, so use a real file.
    return library.Library(str(tmp_path / "lib.db"))


def test_not_ready_without_key(tmp_path, monkeypatch):
    source, _ = make_source(tmp_path, monkeypatch, lambda p: NOT_FOUND, apikey="")
    assert not source.ready


def test_not_found_is_cached_as_none(tmp_path, monkeypatch, lib):
    source, calls = make_source(tmp_path, monkeypatch, lambda p: NOT_FOUND)
    assert source._track_stats(artist="A", track="T", autocorrect=0) is None
    assert source._track_stats(artist="A", track="T", autocorrect=0) is None
    assert len(calls) == 1


def test_mbid_is_tried_before_names(tmp_path, monkeypatch, lib):
    source, calls = make_source(tmp_path, monkeypatch, lambda p: track(7, 3))
    album = add_album(lib)
    item = album.items()[0]
    item["mb_trackid"] = TRACK_MBID
    updates = source.fetch(album, [item])
    assert calls[0]["method"] == "track.getInfo" and calls[0]["mbid"] == TRACK_MBID
    assert updates.items[item.id]["lastfm_match"] == "mbid"
    assert updates.items[item.id]["lastfm_playcount"] == 7


def test_copies_of_a_song_cost_one_lookup(tmp_path, monkeypatch, lib):
    source, calls = make_source(tmp_path, monkeypatch, lambda p: track(5, 2))
    album = add_album(lib, titles=("Song", "Song", "Other"))
    items = album.items()
    updates = source.fetch(album, items)
    song_calls = [c for c in calls if c["method"] == "track.getInfo" and c.get("track") == "Song"]
    assert len(song_calls) == 1
    assert {updates.items[i.id]["lastfm_playcount"] for i in items} == {5}
    assert all(i.id in updates.items for i in items)


def test_cascade_falls_back_to_stripped_title(tmp_path, monkeypatch, lib):
    def answers(p):
        if p["method"] == "track.getInfo" and p.get("track") == "Song":
            return track(9, 1)
        return NOT_FOUND

    source, _ = make_source(tmp_path, monkeypatch, answers)
    album = add_album(lib, titles=("Song (Remastered 2011)",))
    updates = source.fetch(album, album.items())
    assert updates.items[album.items()[0].id]["lastfm_match"] == "stripped"


def test_fresh_item_is_skipped(tmp_path, monkeypatch, lib):
    source, calls = make_source(tmp_path, monkeypatch, lambda p: track())
    album = add_album(lib, titles=("Fresh",))
    item = album.items()[0]
    item["lastfm_updated"] = time.time() - 86400
    updates = source.fetch(album, [item])
    assert item.id not in updates.items
    assert not any(c["method"] == "track.getInfo" for c in calls)


def test_album_and_artist_fields_with_artist_cached(tmp_path, monkeypatch, lib):
    def answers(p):
        if p["method"] == "album.getInfo":
            return {"album": {"playcount": "100", "listeners": "40"}}
        if p["method"] == "artist.getInfo":
            return {"artist": {"stats": {"playcount": "9000", "listeners": "800"}}}
        return NOT_FOUND

    source, calls = make_source(tmp_path, monkeypatch, answers)
    first = add_album(lib, mb_albumid=ALBUM_MBID, mb_albumartistid=ARTIST_MBID)
    second = add_album(lib, titles=("B",), mb_albumartistid=ARTIST_MBID)

    out = source.fetch(first, first.items()).album
    assert out["lastfm_album_playcount"] == 100 and out["lastfm_album_listeners"] == 40
    assert out["lastfm_artist_playcount"] == 9000 and out["lastfm_artist_listeners"] == 800
    assert "lastfm_album_updated" in out and "lastfm_artist_updated" in out
    assert calls[-2]["mbid"] == ALBUM_MBID

    source.fetch(second, second.items())
    assert sum(c["method"] == "artist.getInfo" for c in calls) == 1
