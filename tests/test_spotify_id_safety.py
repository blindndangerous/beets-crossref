"""Tests for Spotify ID validation: malformed IDs never reach the API or the DB."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fakes import FakeAlbum, FakeItem
from plugin_test_utils import fresh_plugin, load_package

from beetsplug.spotify_album_match.helpers import clean_spotify_id

VALID_ALBUM_ID = "1A2b3C4d5E6f7G8h9I0jKl"
VALID_TRACK_ID = "9lKj0I9h8G7f6E5d4C3b2A"


class DummySpotify:
    """Records every call so tests can assert the API was never reached."""

    def __init__(self):
        self.album_track_calls = []
        self.album_calls = []
        self.albums_calls = []
        self.tracks_calls = []

    def album_tracks(self, album_id):
        self.album_track_calls.append(album_id)
        return {"items": [], "next": None}

    def album(self, album_id):
        self.album_calls.append(album_id)
        return {"id": album_id}

    def albums(self, album_ids):
        self.albums_calls.append(list(album_ids))
        return {"albums": [{"id": album_id} for album_id in album_ids]}

    def tracks(self, track_ids):
        self.tracks_calls.append(list(track_ids))
        return {"tracks": [{"id": track_id} for track_id in track_ids]}


class SpotifyIdSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        self.client = self.plugin.client
        self.client._spotify = DummySpotify()
        self.client.min_request_interval = 0

    def test_clean_spotify_id_accepts_only_base62_22_char_values(self):
        self.assertEqual(clean_spotify_id(VALID_TRACK_ID), VALID_TRACK_ID)
        self.assertEqual(clean_spotify_id(f" {VALID_TRACK_ID} "), VALID_TRACK_ID)
        self.assertIsNone(clean_spotify_id(""))
        self.assertIsNone(clean_spotify_id(None))
        self.assertIsNone(clean_spotify_id("bad"))
        self.assertIsNone(clean_spotify_id("1A2b3C4d5E6f7G8h9I0jK!"))

    def test_client_filters_blank_and_malformed_bulk_ids_before_spotify_calls(self):
        self.assertEqual(
            self.client.get_albums_bulk(["", "bad", VALID_ALBUM_ID, VALID_ALBUM_ID]),
            {VALID_ALBUM_ID: {"id": VALID_ALBUM_ID}},
        )
        self.assertEqual(self.client._spotify.albums_calls, [[VALID_ALBUM_ID]])

    def test_client_skips_blank_or_malformed_single_id_lookups(self):
        self.assertEqual(self.client.get_album_tracks(""), [])
        self.assertIsNone(self.client.get_album("bad"))

        self.assertEqual(self.client._spotify.album_track_calls, [])
        self.assertEqual(self.client._spotify.album_calls, [])

    def test_clear_functions_delete_flexible_fields_instead_of_blanking_them(self):
        item_with_id = FakeItem("matched")
        item_with_id["spotify_track_id"] = VALID_TRACK_ID
        item_with_blank = FakeItem("blank")
        item_with_blank["spotify_track_id"] = ""
        item_without_id = FakeItem("missing")
        album = FakeAlbum(
            "Album", "Artist",
            items=[item_with_id, item_with_blank, item_without_id],
        )
        album["spotify_album_id"] = VALID_ALBUM_ID

        self.plugin.clear_all_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        self.assertNotIn("spotify_track_id", item_with_id)
        self.assertNotIn("spotify_track_id", item_with_blank)
        self.assertNotIn("spotify_track_id", item_without_id)

    def test_malformed_stored_ids_are_deleted_before_processing_album(self):
        item = FakeItem("bad track")
        item["spotify_track_id"] = "bad"
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ""

        self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        self.assertNotIn("spotify_track_id", item)
        self.assertEqual(album.store_calls, 1)
        self.assertEqual(item.store_calls, 1)

    def test_malformed_album_id_is_not_returned_for_use_under_dry_run(self):
        """Dry-run leaves the bad ID in the library but must not act on it.

        Otherwise the dry run sends the malformed ID to verification, reports
        it unverifiable, and logs a clear-and-re-search the real run (which
        deletes the ID first and searches directly) would never perform.
        """
        album = FakeAlbum("Album", "Artist", items=[FakeItem("track")])
        album["spotify_album_id"] = "bad"
        album.store(inherit=False)

        album_id = self.plugin._clear_malformed_stored_ids(album, dry_run=True)

        self.assertIsNone(album_id)
        self.assertEqual(album.get("spotify_album_id"), "bad")
        self.assertEqual(album.store_calls, 1)


if __name__ == "__main__":
    unittest.main()
