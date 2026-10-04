import pytest
from beets import ui
from beets.library import Item, Library

from beetsplug.crossref import plugin as plugin_mod
from beetsplug.crossref.cache import Cache
from beetsplug.crossref.http import SourceUnavailable
from beetsplug.crossref.musicbrainz import MusicBrainz
from beetsplug.crossref.sources.base import Source, Updates
from beetsplug.crossref.tracks import TrackHit

MBID = "11111111-2222-3333-4444-555555555555"
MB_URL = "https://fake.test/album/42"


class FakeSource(Source):
    def __init__(self, name="fake"):
        super().__init__(None, None)
        self.name = name
        self.album_field = f"{name}_album_id"
        self.track_field = f"{name}_track_id"
        self.tracklists = {}
        self.by_url = {}
        self.by_barcode = {}
        self.by_isrc = {}
        self.extras = {}
        self.updates = Updates()
        self.calls = []
        self.fail_on = None

    def _hit(self, what):
        self.calls.append(what)
        if self.fail_on == what:
            raise SourceUnavailable("quota")

    def album_id_from_url(self, url):
        self._hit("url")
        return self.by_url.get(url)

    def album_by_barcode(self, barcode):
        self._hit("barcode")
        return self.by_barcode.get(barcode)

    def album_ids_by_isrc(self, isrc):
        self._hit("isrc")
        return self.by_isrc.get(isrc, [])

    def album_fuzzy(self, album, items):
        self._hit("fuzzy")

    def album_tracks(self, album_id):
        self._hit("tracks")
        return self.tracklists.get(album_id)

    def album_extras(self, album_id):
        return self.extras

    def fetch(self, album, items):
        self._hit("fetch")
        return self.updates


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = Library(str(tmp_path / "lib.db"))
    cache = Cache(tmp_path / "cache.db")
    plugin = plugin_mod.CrossrefPlugin()
    urls = []
    monkeypatch.setattr(MusicBrainz, "release_urls", lambda self, mbid: urls)
    yield type("Env", (), {"lib": lib, "cache": cache, "plugin": plugin, "mb_urls": urls})
    cache.close()


def make_album(lib, count=4, **album_fields):
    items = []
    for n in range(1, count + 1):
        item = Item(title=f"t{n}", track=n, length=100.0 + n, isrc=f"ISRC{n}", albumartist="A", album="B")
        lib.add(item)
        items.append(item)
    album = lib.add_album(items)
    album.update(album_fields)
    album.store(inherit=False)
    return album


def hits(count=4):
    return [TrackHit(f"s{n}", 1, n, 100.0 + n, f"ISRC{n}") for n in range(1, count + 1)]


def run_resolve(env, albums, sources, pretend=False):
    env.plugin._resolve(env.lib, albums, sources, env.cache, pretend)


def reload(env, album):
    fresh = env.lib.get_album(album.id)
    return fresh, sorted(fresh.items(), key=lambda i: i.track)


# --- resolve -----------------------------------------------------------------


def test_musicbrainz_link_wins_over_barcode_and_is_recorded(env):
    album = make_album(env.lib, mb_albumid=MBID, barcode="999")
    env.mb_urls.append(MB_URL)
    src = FakeSource()
    src.by_url = {MB_URL: "42"}
    src.by_barcode = {"999": "77"}
    src.tracklists = {"42": hits(), "77": hits()}
    run_resolve(env, [album], [src])
    fresh, items = reload(env, album)
    assert fresh.fake_album_id == "42"
    assert fresh.fake_album_id_source == "musicbrainz"
    assert "barcode" not in src.calls
    assert [i._values_flex["fake_track_id"] for i in items] == ["s1", "s2", "s3", "s4"]
    assert {i._values_flex["fake_track_id_source"] for i in items} == {"musicbrainz"}


def test_musicbrainz_id_is_not_replaced_by_barcode_hit(env):
    album = make_album(env.lib, barcode="999", fake_album_id="1", fake_album_id_source="musicbrainz")
    src = FakeSource()
    src.by_barcode = {"999": "2"}
    src.tracklists = {"2": hits()}
    run_resolve(env, [album], [src])
    assert reload(env, album)[0].fake_album_id == "1"
    assert src.calls == []


def test_barcode_hit_upgrades_a_fuzzy_stored_id(env):
    album = make_album(env.lib, barcode="999", fake_album_id="1")  # no _source record: fuzzy
    src = FakeSource()
    src.by_barcode = {"999": "2"}
    src.tracklists = {"2": hits()}
    run_resolve(env, [album], [src])
    fresh = reload(env, album)[0]
    assert (fresh.fake_album_id, fresh.fake_album_id_source) == ("2", "barcode")


