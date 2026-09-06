"""Tests for plugin.py: top-level orchestration, _process_single_album flows."""
import pathlib
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plugin_test_utils import fresh_plugin, load_package
from fakes import FakeAlbum, FakeItem


class PluginSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_commands_register(self):
        commands = self.plugin.commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].name, "spotify-album-match")
        self.assertTrue(callable(commands[0].func))

    def test_item_fields_declares_spotify_track_id(self):
        self.assertIn("spotify_track_id", self.plugin.item_fields())

    def test_album_fields_declares_spotify_album_id(self):
        self.assertIn("spotify_album_id", self.plugin.album_fields())


class ProcessSingleAlbumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()
        cls.cli = __import__("beetsplug.spotify_album_match.cli", fromlist=["x"])

    def setUp(self):
        self.plugin = fresh_plugin()
        self.plugin.config.data["min_track_artist_score"] = 0.55
        self.plugin.config.data["existing_album_repair_strategy"] = "strict"

    def test_applies_selected_album_id(self):
        album = FakeAlbum(
            "Local Album", "Local Artist",
            items=[FakeItem("Track 1", artist="Local Artist", albumartist="Local Artist", track=1, disc=1)],
        )
        spotify_album = {"id": "sp_album_1", "name": "Matched Album"}

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=[{"id": "sp_track_1"}]):
                with mock.patch.object(self.plugin.matcher, "match_items_to_tracks",
                                       return_value=[]) as match_mock:
                    with mock.patch.object(self.plugin.repairer, "fallback_track_search") as fallback_mock:
                        self.plugin._process_single_album(
                            album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                        )

        self.assertEqual(album.get("spotify_album_id"), "sp_album_1")
        self.assertEqual(album.store_calls, 1)
        self.assertTrue(match_mock.called)
        fallback_mock.assert_not_called()

    def test_falls_back_when_no_album_match(self):
        item = FakeItem("Track 1", artist="Local Artist", albumartist="Local Artist", track=1, disc=1)
        album = FakeAlbum("Local Album", "Local Artist", items=[item])
        self.plugin.config.data["use_track_fallback"] = True

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(
                self.plugin.repairer, "fallback_track_search",
                return_value={
                    "matched": 0, "album_id_changed": False,
                    "new_album_id": None, "consensus_album_obj": None,
                },
            ) as fallback_mock:
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                )

        fallback_mock.assert_called_once()
        # Positional args[0] is album, args[1] is items list.
        args, kwargs = fallback_mock.call_args
        self.assertIs(args[0], album)
        self.assertEqual(len(args[1]), 1)
        self.assertFalse(kwargs["overwrite"])

    def test_use_track_fallback_false_does_not_call_fallback(self):
        item = FakeItem("Track", artist="Artist", albumartist="Artist", track=1, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item])
        # use_track_fallback defaults to False — do not set it.

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(self.plugin.repairer, "fallback_track_search") as fallback_mock:
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                )

        fallback_mock.assert_not_called()

    def test_two_runs_fill_missing_then_second_run_does_not_repeat_fallback(self):
        self.plugin.config.data["existing_album_repair_strategy"] = "global_fallback"
        self.plugin.config.data["use_track_fallback"] = True
        item1 = FakeItem("Scream at the Walls", artist="10 Years", albumartist="10 Years", track=1, disc=1)
        item2 = FakeItem("Cycle of Life", artist="10 Years", albumartist="10 Years", track=2, disc=1)
        album = FakeAlbum("Division", "10 Years", items=[item1, item2])
        album["spotify_album_id"] = "oldalbum00000000000000"

        old_album_tracks = [
            {"id": "old_track_a", "disc_number": 1, "track_number": 7},
            {"id": "old_track_b", "disc_number": 1, "track_number": 8},
        ]
        new_album_tracks = [
            {
                "id": "newtrack10000000000000", "name": "Scream at the Walls",
                "artists": [{"name": "10 Years"}], "disc_number": 1,
                "track_number": 1, "duration_ms": 210000,
            },
            {
                "id": "newtrack20000000000000", "name": "Cycle of Life",
                "artists": [{"name": "10 Years"}], "disc_number": 1,
                "track_number": 2, "duration_ms": 220000,
            },
        ]

        def get_tracks(album_id):
            if album_id == "oldalbum00000000000000":
                return old_album_tracks
            if album_id == "newalbum00000000000000":
                return new_album_tracks
            return []

        def search_track(item, _album, *, min_artist_score=None):
            if item.title == "Scream at the Walls":
                return {"id": "newtrack10000000000000", "album": {"id": "newalbum00000000000000"}}
            if item.title == "Cycle of Life":
                return {"id": "newtrack20000000000000", "album": {"id": "newalbum00000000000000"}}
            return None

        with mock.patch.object(self.plugin.client, "get_album", return_value=None):
            with mock.patch.object(self.plugin.client, "get_album_tracks", side_effect=get_tracks):
                with mock.patch.object(self.plugin.matcher, "search_spotify_track", side_effect=search_track):
                    with mock.patch.object(
                        self.plugin.repairer, "fallback_track_search",
                        wraps=self.plugin.repairer.fallback_track_search,
                    ) as fallback_mock:
                        with mock.patch.object(self.plugin.matcher, "find_best_album_match") as find_album_mock:
                            self.plugin._process_single_album(
                                album, dry_run=False, interactive=False,
                                force=False, provided_album_id=None,
                            )
                            self.plugin._process_single_album(
                                album, dry_run=False, interactive=False,
                                force=False, provided_album_id=None,
                            )

        self.assertEqual(album.get("spotify_album_id"), "newalbum00000000000000")
        self.assertEqual(item1.get("spotify_track_id"), "newtrack10000000000000")
        self.assertEqual(item2.get("spotify_track_id"), "newtrack20000000000000")
        self.assertEqual(fallback_mock.call_count, 1)
        find_album_mock.assert_not_called()

    def test_strict_repair_mode_skips_fallback_for_existing_album(self):
        self.plugin.config.data["existing_album_repair_strategy"] = "strict"
        item1 = FakeItem("Missing A", artist="10 Years", albumartist="10 Years", track=1, disc=1)
        item2 = FakeItem("Missing B", artist="10 Years", albumartist="10 Years", track=2, disc=1)
        album = FakeAlbum("Division", "10 Years", items=[item1, item2])
        album["spotify_album_id"] = "albuma0000000000000000"

        with mock.patch.object(self.plugin.client, "get_album", return_value=None):
            with mock.patch.object(
                self.plugin.client, "get_album_tracks",
                return_value=[{"id": "track_a", "disc_number": 1, "track_number": 1}],
            ):
                with mock.patch.object(
                    self.plugin.matcher, "match_items_to_tracks",
                    return_value=[item1, item2],
                ):
                    with mock.patch.object(self.plugin.repairer, "fallback_track_search") as fallback_mock:
                        with mock.patch.object(self.plugin.matcher, "find_best_album_match") as find_album_mock:
                            self.plugin._process_single_album(
                                album, dry_run=False, interactive=False,
                                force=False, provided_album_id=None,
                            )

        fallback_mock.assert_not_called()
        find_album_mock.assert_not_called()

    def test_related_release_repair_mode_uses_related_release_matcher(self):
        self.plugin.config.data["existing_album_repair_strategy"] = "related_release"
        item1 = FakeItem("Bonus 1", artist="Artist", albumartist="Artist", track=11, disc=1)
        item2 = FakeItem("Bonus 2", artist="Artist", albumartist="Artist", track=12, disc=1)
        item1["spotify_track_id"] = "old1000000000000000000"
        item2["spotify_track_id"] = "old2000000000000000000"
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        album["spotify_album_id"] = "basealbum0000000000000"

        with mock.patch.object(self.plugin.client, "get_album", return_value=None), \
                mock.patch.object(self.plugin.client, "get_tracks_bulk", return_value={}):
            with mock.patch.object(self.plugin.client, "get_album_tracks", return_value=[{"id": "t"}]):
                with mock.patch.object(
                    self.plugin.repairer, "evaluate_existing_track_ids",
                    return_value=([item1, item2], [], 2, 2),
                ):
                    with mock.patch.object(
                        self.plugin.matcher, "match_items_to_tracks",
                        return_value=[item1, item2],
                    ):
                        with mock.patch.object(
                            self.plugin.repairer, "repair_from_related_releases",
                            return_value=[],
                        ) as related_repair_mock:
                            with mock.patch.object(self.plugin.repairer, "fallback_track_search") as fallback_mock:
                                with mock.patch.object(self.plugin.matcher, "find_best_album_match") as find_album_mock:
                                    self.plugin._process_single_album(
                                        album, dry_run=False, interactive=False,
                                        force=False, provided_album_id=None,
                                    )

        related_repair_mock.assert_called_once()
        fallback_mock.assert_not_called()
        find_album_mock.assert_not_called()

    def test_dry_run_does_not_store(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist", track=1, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item])
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
                self.plugin._process_single_album(
                    album, dry_run=True, interactive=False, force=False, provided_album_id=None,
                )

        self.assertEqual(album.store_calls, 0)
        self.assertEqual(item.store_calls, 0)


class FallbackConsensusValidationTests(unittest.TestCase):
    """When fallback consensus picks an unrelated album, all IDs should clear."""

    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_fallback_unrelated_album_clears_ids(self):
        # Use a high threshold so the unrelated consensus is definitely below it.
        self.plugin.config.data["fallback_album_validation_threshold"] = 0.9
        self.plugin.config.data["verify_existing_ids"] = False
        self.plugin.config.data["use_track_fallback"] = True
        item = FakeItem("Blank Shell", artist="Some Artist", albumartist="Some Artist", track=1, disc=1)
        album = FakeAlbum("My Obscure Album", "Some Artist", items=[item])

        fallback_result = {
            "matched": 1, "album_id_changed": True,
            "new_album_id": "wasteland_id",
            "consensus_album_obj": {
                "id": "wasteland_id", "name": "Wasteland",
                "artists": [{"name": "Brent Faiyaz"}],
            },
        }

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(self.plugin.repairer, "fallback_track_search",
                                   return_value=fallback_result):
                with mock.patch.object(self.plugin, "clear_all_ids") as clear_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                    )

        clear_mock.assert_called_once_with(album, False)

    def test_fallback_matching_album_proceeds_to_authoritative_mapping(self):
        self.plugin.config.data["fallback_album_validation_threshold"] = 0.35
        self.plugin.config.data["use_track_fallback"] = True
        item = FakeItem("Track One", artist="Artist", albumartist="Artist", track=1, disc=1)
        album = FakeAlbum("My Album", "Artist", items=[item])

        fallback_result = {
            "matched": 1, "album_id_changed": True,
            "new_album_id": "real_album_id",
            "consensus_album_obj": {
                "id": "real_album_id", "name": "My Album",
                "artists": [{"name": "Artist"}],
            },
        }

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(self.plugin.repairer, "fallback_track_search",
                                   return_value=fallback_result):
                with mock.patch.object(
                    self.plugin, "_apply_authoritative_album_mapping", return_value=[],
                ) as auth_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                    )

        auth_mock.assert_called_once_with(album, "real_album_id", False)

    def test_no_album_match_clears_when_clear_on_no_match_enabled(self):
        self.plugin.config.data["clear_on_no_match"] = True
        item = FakeItem("Track", artist="Artist", albumartist="Artist", track=1, disc=1)
        item["spotify_track_id"] = "stale"
        album = FakeAlbum("Ghost Album", "Artist", items=[item])

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(self.plugin, "clear_all_ids") as clear_mock:
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                )

        clear_mock.assert_called_once_with(album, False)

    def test_fallback_no_consensus_clears_when_clear_on_no_match_enabled(self):
        self.plugin.config.data["clear_on_no_match"] = True
        self.plugin.config.data["use_track_fallback"] = True
        item = FakeItem("Track", artist="Artist", albumartist="Artist", track=1, disc=1)
        item["spotify_track_id"] = "stale"
        album = FakeAlbum("Ghost Album", "Artist", items=[item])

        fallback_result = {
            "matched": 0, "album_id_changed": False,
            "new_album_id": None, "consensus_album_obj": None,
        }

        with mock.patch.object(self.plugin.matcher, "find_best_album_match", return_value=(None, [])):
            with mock.patch.object(self.plugin.repairer, "fallback_track_search", return_value=fallback_result):
                with mock.patch.object(self.plugin, "clear_all_ids") as clear_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                    )

        clear_mock.assert_called_once_with(album, False)


class WrongStoredIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_wrong_stored_album_id_is_cleared_and_triggers_fresh_search(self):
        item = FakeItem("Blank Shell", artist="Some Artist", albumartist="Some Artist", track=1, disc=1)
        item["spotify_track_id"] = "wrongtrack000000000000"
        album = FakeAlbum("Blank Shell", "Some Artist", items=[item])
        album["spotify_album_id"] = "wastelandid00000000000"

        wrong_spotify_album = {
            "id": "wastelandid00000000000", "name": "Wasteland",
            "artists": [{"name": "Brent Faiyaz"}],
        }

        def fake_clear_all(alb, dry):
            alb["spotify_album_id"] = ""
            for it in alb.items():
                it["spotify_track_id"] = ""

        with mock.patch.object(self.plugin.client, "get_album", return_value=wrong_spotify_album), \
                mock.patch.object(self.plugin.client, "get_album_tracks", return_value=[]), \
                mock.patch.object(self.plugin.client, "get_tracks_bulk", return_value={}):
            with mock.patch.object(self.plugin, "clear_all_ids", side_effect=fake_clear_all) as clear_mock:
                with mock.patch.object(
                    self.plugin.matcher, "find_best_album_match", return_value=(None, []),
                ) as find_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                    )

        clear_mock.assert_called()
        find_mock.assert_called_once()


class ApplyProvidedAlbumIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_stores_album_and_fills_tracks(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist", track=1, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item])
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
                self.plugin._apply_provided_album_id(album, "sp_abc", dry_run=False, force=False)

        self.assertEqual(album.get("spotify_album_id"), "sp_abc")
        self.assertEqual(album.store_calls, 1)
        auth_mock.assert_called_once_with(album, "sp_abc", False)

    def test_dry_run_does_not_store(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist", track=1, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item])
        spotify_album = {"id": "sp_abc", "name": "Album", "artists": []}
        candidate = {
            "score": 0.9, "album": spotify_album, "track_count": 1,
            "is_variant": False, "popularity": 0,
            "base_title_score": 0.9, "artist_score": 1.0, "tracks": [],
        }

        with mock.patch.object(self.plugin.matcher, "build_candidate_from_album_id", return_value=candidate):
            with mock.patch.object(self.plugin, "_apply_authoritative_album_mapping", return_value=[]):
                self.plugin._apply_provided_album_id(album, "sp_abc", dry_run=True, force=False)

        self.assertEqual(album.store_calls, 0)


class ClearIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_clear_track_ids_clears_when_enabled(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = True
        item = FakeItem("Bonus Track", track=99, disc=1)
        item["spotify_track_id"] = "stale_id"
        self.plugin.clear_track_ids([item], dry_run=False)
        self.assertNotIn("spotify_track_id", item)
        self.assertEqual(item.store_calls, 1)

    def test_clear_track_ids_no_op_when_disabled(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = False
        item = FakeItem("Bonus Track", track=99, disc=1)
        item["spotify_track_id"] = "stale_id"
        self.plugin.clear_track_ids([item], dry_run=False)
        self.assertEqual(item.get("spotify_track_id"), "stale_id")
        self.assertEqual(item.store_calls, 0)

    def test_clear_track_ids_dry_run_does_not_store(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = True
        item = FakeItem("Bonus Track", track=99, disc=1)
        item["spotify_track_id"] = "stale_id"
        self.plugin.clear_track_ids([item], dry_run=True)
        self.assertEqual(item.get("spotify_track_id"), "stale_id")
        self.assertEqual(item.store_calls, 0)

    def test_clear_track_ids_skips_items_without_existing_id(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = True
        item = FakeItem("Track No ID", track=1, disc=1)
        self.plugin.clear_track_ids([item], dry_run=False)
        self.assertEqual(item.store_calls, 0)

    def test_clear_all_ids_clears_album_and_tracks(self):
        item1 = FakeItem("Track 1", track=1, disc=1)
        item1["spotify_track_id"] = "t1"
        item2 = FakeItem("Track 2", track=2, disc=1)
        item2["spotify_track_id"] = "t2"
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        album["spotify_album_id"] = "album_id"

        self.plugin.clear_all_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        self.assertNotIn("spotify_track_id", item1)
        self.assertNotIn("spotify_track_id", item2)
        self.assertGreater(album.store_calls, 0)
        self.assertGreater(item1.store_calls, 0)
        self.assertGreater(item2.store_calls, 0)

    def test_clear_all_ids_dry_run_does_not_store(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = "t1"
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = "album_id"

        self.plugin.clear_all_ids(album, dry_run=True)

        self.assertEqual(album.get("spotify_album_id"), "album_id")
        self.assertEqual(item.get("spotify_track_id"), "t1")
        self.assertEqual(album.store_calls, 0)
        self.assertEqual(item.store_calls, 0)


class RunSpotifyMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()
        cls.cli = __import__("beetsplug.spotify_album_match.cli", fromlist=["x"])

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_run_stops_processing_albums_on_user_abort(self):
        class FakeLib:
            def __init__(self, albums):
                self._albums = albums

            def albums(self, _query):
                return self._albums

        album1 = FakeAlbum("Album 1", "Artist")
        album2 = FakeAlbum("Album 2", "Artist")
        lib = FakeLib([album1, album2])
        opts = types.SimpleNamespace(
            debug=False, force=False, spotify_album_id=None,
            interactive=True, dry_run=False,
            resume=False, progress_file=None, clear_progress=False,
        )
        # Plugin client has no creds (None/None) so is_ready=False; force-ready for this test.
        self.plugin.client._spotify = object()

        with mock.patch.object(
            self.plugin, "_process_single_album",
            side_effect=self.cli.UserAbort("Aborted by user."),
        ) as process_mock:
            self.plugin._run_spotify_match(lib, opts, [])

        self.assertEqual(process_mock.call_count, 1)


class CalculateMatchScoreTests(unittest.TestCase):
    """Helper-level tests that exercise calculate_match_score directly."""

    @classmethod
    def setUpClass(cls):
        load_package()
        cls.helpers = __import__("beetsplug.spotify_album_match.helpers", fromlist=["x"])

    def test_year_bonus_applied(self):
        item = FakeItem("Track 1", track=1, disc=1)
        album = FakeAlbum("Test Album", "Artist", year=2010, items=[item])
        sp_album = {
            "name": "Test Album", "artists": [{"name": "Artist"}],
            "album_type": "album", "release_date": "2010-06-01",
        }
        score_with_year = self.helpers.calculate_match_score(album, [item], [], sp_album)
        sp_album_no_year = dict(sp_album, release_date="2005-01-01")
        score_no_year = self.helpers.calculate_match_score(album, [item], [], sp_album_no_year)
        self.assertGreater(score_with_year, score_no_year)

    def test_missing_release_date_key_does_not_raise(self):
        item = FakeItem("Track 1", track=1, disc=1)
        album = FakeAlbum("Test Album", "Artist", year=2010, items=[item])
        sp_album = {
            "name": "Test Album", "artists": [{"name": "Artist"}],
            "album_type": "album",
        }
        try:
            self.helpers.calculate_match_score(album, [item], [], sp_album)
        except KeyError:
            self.fail("calculate_match_score raised KeyError on missing release_date")


if __name__ == "__main__":
    unittest.main()
