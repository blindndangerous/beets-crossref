"""Tests for Spotify ID validation: malformed IDs never reach the API or the DB."""
from unittest import mock

from beets import plugins
from beets.library import Album, Item
from beets.test.helper import PluginTestCase

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


class SpotifyIdSafetyTest(PluginTestCase):
    """See test_plugin.py for why the plugin is loaded rather than constructed."""

    plugin = "spotify_album_match"

    def setUp(self):
        super().setUp()
        self.plugin = next(
            p for p in plugins.find_plugins() if p.name == "spotify_album_match"
        )
        self.client = self.plugin.client
        self.client._spotify = fake_spotify()
        self.client.min_request_interval = 0

    def add_album_with_items(self, album_name, albumartist, tracks):
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
        album, items = self.add_album_with_items("Album", "Artist", [
            {"title": "matched", "track": 1, "flex": {"spotify_track_id": VALID_TRACK_ID}},
            {"title": "blank", "track": 2, "flex": {"spotify_track_id": ""}},
            {"title": "missing", "track": 3},
        ])
        album["spotify_album_id"] = VALID_ALBUM_ID
        album.store(inherit=False)

        self.plugin.clear_all_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        for item in items:
            item.load()
            self.assertNotIn("spotify_track_id", item)

    def test_malformed_stored_ids_are_deleted_before_processing_album(self):
        album, (item,) = self.add_album_with_items("Album", "Artist", [
            {"title": "bad track", "track": 1, "flex": {"spotify_track_id": "bad"}},
        ])
        album["spotify_album_id"] = ""
        album.store(inherit=False)

        with mock.patch.object(Album, "store", autospec=True, side_effect=Album.store) as album_store:
            with mock.patch.object(Item, "store", autospec=True, side_effect=Item.store) as item_store:
                self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album)
        item.load()
        self.assertNotIn("spotify_track_id", item)
        album_store.assert_called_once_with(album, inherit=False)
        self.assertEqual([call.args[0].id for call in item_store.call_args_list], [item.id])
