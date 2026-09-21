"""Tests for plugin.py: top-level orchestration, _process_single_album flows."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from beets import plugins
from beets.library import Album, Item
from beets.test.helper import PluginTestCase

from beetsplug.spotify_album_match import helpers
from beetsplug.spotify_album_match.cli import UserAbort


class SpotifyPluginTestCase(PluginTestCase):
    """beets' own harness: temp library, temp confuse config, real plugin.

    `preload_plugin` is left at its default True, so beets loads the plugin the
    way a real run does -- registering the three flexible field types -- and
    each test picks the resulting instance out of `find_plugins()`. The plugin
    is built with client_id None, so its SpotifyClient never authenticates;
    every test patches `plugin.client` methods, so nothing reaches the network.

    Two consequences of running on real beets models show up throughout:
    `album.items()` re-queries the database and hands back fresh objects, so
    assertions read the stored state back rather than inspecting the objects
    the test created; and beets Models route attribute assignment into their
    field storage, so `store()` can only be spied on via the class.
    """

    plugin = "spotify_album_match"

    def setUp(self):
        super().setUp()
        self.plugin = next(
            p for p in plugins.find_plugins() if p.name == "spotify_album_match"
        )

    def add_album_with_items(self, album_name, albumartist, tracks):
        """Add an album to the temp library. Returns (album, items).

        Each entry in *tracks* is a dict of item fields; an optional "flex" key
        holds flexible fields, stored so `album.items()` sees them.
        """
        items = []
        for fields in tracks:
            fields = dict(fields)
            flex = fields.pop("flex", {})
            item = self.add_item(album=album_name, albumartist=albumartist, **fields)
            for key, value in flex.items():
                item[key] = value
            item.store()
            items.append(item)
        return self.lib.add_album(items), items

    def spy_on_stores(self, model_cls):
        """Patch `model_cls.store` with a pass-through spy."""
        return mock.patch.object(
            model_cls, "store", autospec=True, side_effect=model_cls.store,
        )


class PluginSmokeTests(SpotifyPluginTestCase):
    def test_commands_register(self):
        commands = self.plugin.commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].name, "spotify-album-match")
        self.assertTrue(callable(commands[0].func))

    def test_registers_its_three_flexible_fields(self):
        self.assertEqual(
            set(type(self.plugin).item_types),
            {"spotify_track_id", "spotify_artist_id"},
        )
        self.assertEqual(
            set(type(self.plugin).album_types),
            {"spotify_album_id", "spotify_artist_id"},
        )


class ProcessSingleAlbumTests(SpotifyPluginTestCase):
    def setUp(self):
        super().setUp()
        self.config["spotify_album_match"].set({
            "min_track_artist_score": 0.55,
            "existing_album_repair_strategy": "strict",
        })

    def test_applies_selected_album_id(self):
        album, _items = self.add_album_with_items("Local Album", "Local Artist", [
            {"title": "Track 1", "artist": "Local Artist", "track": 1, "disc": 1},
        ])
        spotify_album = {"id": "sp_album_1", "name": "Matched Album"}

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=[{"id": "sp_track_1"}]):
                with mock.patch.object(self.plugin.matcher, "match_items_to_tracks",
                                       return_value=[]) as match_mock:
                    with self.spy_on_stores(Album) as album_store:
                        self.plugin._process_single_album(
                            album, dry_run=False, interactive=False,
                            force=False, provided_album_id=None,
                        )

        self.assertEqual(album.get("spotify_album_id"), "sp_album_1")
        album_store.assert_called_once_with(album, inherit=False)
        self.assertTrue(match_mock.called)

    def test_dry_run_does_not_verify_a_malformed_stored_album_id(self):
        """The real run deletes the bad ID and searches; the dry run must agree.

        Dry-run cannot delete the field, so the malformed value is still in the
        library when processing continues -- it just must not be used.
        """
        album, _items = self.add_album_with_items("Local Album", "Local Artist", [
            {"title": "Track 1", "artist": "Local Artist", "track": 1, "disc": 1},
        ])
        album["spotify_album_id"] = "not-a-spotify-id"
        album.store(inherit=False)

        with mock.patch.object(self.plugin.repairer, "try_verify_existing_album_id") as verify:
            with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                                   return_value=(None, [])):
                self.plugin._process_single_album(
                    album, dry_run=True, interactive=False, force=False, provided_album_id=None,
                )

        verify.assert_not_called()

    def test_strict_repair_mode_does_not_re_search_the_album(self):
        self.config["spotify_album_match"]["existing_album_repair_strategy"] = "strict"
        album, items = self.add_album_with_items("Division", "10 Years", [
            {"title": "Missing A", "artist": "10 Years", "track": 1, "disc": 1},
            {"title": "Missing B", "artist": "10 Years", "track": 2, "disc": 1},
        ])
        album["spotify_album_id"] = "albuma0000000000000000"
        album.store(inherit=False)

        with mock.patch.object(self.plugin.client, "get_album", return_value=None):
            with mock.patch.object(
                self.plugin.client, "get_album_tracks",
                return_value=[{"id": "track_a", "disc_number": 1, "track_number": 1}],
            ):
                with mock.patch.object(
                    self.plugin.matcher, "match_items_to_tracks",
                    return_value=items,
                ):
                    with mock.patch.object(
                        self.plugin.matcher, "find_best_album_match",
                    ) as find_album_mock:
                        self.plugin._process_single_album(
                            album, dry_run=False, interactive=False,
                            force=False, provided_album_id=None,
                        )

        find_album_mock.assert_not_called()

    def test_related_release_repair_mode_uses_related_release_matcher(self):
        self.config["spotify_album_match"]["existing_album_repair_strategy"] = "related_release"
        album, items = self.add_album_with_items("Album", "Artist", [
            {
                "title": "Bonus 1", "artist": "Artist", "track": 11, "disc": 1,
                "flex": {"spotify_track_id": "old1000000000000000000"},
            },
            {
                "title": "Bonus 2", "artist": "Artist", "track": 12, "disc": 1,
                "flex": {"spotify_track_id": "old2000000000000000000"},
            },
        ])
        album["spotify_album_id"] = "basealbum0000000000000"
        album.store(inherit=False)

        with mock.patch.object(self.plugin.client, "get_album", return_value=None):
            with mock.patch.object(self.plugin.client, "get_album_tracks", return_value=[{"id": "t"}]):
                with mock.patch.object(
                    self.plugin.repairer, "evaluate_existing_track_ids",
                    return_value=(items, [], 2, 2),
                ):
                    with mock.patch.object(
                        self.plugin.matcher, "match_items_to_tracks",
                        return_value=items,
                    ):
                        with mock.patch.object(
                            self.plugin.repairer, "repair_from_related_releases",
                            return_value=[],
                        ) as related_repair_mock:
                            with mock.patch.object(
                                self.plugin.matcher, "find_best_album_match",
                            ) as find_album_mock:
                                self.plugin._process_single_album(
                                    album, dry_run=False, interactive=False,
                                    force=False, provided_album_id=None,
                                )

        related_repair_mock.assert_called_once()
        find_album_mock.assert_not_called()

    def test_dry_run_does_not_store(self):
        album, (item,) = self.add_album_with_items("Album", "Artist", [
            {"title": "Track 1", "artist": "Artist", "track": 1, "disc": 1, "length": 180.0},
        ])
        spotify_album = {"id": "sp_album_1", "name": "Matched Album"}
        spotify_tracks = [{
            "id": "sp_track_1", "name": "Track 1",
            "artists": [{"name": "Artist"}], "disc_number": 1,
            "track_number": 1, "duration_ms": 180000,
        }]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=spotify_tracks):
                with self.spy_on_stores(Album) as album_store:
                    with self.spy_on_stores(Item) as item_store:
                        self.plugin._process_single_album(
                            album, dry_run=True, interactive=False,
                            force=False, provided_album_id=None,
                        )

        album_store.assert_not_called()
        item_store.assert_not_called()
        item.load()
        self.assertNotIn("spotify_track_id", item)


class NoAlbumMatchTests(SpotifyPluginTestCase):
    """What happens when no Spotify album matches at all."""

    def test_no_album_match_clears_when_clear_on_no_match_enabled(self):
        self.config["spotify_album_match"]["clear_on_no_match"] = True
        album, _items = self.add_album_with_items("Ghost Album", "Artist", [
            {
                "title": "Track", "artist": "Artist", "track": 1, "disc": 1,
                "flex": {"spotify_track_id": "stale"},
            },
        ])

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(self.plugin, "clear_all_ids") as clear_mock:
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                )

        clear_mock.assert_called_once_with(album, False)


class WrongStoredIdTests(SpotifyPluginTestCase):
    def test_wrong_stored_album_id_is_cleared_and_triggers_fresh_search(self):
        album, (item,) = self.add_album_with_items("Blank Shell", "Some Artist", [
            {
                "title": "Blank Shell", "artist": "Some Artist", "track": 1, "disc": 1,
                "flex": {"spotify_track_id": "wrongtrack000000000000"},
            },
        ])
        album["spotify_album_id"] = "wastelandid00000000000"
        album.store(inherit=False)

        wrong_spotify_album = {
            "id": "wastelandid00000000000", "name": "Wasteland",
            "artists": [{"name": "Brent Faiyaz"}],
        }

        with mock.patch.object(self.plugin.client, "get_album", return_value=wrong_spotify_album), \
                mock.patch.object(self.plugin.client, "get_album_tracks", return_value=[]):
            with mock.patch.object(
                self.plugin, "clear_all_ids", wraps=self.plugin.clear_all_ids,
            ) as clear_mock:
                with mock.patch.object(
                    self.plugin.matcher, "find_best_album_match", return_value=(None, []),
                ) as find_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                    )

        clear_mock.assert_called()
        find_mock.assert_called_once()
        item.load()
        self.assertNotIn("spotify_album_id", album)
        self.assertNotIn("spotify_track_id", item)


class ApplyProvidedAlbumIdTests(SpotifyPluginTestCase):
    def test_stores_album_and_fills_tracks(self):
        album, _items = self.add_album_with_items("Album", "Artist", [
            {"title": "Track 1", "artist": "Artist", "track": 1, "disc": 1},
        ])
        spotify_album = {"id": "sp_abc", "name": "Album", "artists": [{"name": "Artist"}]}
        candidate = {
            "score": 0.9, "album": spotify_album, "track_count": 1,
            "is_variant": False, "popularity": 50,
            "base_title_score": 0.9, "artist_score": 1.0,
            "tracks": [{"id": "sp_t1", "name": "Track 1", "artists": [{"name": "Artist"}]}],
        }

        with mock.patch.object(self.plugin.matcher, "build_candidate_from_album_id", return_value=candidate):
            with mock.patch.object(
                self.plugin, "_apply_authoritative_album_mapping", return_value=[],
            ) as auth_mock:
                with self.spy_on_stores(Album) as album_store:
                    self.plugin._apply_provided_album_id(album, "sp_abc", dry_run=False, force=False)

        self.assertEqual(album.get("spotify_album_id"), "sp_abc")
        album_store.assert_called_once_with(album, inherit=False)
        auth_mock.assert_called_once_with(album, "sp_abc", False)

    def test_dry_run_does_not_store(self):
        album, _items = self.add_album_with_items("Album", "Artist", [
            {"title": "Track 1", "artist": "Artist", "track": 1, "disc": 1},
        ])
        spotify_album = {"id": "sp_abc", "name": "Album", "artists": []}
        candidate = {
            "score": 0.9, "album": spotify_album, "track_count": 1,
            "is_variant": False, "popularity": 0,
            "base_title_score": 0.9, "artist_score": 1.0, "tracks": [],
        }

        with mock.patch.object(self.plugin.matcher, "build_candidate_from_album_id", return_value=candidate):
            with mock.patch.object(self.plugin, "_apply_authoritative_album_mapping", return_value=[]):
                with self.spy_on_stores(Album) as album_store:
                    self.plugin._apply_provided_album_id(album, "sp_abc", dry_run=True, force=False)

        album_store.assert_not_called()


class ClearIdTests(SpotifyPluginTestCase):
    def _bonus_item(self, **flex):
        item = self.add_item(title="Bonus Track", album="Album", albumartist="Artist",
                             track=99, disc=1)
        for key, value in flex.items():
            item[key] = value
        item.store()
        return item

    def test_clear_track_ids_no_op_when_disabled(self):
        self.config["spotify_album_match"]["clear_unmatched_track_ids"] = False
        item = self._bonus_item(spotify_track_id="stale_id")
        with self.spy_on_stores(Item) as item_store:
            self.plugin.clear_track_ids([item], dry_run=False)
        self.assertEqual(item.get("spotify_track_id"), "stale_id")
        item_store.assert_not_called()

    def test_clear_track_ids_dry_run_does_not_store(self):
        self.config["spotify_album_match"]["clear_unmatched_track_ids"] = True
        item = self._bonus_item(spotify_track_id="stale_id")
        with self.spy_on_stores(Item) as item_store:
            self.plugin.clear_track_ids([item], dry_run=True)
        self.assertEqual(item.get("spotify_track_id"), "stale_id")
        item_store.assert_not_called()

    def test_clear_track_ids_skips_items_without_existing_id(self):
        self.config["spotify_album_match"]["clear_unmatched_track_ids"] = True
        item = self.add_item(title="Track No ID", album="Album", albumartist="Artist",
                             track=1, disc=1)
        with self.spy_on_stores(Item) as item_store:
            self.plugin.clear_track_ids([item], dry_run=False)
        item_store.assert_not_called()


class RunSpotifyMatchTests(SpotifyPluginTestCase):
    def setUp(self):
        super().setUp()
        # The plugin has no credentials (None/None) so is_ready is False;
        # force-ready so _run_spotify_match gets past its first guard.
        self.plugin.client._spotify = object()

    def _add_albums(self, *names):
        return [
            self.add_album_with_items(name, "Artist", [
                {"title": f"{name} Track", "artist": "Artist", "track": 1, "disc": 1},
            ])[0]
            for name in names
        ]

    @staticmethod
    def _opts(progress_file, **overrides):
        opts = types.SimpleNamespace(
            force=False, spotify_album_id=None,
            interactive=False, dry_run=False,
            resume=True, progress_file=progress_file, clear_progress=False,
        )
        for key, value in overrides.items():
            setattr(opts, key, value)
        return opts

    def test_run_stops_processing_albums_on_user_abort(self):
        self._add_albums("Album 1", "Album 2")
        opts = self._opts(None, interactive=True, resume=False)

        with mock.patch.object(
            self.plugin, "_process_single_album",
            side_effect=UserAbort("Aborted by user."),
        ) as process_mock:
            self.plugin._run_spotify_match(self.lib, opts, [])

        self.assertEqual(process_mock.call_count, 1)

    def test_progress_is_written_then_resumed_then_cleared(self):
        albums = self._add_albums("Album 1", "Album 2")

        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = os.path.join(tmpdir, "progress.json")
            opts = self._opts(progress_file)

            with mock.patch.object(self.plugin, "_process_single_album") as first:
                self.plugin._run_spotify_match(self.lib, opts, [])
            self.assertEqual(first.call_count, 2)
            with open(progress_file, encoding="utf-8") as fh:
                self.assertEqual(sorted(json.load(fh)), sorted(str(a.id) for a in albums))

            # --resume over a finished run has nothing left to do.
            with mock.patch.object(self.plugin, "_process_single_album") as second:
                self.plugin._run_spotify_match(self.lib, opts, [])
            second.assert_not_called()

            # --clear-progress starts over.
            with mock.patch.object(self.plugin, "_process_single_album") as third:
                self.plugin._run_spotify_match(
                    self.lib, self._opts(progress_file, clear_progress=True), [],
                )
            self.assertEqual(third.call_count, 2)

    def test_dry_run_records_no_progress(self):
        self._add_albums("Album 1")

        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = os.path.join(tmpdir, "progress.json")
            with mock.patch.object(self.plugin, "_process_single_album"):
                self.plugin._run_spotify_match(
                    self.lib, self._opts(progress_file, dry_run=True), [],
                )
            self.assertFalse(os.path.exists(progress_file))

    def test_empty_query_processes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(self.plugin, "_process_single_album") as process:
                self.plugin._run_spotify_match(
                    self.lib, self._opts(os.path.join(tmpdir, "progress.json")), [],
                )

        process.assert_not_called()


class CalculateMatchScoreTests(unittest.TestCase):
    """Helper-level tests that exercise calculate_match_score directly.

    calculate_match_score only reads fields, so detached beets models are
    enough -- no library, and no plugin.
    """

    def setUp(self):
        self.item = Item(title="Track 1", track=1, disc=1)
        self.album = Album(album="Test Album", albumartist="Artist", year=2010)

    def test_year_bonus_applied(self):
        sp_album = {
            "name": "Test Album", "artists": [{"name": "Artist"}],
            "album_type": "album", "release_date": "2010-06-01",
        }
        score_with_year = helpers.calculate_match_score(self.album, [self.item], [], sp_album)
        sp_album_no_year = dict(sp_album, release_date="2005-01-01")
        score_no_year = helpers.calculate_match_score(self.album, [self.item], [], sp_album_no_year)
        self.assertGreater(score_with_year, score_no_year)

    def test_missing_release_date_key_does_not_raise(self):
        sp_album = {
            "name": "Test Album", "artists": [{"name": "Artist"}],
            "album_type": "album",
        }
        try:
            helpers.calculate_match_score(self.album, [self.item], [], sp_album)
        except KeyError:
            self.fail("calculate_match_score raised KeyError on missing release_date")