def test_isrc_candidate_under_half_the_tracks_is_rejected(env):
    album = make_album(env.lib)
    src = FakeSource()
    src.by_isrc = {f"ISRC{n}": ["99"] for n in range(1, 5)}
    src.tracklists = {"99": hits(1)}  # lines up 1 of 4 items
    run_resolve(env, [album], [src])
    fresh, items = reload(env, album)
    assert "fake_album_id" not in fresh._values_flex
    assert all("fake_track_id" not in i._values_flex for i in items)


def test_isrc_candidate_with_half_the_tracks_is_accepted(env):
    album = make_album(env.lib)
    src = FakeSource()
    src.by_isrc = {"ISRC1": ["99"]}
    src.tracklists = {"99": hits(2)}
    run_resolve(env, [album], [src])
    fresh, items = reload(env, album)
    assert (fresh.fake_album_id, fresh.fake_album_id_source) == ("99", "isrc")
    assert [i._values_flex.get("fake_track_id") for i in items] == ["s1", "s2", None, None]


def test_isrc_prefers_the_edition_covering_the_bonus_tracks(env):
    album = make_album(env.lib, count=6)
    src = FakeSource()
    # Opening tracks lead to the standard edition, the bonus track to the deluxe.
    src.by_isrc = {"ISRC1": ["std"], "ISRC5": ["std"], "ISRC6": ["dlx"]}
    src.tracklists = {"std": hits(5), "dlx": hits(6)}
    run_resolve(env, [album], [src])
    assert reload(env, album)[0].fake_album_id == "dlx"


def test_stronger_evidence_does_not_swap_in_a_smaller_edition(env):
    album = make_album(env.lib, count=6, fake_album_id="dlx")  # fuzzy, but all 6 tracks line up
    src = FakeSource()
    src.by_isrc = {f"ISRC{n}": ["std"] for n in range(1, 7)}
    src.tracklists = {"std": hits(5), "dlx": hits(6)}
    run_resolve(env, [album], [src])
    fresh = reload(env, album)[0]
    assert fresh.fake_album_id == "dlx"
    assert "fake_album_id_source" not in fresh._values_flex


def test_unreadable_tracklist_skips_candidate(env):
    album = make_album(env.lib)
    src = FakeSource()
    src.by_isrc = {"ISRC1": ["gone", "ok"]}
    src.tracklists = {"ok": hits()}  # "gone": album_tracks returns None
    run_resolve(env, [album], [src])
    assert reload(env, album)[0].fake_album_id == "ok"


def test_album_fields_are_not_copied_onto_items(env):
    album = make_album(env.lib, mb_albumid=MBID)
    env.mb_urls.append(MB_URL)
    src = FakeSource()
    src.by_url = {MB_URL: "42"}
    src.tracklists = {"42": hits()}
    src.extras = {"fake_album_note": "hello"}
    run_resolve(env, [album], [src])
    fresh, items = reload(env, album)
    assert fresh.fake_album_note == "hello"
    for item in items:
        assert "fake_album_id" not in item._values_flex
        assert "fake_album_id_source" not in item._values_flex
        assert "fake_album_note" not in item._values_flex
        assert "fake_track_id" in item._values_flex


def test_stronger_stored_track_id_is_kept(env):
    album = make_album(env.lib, barcode="999")
    first = sorted(album.items(), key=lambda i: i.track)[0]
    first["fake_track_id"] = "old"
    first["fake_track_id_source"] = "musicbrainz"
    first.store()
    src = FakeSource()
    src.by_barcode = {"999": "2"}
    src.tracklists = {"2": hits()}
    run_resolve(env, [album], [src])
    items = reload(env, album)[1]
    assert items[0]._values_flex["fake_track_id"] == "old"
    assert items[1]._values_flex["fake_track_id"] == "s2"


def test_pretend_writes_nothing(env, capsys):
    album = make_album(env.lib, mb_albumid=MBID)
    env.mb_urls.append(MB_URL)
    src = FakeSource()
    src.by_url = {MB_URL: "42"}
    src.tracklists = {"42": hits()}
    run_resolve(env, [album], [src], pretend=True)
    fresh, items = reload(env, album)
    assert "fake_album_id" not in fresh._values_flex
    assert all("fake_track_id" not in i._values_flex for i in items)
    assert "42" in capsys.readouterr().out


def test_source_unavailable_stops_only_that_source(env):
    albums = [make_album(env.lib, mb_albumid=MBID) for _ in range(2)]
    env.mb_urls.append(MB_URL)
    bad = FakeSource("bad")
    bad.fail_on = "url"
    good = FakeSource("good")
    good.by_url = {MB_URL: "42"}
    good.tracklists = {"42": hits()}
    run_resolve(env, albums, [bad, good])
    assert bad.calls == ["url"]  # dropped after the first failure, not asked again for album two
    for album in albums:
        assert reload(env, album)[0].good_album_id == "42"


# --- fetch -------------------------------------------------------------------


def run_fetch(env, albums, sources, pretend=False):
    env.plugin._fetch(env.lib, albums, sources, pretend)


