"""Tests for plugin.py: top-level orchestration, _process_single_album flows."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from fakes import FakeAlbum, FakeItem
from plugin_test_utils import fresh_plugin

from beetsplug.spotify_album_match import helpers
from beetsplug.spotify_album_match.cli import UserAbort


class PluginSmokeTests(unittest.TestCase):
    def setUp(self):
        self.plugin = fresh_plugin()

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


class ProcessSingleAlbumTests(unittest.TestCase):
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
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False, provided_album_id=None,
                    )

        self.assertEqual(album.get("spotify_album_id"), "sp_album_1")
        self.assertEqual(album.store_calls, 1)
        self.assertTrue(match_mock.called)

    def test_dry_run_does_not_verify_a_malformed_stored_album_id(self):
        """The real run deletes the bad ID and searches; the dry run must agree.

        Dry-run cannot delete the field, so the malformed value is still in the
        library when processing continues -- it just must not be used.
        """
        album = FakeAlbum(
            "Local Album", "Local Artist",
            items=[FakeItem("Track 1", artist="Local Artist", albumartist="Local Artist",
                            track=1, disc=1)],
        )
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
                    with mock.patch.object(
                        self.plugin.matcher, "find_best_album_match",
                    ) as find_album_mock:
                        self.plugin._process_single_album(
                            album, dry_run=False, interactive=False,
                            force=False, provided_album_id=None,
                        )

        find_album_mock.assert_not_called()

    def test_related_release_repair_mode_uses_related_release_matcher(self):
        self.plugin.config.data["existing_album_repair_strategy"] = "related_release"
        item1 = FakeItem("Bonus 1", artist="Artist", albumartist="Artist", track=11, disc=1)
        item2 = FakeItem("Bonus 2", artist="Artist", albumartist="Artist", track=12, disc=1)
        item1["spotify_track_id"] = "old1000000000000000000"
        item2["spotify_track_id"] = "old2000000000000000000"
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        album["spotify_album_id"] = "basealbum0000000000000"

        with mock.patch.object(self.plugin.client, "get_album", return_value=None):
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


class NoAlbumMatchTests(unittest.TestCase):
    """What happens when no Spotify album matches at all."""

    def setUp(self):
        self.plugin = fresh_plugin()

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


class WrongStoredIdTests(unittest.TestCase):
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
            # Mirrors the real clear_all_ids: fields are deleted, never blanked.
            if "spotify_album_id" in alb._values_flex:
                del alb["spotify_album_id"]
            for it in alb.items():
                if "spotify_track_id" in it._values_flex:
                    del it["spotify_track_id"]

        with mock.patch.object(self.plugin.client, "get_album", return_value=wrong_spotify_album), \
                mock.patch.object(self.plugin.client, "get_album_tracks", return_value=[]):
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
    def setUp(self):
        self.plugin = fresh_plugin()

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


class RunSpotifyMatchTests(unittest.TestCase):
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
            force=False, spotify_album_id=None,
            interactive=True, dry_run=False,
            resume=False, progress_file=None, clear_progress=False,
        )
        # Plugin client has no creds (None/None) so is_ready=False; force-ready for this test.
        self.plugin.client._spotify = object()

        with mock.patch.object(
            self.plugin, "_process_single_album",
            side_effect=UserAbort("Aborted by user."),
        ) as process_mock:
            self.plugin._run_spotify_match(lib, opts, [])

        self.assertEqual(process_mock.call_count, 1)

    def _fake_lib(self, albums):
        class FakeLib:
            def albums(self, _query):
                return albums

        return FakeLib()

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

    def test_progress_is_written_then_resumed_then_cleared(self):
        albums = [FakeAlbum("Album 1", "Artist"), FakeAlbum("Album 2", "Artist")]
        lib = self._fake_lib(albums)
        self.plugin.client._spotify = object()

        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = os.path.join(tmpdir, "progress.json")
            opts = self._opts(progress_file)

            with mock.patch.object(self.plugin, "_process_single_album") as first:
                self.plugin._run_spotify_match(lib, opts, [])
            self.assertEqual(first.call_count, 2)
            with open(progress_file, encoding="utf-8") as fh:
                self.assertEqual(sorted(json.load(fh)), sorted(str(a.id) for a in albums))

            # --resume over a finished run has nothing left to do.
            with mock.patch.object(self.plugin, "_process_single_album") as second:
                self.plugin._run_spotify_match(lib, opts, [])
            second.assert_not_called()

            # --clear-progress starts over.
            with mock.patch.object(self.plugin, "_process_single_album") as third:
                self.plugin._run_spotify_match(
                    lib, self._opts(progress_file, clear_progress=True), [],
                )
            self.assertEqual(third.call_count, 2)

    def test_dry_run_records_no_progress(self):
        lib = self._fake_lib([FakeAlbum("Album 1", "Artist")])
        self.plugin.client._spotify = object()

        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = os.path.join(tmpdir, "progress.json")
            with mock.patch.object(self.plugin, "_process_single_album"):
                self.plugin._run_spotify_match(
                    lib, self._opts(progress_file, dry_run=True), [],
                )
            self.assertFalse(os.path.exists(progress_file))

    def test_empty_query_processes_nothing(self):
        lib = self._fake_lib([])
        self.plugin.client._spotify = object()

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(self.plugin, "_process_single_album") as process:
                self.plugin._run_spotify_match(
                    lib, self._opts(os.path.join(tmpdir, "progress.json")), [],
                )

        process.assert_not_called()


class CalculateMatchScoreTests(unittest.TestCase):
    """Helper-level tests that exercise calculate_match_score directly."""

    def test_year_bonus_applied(self):
        item = FakeItem("Track 1", track=1, disc=1)
        album = FakeAlbum("Test Album", "Artist", year=2010, items=[item])
        sp_album = {
            "name": "Test Album", "artists": [{"name": "Artist"}],
            "album_type": "album", "release_date": "2010-06-01",
        }
        score_with_year = helpers.calculate_match_score(album, [item], [], sp_album)
        sp_album_no_year = dict(sp_album, release_date="2005-01-01")
        score_no_year = helpers.calculate_match_score(album, [item], [], sp_album_no_year)
        self.assertGreater(score_with_year, score_no_year)

    def test_missing_release_date_key_does_not_raise(self):
        item = FakeItem("Track 1", track=1, disc=1)
        album = FakeAlbum("Test Album", "Artist", year=2010, items=[item])
        sp_album = {
            "name": "Test Album", "artists": [{"name": "Artist"}],
            "album_type": "album",
        }
        try:
            helpers.calculate_match_score(album, [item], [], sp_album)
        except KeyError:
            self.fail("calculate_match_score raised KeyError on missing release_date")

