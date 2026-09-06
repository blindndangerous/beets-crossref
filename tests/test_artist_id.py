"""Tests for spotify_artist_id: writes at every match site, clearing, and backfill."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plugin_test_utils import fresh_plugin, load_package
from fakes import FakeAlbum, FakeItem


ALBUM_ID = "albumid000000000000000"
TRACK_ID_1 = "track10000000000000000"
TRACK_ID_2 = "track20000000000000000"
OUTSIDE_TRACK_ID = "outsidetrack0000000000"
RELATED_ALBUM_ID = "relalbum00000000000000"
CONSENSUS_ALBUM_ID = "consensus0000000000000"
ARTIST_ALBUM = "artistalbum00000000000"
ARTIST_TRACK_1 = "artisttrack00000000000"
ARTIST_TRACK_2 = "artisttrack20000000000"
ARTIST_RELATED = "artistrel0000000000000"


def spotify_track(track_id, name, artist_id, *, track_number=1, duration_ms=180000):
    return {
        "id": track_id,
        "name": name,
        "artists": [{"id": artist_id, "name": "Artist"}],
        "disc_number": 1,
        "track_number": track_number,
        "duration_ms": duration_ms,
    }


class AlbumMatchWritesArtistIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        self.plugin.config.data["min_track_artist_score"] = 0.55

    def test_album_match_stores_artist_id_on_album_and_items(self):
        item1 = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                         track=1, disc=1, length=180.0)
        item2 = FakeItem("Track 2", artist="Artist", albumartist="Artist",
                         track=2, disc=1, length=190.0)
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        tracks = [
            spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1, track_number=1),
            spotify_track(TRACK_ID_2, "Track 2", ARTIST_TRACK_2,
                          track_number=2, duration_ms=190000),
        ]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)
        self.assertEqual(item1.get("spotify_track_id"), TRACK_ID_1)
        self.assertEqual(item1.get("spotify_artist_id"), ARTIST_TRACK_1)
        self.assertEqual(item2.get("spotify_track_id"), TRACK_ID_2)
        self.assertEqual(item2.get("spotify_artist_id"), ARTIST_TRACK_2)

    def test_album_match_dry_run_stores_no_artist_id(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                        track=1, disc=1, length=180.0)
        album = FakeAlbum("Album", "Artist", items=[item])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        tracks = [spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1)]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                self.plugin._process_single_album(
                    album, dry_run=True, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertNotIn("spotify_artist_id", album)
        self.assertNotIn("spotify_artist_id", item)

    def test_provided_album_id_stores_artist_ids(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                        track=1, disc=1, length=180.0)
        album = FakeAlbum("Album", "Artist", items=[item])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        tracks = [spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1)]
        candidate = {
            "score": 0.9, "album": spotify_album, "track_count": 1,
            "is_variant": False, "popularity": 50,
            "base_title_score": 0.9, "artist_score": 1.0, "tracks": tracks,
        }

        with mock.patch.object(self.plugin.matcher, "build_candidate_from_album_id",
                               return_value=candidate):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                self.plugin._apply_provided_album_id(
                    album, ALBUM_ID, dry_run=False, force=False,
                )

        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)
        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertEqual(item.get("spotify_artist_id"), ARTIST_TRACK_1)

    def test_related_release_promotion_stores_album_artist_id(self):
        item = FakeItem("Bonus 1", artist="Artist", albumartist="Artist", track=11, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        related_candidates = [{
            "album": {
                "id": RELATED_ALBUM_ID, "name": "Album (Deluxe)",
                "artists": [{"id": ARTIST_RELATED, "name": "Artist"}],
            },
            "tracks": [spotify_track(TRACK_ID_2, "Bonus 1", ARTIST_TRACK_2)],
            "score": 0.9, "popularity": 20,
            "artist_score": 1.0, "base_title_score": 0.95,
        }]

        with mock.patch.object(self.plugin.matcher, "get_related_release_candidates",
                               return_value=related_candidates):
            with mock.patch.object(self.plugin.matcher, "match_items_to_tracks",
                                   return_value=[]):
                remaining = self.plugin.repairer.repair_from_related_releases(
                    album, [item], dry_run=False,
                )

        self.assertEqual(remaining, [])
        self.assertEqual(album.get("spotify_album_id"), RELATED_ALBUM_ID)
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_RELATED)

    def test_fallback_track_search_stores_item_and_album_artist_ids(self):
        item = FakeItem("Song A", artist="Artist", albumartist="Artist")
        album = FakeAlbum("Album", "Artist", items=[item])
        match = {
            "id": TRACK_ID_1,
            "artists": [{"id": ARTIST_TRACK_1, "name": "Artist"}],
            "album": {
                "id": CONSENSUS_ALBUM_ID, "name": "Album",
                "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
            },
        }

        with mock.patch.object(self.plugin.matcher, "search_spotify_track",
                               return_value=match):
            self.plugin.repairer.fallback_track_search(
                album, [item], dry_run=False, overwrite=True,
            )

        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertEqual(item.get("spotify_artist_id"), ARTIST_TRACK_1)
        self.assertEqual(album.get("spotify_album_id"), CONSENSUS_ALBUM_ID)
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)

    def test_missing_or_malformed_artists_stores_ids_without_artist_id(self):
        item1 = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                         track=1, disc=1, length=180.0)
        item2 = FakeItem("Track 2", artist="Artist", albumartist="Artist",
                         track=2, disc=1, length=190.0)
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        # Album object has an empty artist list.
        spotify_album = {"id": ALBUM_ID, "name": "Album", "artists": []}
        tracks = [
            # Track 1 carries a malformed artist ID; track 2 has no 'id' key.
            {
                "id": TRACK_ID_1, "name": "Track 1",
                "artists": [{"id": "not-a-spotify-id", "name": "Artist"}],
                "disc_number": 1, "track_number": 1, "duration_ms": 180000,
            },
            {
                "id": TRACK_ID_2, "name": "Track 2",
                "artists": [{"name": "Artist"}],
                "disc_number": 1, "track_number": 2, "duration_ms": 190000,
            },
        ]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertNotIn("spotify_artist_id", album)
        self.assertEqual(item1.get("spotify_track_id"), TRACK_ID_1)
        self.assertNotIn("spotify_artist_id", item1)
        self.assertEqual(item2.get("spotify_track_id"), TRACK_ID_2)
        self.assertNotIn("spotify_artist_id", item2)


class PrimaryArtistIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()
        cls.helpers = __import__("beetsplug.spotify_album_match.helpers", fromlist=["x"])

    def test_returns_cleaned_primary_artist_id(self):
        obj = {"artists": [{"id": ARTIST_ALBUM}, {"id": ARTIST_TRACK_1}]}
        self.assertEqual(self.helpers.primary_artist_id(obj), ARTIST_ALBUM)

    def test_returns_none_for_unusable_shapes(self):
        for obj in (None, {}, {"artists": None}, {"artists": []},
                    {"artists": ["not a dict"]}, {"artists": [{"name": "No ID"}]},
                    {"artists": [{"id": "bad"}]}):
            self.assertIsNone(self.helpers.primary_artist_id(obj), obj)


class ClearArtistIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_clear_all_ids_removes_artist_ids(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ARTIST_ALBUM

        self.plugin.clear_all_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        self.assertNotIn("spotify_artist_id", album)
        self.assertNotIn("spotify_track_id", item)
        self.assertNotIn("spotify_artist_id", item)

    def test_clear_all_ids_dry_run_keeps_artist_ids(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ARTIST_ALBUM

        self.plugin.clear_all_ids(album, dry_run=True)

        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)
        self.assertEqual(item.get("spotify_artist_id"), ARTIST_TRACK_1)

    def test_clear_track_ids_removes_artist_id(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = True
        item = FakeItem("Bonus Track", track=99, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1

        self.plugin.clear_track_ids([item], dry_run=False)

        self.assertNotIn("spotify_track_id", item)
        self.assertNotIn("spotify_artist_id", item)

    def test_clear_malformed_stored_ids_removes_malformed_artist_id(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = "bad"
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ""

        self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        # Valid album/track IDs survive; only the malformed artist IDs go.
        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertNotIn("spotify_artist_id", album)
        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertNotIn("spotify_artist_id", item)

    def test_clear_malformed_stored_ids_removes_artist_id_with_its_owner_id(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = "bad"
        item["spotify_artist_id"] = ARTIST_TRACK_1
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = "bad"
        album["spotify_artist_id"] = ARTIST_ALBUM

        self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        self.assertNotIn("spotify_artist_id", album)
        self.assertNotIn("spotify_track_id", item)
        self.assertNotIn("spotify_artist_id", item)


class BackfillArtistIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        # Take the "all tracks matched" early-return path so nothing re-matches.
        self.plugin.config.data["verify_existing_ids"] = False
        self.spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        self.album_tracks = [
            spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1, track_number=1),
            spotify_track(TRACK_ID_2, "Track 2", ARTIST_TRACK_2,
                          track_number=2, duration_ms=190000),
        ]

    def _patched_client(self):
        """Return (context managers, call log) for the three client lookups."""
        calls = []

        def get_album(album_id):
            calls.append(("get_album", album_id))
            return self.spotify_album

        def get_album_tracks(album_id):
            calls.append(("get_album_tracks", album_id))
            return self.album_tracks

        def get_track(track_id):
            calls.append(("get_track", track_id))
            return spotify_track(track_id, "Outside Track", ARTIST_TRACK_2)

        patches = [
            mock.patch.object(self.plugin.client, "get_album", side_effect=get_album),
            mock.patch.object(self.plugin.client, "get_album_tracks",
                              side_effect=get_album_tracks),
            mock.patch.object(self.plugin.client, "get_track", side_effect=get_track),
        ]
        return patches, calls

    def _album_with_ids(self, *, artist_ids=False):
        item1 = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                         track=1, disc=1, length=180.0)
        item2 = FakeItem("Track 2", artist="Artist", albumartist="Artist",
                         track=2, disc=1, length=190.0)
        item1["spotify_track_id"] = TRACK_ID_1
        item2["spotify_track_id"] = TRACK_ID_2
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        album["spotify_album_id"] = ALBUM_ID
        if artist_ids:
            album["spotify_artist_id"] = ARTIST_ALBUM
            item1["spotify_artist_id"] = ARTIST_TRACK_1
            item2["spotify_artist_id"] = ARTIST_TRACK_2
        return album, item1, item2

    def test_backfill_fills_artist_ids_without_rematching(self):
        album, item1, item2 = self._album_with_ids()
        patches, calls = self._patched_client()

        with patches[0], patches[1], patches[2]:
            with mock.patch.object(self.plugin.client, "search") as search_mock:
                with mock.patch.object(self.plugin.matcher,
                                       "find_best_album_match") as find_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False,
                        provided_album_id=None,
                    )

        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)
        self.assertEqual(item1.get("spotify_artist_id"), ARTIST_TRACK_1)
        self.assertEqual(item2.get("spotify_artist_id"), ARTIST_TRACK_2)
        search_mock.assert_not_called()
        find_mock.assert_not_called()
        # One album-details lookup plus one album-tracks lookup, nothing else.
        self.assertEqual(calls, [
            ("get_album", ALBUM_ID),
            ("get_album_tracks", ALBUM_ID),
        ])

    def test_backfill_is_a_no_op_with_zero_api_calls_when_fully_populated(self):
        album, _item1, _item2 = self._album_with_ids(artist_ids=True)
        patches, calls = self._patched_client()

        with patches[0], patches[1], patches[2]:
            with mock.patch.object(self.plugin.client, "search") as search_mock:
                with mock.patch.object(self.plugin.matcher,
                                       "find_best_album_match") as find_mock:
                    self.plugin._process_single_album(
                        album, dry_run=False, interactive=False, force=False,
                        provided_album_id=None,
                    )

        self.assertEqual(calls, [])
        search_mock.assert_not_called()
        find_mock.assert_not_called()

    def test_backfill_dry_run_stores_nothing(self):
        album, item1, item2 = self._album_with_ids()
        patches, _calls = self._patched_client()

        with patches[0], patches[1], patches[2]:
            with mock.patch.object(self.plugin.matcher, "find_best_album_match"):
                self.plugin._process_single_album(
                    album, dry_run=True, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertNotIn("spotify_artist_id", album)
        self.assertNotIn("spotify_artist_id", item1)
        self.assertNotIn("spotify_artist_id", item2)
        self.assertEqual(album.store_calls, 0)
        self.assertEqual(item1.store_calls, 0)
        self.assertEqual(item2.store_calls, 0)

    def test_backfill_falls_back_to_get_track_for_track_outside_the_album(self):
        album, item1, item2 = self._album_with_ids()
        # item2 was matched from a related release, so it is not on this album.
        item2["spotify_track_id"] = OUTSIDE_TRACK_ID
        patches, calls = self._patched_client()

        with patches[0], patches[1], patches[2]:
            with mock.patch.object(self.plugin.matcher, "find_best_album_match"):
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertEqual(item1.get("spotify_artist_id"), ARTIST_TRACK_1)
        self.assertEqual(item2.get("spotify_artist_id"), ARTIST_TRACK_2)
        self.assertIn(("get_track", OUTSIDE_TRACK_ID), calls)
        self.assertEqual(len([c for c in calls if c[0] == "get_track"]), 1)


class ArtistIdFieldRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_album_fields_declares_spotify_artist_id(self):
        self.assertIn("spotify_artist_id", self.plugin.album_fields())

    def test_item_fields_declares_spotify_artist_id(self):
        self.assertIn("spotify_artist_id", self.plugin.item_fields())


if __name__ == "__main__":
    unittest.main()
