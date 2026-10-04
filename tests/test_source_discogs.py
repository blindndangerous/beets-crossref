"""DiscogsSource: URL parsing, position mapping and the fields fetch fills."""

from __future__ import annotations

import confuse
import pytest
from beets.library import Album

from beetsplug.crossref.cache import Cache
from beetsplug.crossref.sources.discogs import DiscogsSource


def make(tmp_path, monkeypatch, release=None, token="abc"):
    config = confuse.RootView([confuse.ConfigSource.of({"token": token})])
    source = DiscogsSource(config, Cache(tmp_path / "c.db"))
    monkeypatch.setattr(source.client, "get", lambda path, **params: release)
    return source


def track(position, duration="3:00"):
    return {"position": position, "type_": "track", "title": "t", "duration": duration}


def test_not_ready_without_token(tmp_path, monkeypatch):
    assert not make(tmp_path, monkeypatch, token="").ready
    assert make(tmp_path, monkeypatch).ready


@pytest.mark.parametrize("url, expected", [
    ("https://www.discogs.com/release/1234567", "1234567"),
    ("https://www.discogs.com/release/1234567-Rick-Astley-Never-Gonna", "1234567"),
    ("https://www.discogs.com/master/99-Some-Album", None),
])
def test_album_id_from_url(tmp_path, monkeypatch, url, expected):
    assert make(tmp_path, monkeypatch).album_id_from_url(url) == expected


def slots(tmp_path, monkeypatch, tracklist):
    source = make(tmp_path, monkeypatch, {"tracklist": tracklist})
    return [(h.disc, h.position) for h in source.album_tracks("1")]


def test_disc_dash_track(tmp_path, monkeypatch):
    assert slots(tmp_path, monkeypatch, [track("1-3"), track("CD2-1")]) == [(1, 3), (2, 1)]


def test_vinyl_sides_run_on_one_disc(tmp_path, monkeypatch):
    assert slots(tmp_path, monkeypatch, [track("A1"), track("A2"), track("B1")]) == [(1, 1), (1, 2), (1, 3)]


def test_index_track_uses_sub_tracks_and_headings_are_skipped(tmp_path, monkeypatch):
    tracklist = [
        {"position": "", "type_": "heading", "title": "Side one"},
        track("1"),
        {"position": "2", "type_": "index", "title": "Suite",
         "sub_tracks": [track("2.1", "1:02"), track("2.2", "")]},
    ]
    hits = make(tmp_path, monkeypatch, {"tracklist": tracklist}).album_tracks("7")
    assert [(h.id, h.disc, h.position, h.duration) for h in hits] == [
        ("7-1", 1, 1, 180.0), ("7-2", 1, 2, 62.0), ("7-3", 1, 3, None),
    ]


def test_fetch_fields(tmp_path, monkeypatch):
    release = {
        "styles": ["Euro-Disco", "Synth-pop"], "genres": ["Electronic", "Pop"],
        "labels": [{"name": "RCA (2)", "catno": "PB 41447"}], "tracklist": [],
    }
    source = make(tmp_path, monkeypatch, release)
    album = Album()
    album._values_flex["discogs_album_id"] = "249504"
    updates = source.fetch(album, [])
    assert updates.album == {
        "style": "Euro-Disco, Synth-pop", "label": "RCA", "catalognum": "PB 41447",
        "discogs_genre": "Electronic, Pop",
    }
    assert "genre" not in updates.album and not updates.items


def test_fetch_without_stored_id_and_catno_none(tmp_path, monkeypatch):
    source = make(tmp_path, monkeypatch, {"labels": [{"name": "X", "catno": "none"}], "tracklist": []})
    album = Album()
    assert source.fetch(album, []).album == {}
    album._values_flex["discogs_album_id"] = "1"
    assert source.fetch(album, []).album["catalognum"] == ""