def test_fill_if_empty_fields_keep_existing_but_fill_and_mirror_empty(env):
    album = make_album(env.lib, label="Old Label")
    src = FakeSource()
    src.updates = Updates(album={"label": "New Label", "style": "Rock", "mood": "calm"})
    run_fetch(env, [album], [src])
    fresh, items = reload(env, album)
    assert fresh.label == "Old Label"
    assert fresh.style == "Rock"
    assert fresh.mood == "calm"
    assert [i.style for i in items] == ["Rock"] * 4
    assert [i.label for i in items] == [""] * 4  # label was not filled, so not mirrored
    assert all("mood" not in i._values_flex for i in items)


def test_fill_if_empty_mirror_keeps_items_existing_value(env):
    album = make_album(env.lib)
    first = sorted(album.items(), key=lambda i: i.track)[0]
    first.style = "Jazz"
    first.store()
    src = FakeSource()
    src.updates = Updates(album={"style": "Rock"})
    run_fetch(env, [album], [src])
    assert [i.style for i in reload(env, album)[1]] == ["Jazz", "Rock", "Rock", "Rock"]


def test_flexible_fields_update_on_album_and_items(env):
    album = make_album(env.lib, mood="old")
    first = sorted(album.items(), key=lambda i: i.track)[0]
    src = FakeSource()
    src.updates = Updates(album={"mood": "new"}, items={first.id: {"energy": 7}, 9999: {"energy": 1}})
    run_fetch(env, [album], [src])
    fresh, items = reload(env, album)
    assert fresh.mood == "new"
    assert items[0]._values_flex["energy"] == "7"
    assert "energy" not in items[1]._values_flex


def test_empty_values_never_overwrite(env):
    album = make_album(env.lib, mood="keep")
    src = FakeSource()
    src.updates = Updates(album={"mood": "", "other": None})
    run_fetch(env, [album], [src])
    fresh = reload(env, album)[0]
    assert fresh.mood == "keep"
    assert "other" not in fresh._values_flex


def test_fetch_pretend_writes_nothing(env, capsys):
    album = make_album(env.lib)
    first = album.items()[0]
    src = FakeSource()
    src.updates = Updates(album={"style": "Rock", "mood": "x"}, items={first.id: {"energy": 1}})
    run_fetch(env, [album], [src], pretend=True)
    fresh, items = reload(env, album)
    assert fresh.style == ""
    assert "mood" not in fresh._values_flex
    assert all(i.style == "" and "energy" not in i._values_flex for i in items)
    assert "sets" in capsys.readouterr().out


def test_fetch_source_unavailable_stops_only_that_source(env):
    albums = [make_album(env.lib) for _ in range(2)]
    bad = FakeSource("bad")
    bad.fail_on = "fetch"
    good = FakeSource("good")
    good.updates = Updates(album={"mood": "x"})
    run_fetch(env, albums, [bad, good])
    assert bad.calls == ["fetch"]
    assert all(reload(env, a)[0].mood == "x" for a in albums)


# --- command wiring ----------------------------------------------------------


def test_bare_command_resolves_then_fetches_and_rejects_unknown_sources(env, monkeypatch):
    monkeypatch.setattr(plugin_mod, "REGISTRY", {"fake": "x:Y"})
    monkeypatch.setattr(plugin_mod, "load", lambda name: lambda config, cache: FakeSource(name))
    monkeypatch.setattr(plugin_mod.CrossrefPlugin, "_cache", lambda self: env.cache)
    ran = []
    monkeypatch.setattr(plugin_mod.CrossrefPlugin, "_resolve", lambda self, *a: ran.append("resolve"))
    monkeypatch.setattr(plugin_mod.CrossrefPlugin, "_fetch", lambda self, *a: ran.append("fetch"))
    env.plugin.config["sources"] = ["fake"]
    opts = type("O", (), {"sources": "", "pretend": False})
    env.plugin._command(env.lib, opts, ["artist:nobody"])
    env.plugin._command(env.lib, opts, ["fetch"])
    assert ran == ["resolve", "fetch", "fetch"]
    opts.sources = "nope"
    with pytest.raises(ui.UserError):
        env.plugin._command(env.lib, opts, [])


def test_config_sources_pick_the_sources(env, monkeypatch):
    monkeypatch.setattr(plugin_mod, "REGISTRY", {"a": "x:Y", "b": "x:Y"})
    monkeypatch.setattr(plugin_mod, "load", lambda name: lambda config, cache: FakeSource(name))
    env.plugin.config["sources"] = ["b"]
    assert [s.name for s in env.plugin._sources("", env.cache)] == ["b"]
    assert [s.name for s in env.plugin._sources("a", env.cache)] == ["a"]
    env.plugin.config["sources"] = ["typo"]
    with pytest.raises(ui.UserError):
        env.plugin._sources("", env.cache)
