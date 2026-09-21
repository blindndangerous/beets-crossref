"""Tests for Spotify ID validation: malformed IDs never reach the API or the DB."""
import unittest
from unittest import mock

from fakes import FakeAlbum, FakeItem
from plugin_test_utils import fresh_plugin

from beetsplug.spotify_album_match.helpers import clean_spotify_id

VALID_ALBUM_ID = "1A2b3C4d5E6f7G8h9I0jKl"
VALID_TRACK_ID = "9lKj0I9h8G7f6E5d4C3b2A"


def fake_spotify():
    """A spotipy stand-in that echoes the IDs it is given and records every call."""
    return mock.MagicMock(
        album_tracks=mock.MagicMock(return_value={"items": [], "next": None}),
        album=mock.MagicMock(side_effect=lambda album_id: {"id": album_id}),
        albums=mock.MagicMock(
            side_effect=lambda album_ids: {"albums": [{"id": aid} for aid in album_ids]},
        ),
    )


class SpotifyIdSafetyTest(unittest.TestCase):
    def setUp(self):
        self.plugin = fresh_plugin()
        self.client = self.plugin.client
        self.client._spotify = fake_spotify()
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
        self.client._spotify.albums.assert_called_once_with([VALID_ALBUM_ID])

    def test_client_skips_blank_or_malformed_single_id_lookups(self):
        self.assertEqual(self.client.get_album_tracks(""), [])
        self.assertIsNone(self.client.get_album("bad"))

        self.client._spotify.album_tracks.assert_not_called()
        self.client._spotify.album.assert_not_called()

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
