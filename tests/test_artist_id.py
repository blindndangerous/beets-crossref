"""Tests for spotify_artist_id: writes at every match site, and clearing."""
import unittest
from unittest import mock

from fakes import FakeAlbum, FakeItem
from plugin_test_utils import fresh_plugin

from beetsplug.spotify_album_match import helpers
from beetsplug.spotify_album_match.helpers import own_artist_id

ALBUM_ID = "albumid000000000000000"
TRACK_ID_1 = "track10000000000000000"
TRACK_ID_2 = "track20000000000000000"
OUTSIDE_TRACK_ID = "outsidetrack0000000000"
RELATED_ALBUM_ID = "relalbum00000000000000"
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

    def test_album_store_never_overwrites_per_track_artist_ids(self):
        """A compilation keeps each track's own artist ID when the album is stored.

        Real beets `Album.store()` defaults to `inherit=True` and pushes every
        dirty flexible value into each item (beets 2.2.0 library.py:1494-1527),
        so storing the album's `spotify_artist_id` would replace all three
        tracks' own artist IDs with the compilation's.
        """
        items = [
            FakeItem(f"Track {n}", artist="Artist", albumartist="Various Artists",
                     track=n, disc=1)
            for n in (1, 2, 3)
        ]
        track_artist_ids = [ARTIST_TRACK_1, ARTIST_TRACK_2, ARTIST_RELATED]
        for item, artist_id in zip(items, track_artist_ids, strict=True):
            item["spotify_track_id"] = TRACK_ID_1
            item["spotify_artist_id"] = artist_id
            item.store()
        album = FakeAlbum("Compilation", "Various Artists", items=items)
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ARTIST_ALBUM

        related_candidates = [{
            "album": {
                "id": RELATED_ALBUM_ID, "name": "Compilation (Deluxe)",
                "artists": [{"id": ARTIST_RELATED, "name": "Various Artists"}],
            },
            "tracks": [spotify_track(TRACK_ID_2, "Track 1", ARTIST_TRACK_2)],
            "score": 0.9, "popularity": 20,
            "artist_score": 1.0, "base_title_score": 0.95,
        }]

        with mock.patch.object(self.plugin.matcher, "match_items_to_tracks",
                               return_value=[]):
            self.plugin.repairer.repair_from_related_releases(
                album, list(items), dry_run=False,
                related_candidates=related_candidates,
            )

        self.assertEqual(album.get("spotify_album_id"), RELATED_ALBUM_ID)
        self.assertEqual(
            [own_artist_id(item) for item in items], track_artist_ids,
        )
        # Items must not acquire the album's ID as a value of their own either.
        self.assertEqual(
            [item._values_flex.get("spotify_album_id") for item in items],
            [None, None, None],
        )

    def test_album_id_clearing_does_not_delete_item_artist_ids(self):
        """Clearing the album's IDs leaves each track's own artist ID alone.

        beets cascades *deletes* as well as writes when `inherit` is on
        (library.py:1520-1527), which would strip `spotify_artist_id` from
        every item while leaving their still-valid track IDs behind.
        """
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1
        item.store()
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = "not-a-spotify-id"
        album["spotify_artist_id"] = ARTIST_ALBUM
        album.store(inherit=False)

        self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album._values_flex)
        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertEqual(own_artist_id(item), ARTIST_TRACK_1)

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


class AlbumStoreInheritGuardTests(unittest.TestCase):
    """One test per album-store site, each failing if `inherit=False` is dropped.

    beets 2.2.0 `Album.store()` defaults to inherit=True and writes every dirty
    flexible value into each item, cascading deletes the same way
    (library.py:1494-1527). Every site below is a place where that would either
    hand an item an ID that is not its own, or delete one that is.
    """

    def setUp(self):
        self.plugin = fresh_plugin()
        self.plugin.config.data["min_track_artist_score"] = 0.55

    @staticmethod
    def _own(item, field):
        return item._values_flex.get(field)

    def test_main_match_store_leaves_items_without_album_id_or_album_artist(self):
        """plugin.py: the album store in _process_single_album."""
        matched = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                           track=1, disc=1, length=180.0)
        unmatched = FakeItem("Not On Spotify", artist="Artist", albumartist="Artist",
                             track=2, disc=1, length=190.0)
        album = FakeAlbum("Album", "Artist", items=[matched, unmatched])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        tracks = [spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1)]

        with mock.patch.object(
            self.plugin.matcher, "find_best_album_match", return_value=(spotify_album, []),
        ), mock.patch.object(
            self.plugin.client, "get_album_tracks", return_value=tracks,
        ), mock.patch.object(
            self.plugin.repairer, "repair_from_related_releases", return_value=[unmatched],
        ):
            self.plugin._process_single_album(
                album, dry_run=False, interactive=False, force=False,
            )

        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertEqual(own_artist_id(album), ARTIST_ALBUM)
        # The matched track owns its own track artist, not the album's.
        self.assertEqual(own_artist_id(matched), ARTIST_TRACK_1)
        # The unmatched track owns nothing at all: no album ID copy, and no
        # artist ID with no track ID beside it.
        self.assertIsNone(own_artist_id(unmatched))
        self.assertIsNone(self._own(unmatched, "spotify_track_id"))
        for item in (matched, unmatched):
            self.assertIsNone(self._own(item, "spotify_album_id"))

    def test_provided_album_id_store_leaves_items_without_album_id_or_album_artist(self):
        """plugin.py: the album store in _apply_provided_album_id (--sid)."""
        matched = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                           track=1, disc=1, length=180.0)
        unmatched = FakeItem("Not On Spotify", artist="Artist", albumartist="Artist",
                             track=2, disc=1, length=190.0)
        album = FakeAlbum("Album", "Artist", items=[matched, unmatched])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        tracks = [spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1)]
        candidate = {
            "album": spotify_album, "tracks": tracks, "score": 0.95,
            "track_count": 1, "is_variant": False, "popularity": 10,
            "base_title_score": 1.0, "artist_score": 1.0,
        }

        with mock.patch.object(
            self.plugin.matcher, "build_candidate_from_album_id", return_value=candidate,
        ), mock.patch.object(
            self.plugin.client, "get_album_tracks", return_value=tracks,
        ):
            self.plugin._apply_provided_album_id(album, ALBUM_ID, dry_run=False, force=True)

        self.assertEqual(own_artist_id(matched), ARTIST_TRACK_1)
        self.assertIsNone(own_artist_id(unmatched))
        for item in (matched, unmatched):
            self.assertIsNone(self._own(item, "spotify_album_id"))

    def test_clear_all_ids_writes_each_item_once_and_leaves_untagged_items_alone(self):
        """plugin.py: the album store in clear_all_ids.

        The album's deleted IDs must not cascade: an item is written by the
        item loop below, once, and an item with nothing stored is not written
        at all.
        """
        tagged = FakeItem("Track 1", artist="Artist", albumartist="Artist", track=1, disc=1)
        tagged["spotify_track_id"] = TRACK_ID_1
        tagged["spotify_artist_id"] = ARTIST_TRACK_1
        untagged = FakeItem("Track 2", artist="Artist", albumartist="Artist", track=2, disc=1)
        album = FakeAlbum("Album", "Artist", items=[tagged, untagged])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ARTIST_ALBUM
        album.store(inherit=False)

        self.plugin.clear_all_ids(album, dry_run=False)

        self.assertNotIn("spotify_album_id", album._values_flex)
        self.assertIsNone(self._own(tagged, "spotify_track_id"))
        self.assertIsNone(own_artist_id(tagged))
        self.assertEqual(tagged.store_calls, 1)
        self.assertEqual(untagged.store_calls, 0)

    def test_clearing_a_malformed_album_artist_id_keeps_item_artist_ids(self):
        """plugin.py: the album store in _clear_malformed_artist_id.

        Deleting the album's own bad artist ID would otherwise delete the
        artist ID from every item, leaving their still-valid track IDs behind
        with nothing beside them.
        """
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = "not-a-spotify-id"
        album.store(inherit=False)

        album_id = self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        self.assertEqual(album_id, ALBUM_ID)
        self.assertNotIn("spotify_artist_id", album._values_flex)
        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertEqual(own_artist_id(item), ARTIST_TRACK_1)
        self.assertEqual(item.store_calls, 0)


class StaleArtistIdTests(unittest.TestCase):
    """A stored artist ID must always belong to the currently stored album/track ID."""

    def setUp(self):
        self.plugin = fresh_plugin()
        self.plugin.config.data["min_track_artist_score"] = 0.55

    def test_album_rematched_without_artists_drops_stale_album_artist_id(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                        track=1, disc=1, length=180.0)
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = RELATED_ALBUM_ID
        album["spotify_artist_id"] = ARTIST_RELATED
        self.plugin.config.data["verify_existing_ids"] = False
        # The new match carries no 'artists' at all.
        spotify_album = {"id": ALBUM_ID, "name": "Album"}
        tracks = [spotify_track(TRACK_ID_1, "Track 1", ARTIST_TRACK_1)]

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

    def test_track_rematched_without_artist_id_drops_stale_item_artist_id(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                        track=1, disc=1, length=180.0)
        item["spotify_track_id"] = OUTSIDE_TRACK_ID
        item["spotify_artist_id"] = ARTIST_TRACK_2
        album = FakeAlbum("Album", "Artist", items=[item])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        # The newly matched track names an artist but exposes no usable ID.
        tracks = [{
            "id": TRACK_ID_1, "name": "Track 1",
            "artists": [{"name": "Artist"}],
            "disc_number": 1, "track_number": 1, "duration_ms": 180000,
        }]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        # The album carries an artist ID, which a real Item reads through the
        # album fallback, so assert on the item's own value.
        self.assertIsNone(own_artist_id(item))

    def test_dry_run_keeps_stale_artist_ids_and_stores_nothing(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                        track=1, disc=1, length=180.0)
        item["spotify_track_id"] = OUTSIDE_TRACK_ID
        item["spotify_artist_id"] = ARTIST_TRACK_2
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = RELATED_ALBUM_ID
        album["spotify_artist_id"] = ARTIST_RELATED
        # Neither the new album nor the new track exposes a usable artist ID.
        spotify_album = {"id": ALBUM_ID, "name": "Album"}
        tracks = [{
            "id": TRACK_ID_1, "name": "Track 1",
            "artists": [{"name": "Artist"}],
            "disc_number": 1, "track_number": 1, "duration_ms": 180000,
        }]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                # force=True so the run reaches the match path instead of the
                # "all tracks matched" early return.
                self.plugin._process_single_album(
                    album, dry_run=True, interactive=False, force=True,
                    provided_album_id=None,
                )

        self.assertEqual(album.get("spotify_artist_id"), ARTIST_RELATED)
        self.assertEqual(item.get("spotify_artist_id"), ARTIST_TRACK_2)
        self.assertEqual(album.store_calls, 0)
        self.assertEqual(item.store_calls, 0)


class PrimaryArtistIdTests(unittest.TestCase):
    def test_returns_cleaned_primary_artist_id(self):
        obj = {"artists": [{"id": ARTIST_ALBUM}, {"id": ARTIST_TRACK_1}]}
        self.assertEqual(helpers.primary_artist_id(obj), ARTIST_ALBUM)

    def test_returns_none_for_unusable_shapes(self):
        for obj in (None, {}, {"artists": None}, {"artists": []},
                    {"artists": ["not a dict"]}, {"artists": [{"name": "No ID"}]},
                    {"artists": [{"id": "bad"}]}):
            self.assertIsNone(helpers.primary_artist_id(obj), obj)


class OwnArtistIdTests(unittest.TestCase):
    """own_artist_id reads the object's own storage, never the album fallback."""

    def test_reads_the_items_own_value_not_the_albums(self):
        item = FakeItem("Track 1")
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_artist_id"] = ARTIST_ALBUM

        # The fallback is real: the item reads the album's value.
        self.assertEqual(item.get("spotify_artist_id"), ARTIST_ALBUM)
        self.assertIn("spotify_artist_id", item)
        # The helper is not fooled by it.
        self.assertIsNone(helpers.own_artist_id(item))
        self.assertEqual(helpers.own_artist_id(album), ARTIST_ALBUM)

        item["spotify_artist_id"] = ARTIST_TRACK_1
        self.assertEqual(helpers.own_artist_id(item), ARTIST_TRACK_1)

    def test_discard_tolerates_a_value_that_belongs_to_the_album(self):
        item = FakeItem("Track 1")
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_artist_id"] = ARTIST_ALBUM

        helpers.discard_artist_id(item)

        self.assertIsNone(helpers.own_artist_id(item))
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)


class ClearArtistIdTests(unittest.TestCase):
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
        self.assertGreater(album.store_calls, 0)
        self.assertEqual(item.store_calls, 1)

    def test_clear_all_ids_dry_run_keeps_artist_ids(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ARTIST_ALBUM

        self.plugin.clear_all_ids(album, dry_run=True)

        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)
        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertEqual(item.get("spotify_artist_id"), ARTIST_TRACK_1)
        self.assertEqual(album.store_calls, 0)
        self.assertEqual(item.store_calls, 0)

    def test_clear_track_ids_removes_artist_id(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = True
        item = FakeItem("Bonus Track", track=99, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        item["spotify_artist_id"] = ARTIST_TRACK_1

        self.plugin.clear_track_ids([item], dry_run=False)

        self.assertNotIn("spotify_track_id", item)
        self.assertNotIn("spotify_artist_id", item)
        self.assertEqual(item.store_calls, 1)

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


class AlbumFallbackTests(unittest.TestCase):
    """An item with no artist ID of its own reads its album's through get/in.

    Nothing may crash on that, and nothing may treat the album's value as if
    it belonged to the item.
    """

    def setUp(self):
        self.plugin = fresh_plugin()
        self.plugin.config.data["min_track_artist_score"] = 0.55

    def _album_with_artist_id(self, item):
        album = FakeAlbum("Album", "Artist", items=[item])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ARTIST_ALBUM
        return album

    def test_clear_track_ids_on_item_without_own_artist_id(self):
        self.plugin.config.data["clear_unmatched_track_ids"] = True
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = TRACK_ID_1
        album = self._album_with_artist_id(item)

        self.plugin.clear_track_ids([item], dry_run=False)

        self.assertNotIn("spotify_track_id", item)
        self.assertIsNone(own_artist_id(item))
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)

    def test_clear_malformed_stored_ids_on_item_without_own_artist_id(self):
        item = FakeItem("Track 1", track=1, disc=1)
        item["spotify_track_id"] = "bad"
        album = self._album_with_artist_id(item)

        self.plugin._clear_malformed_stored_ids(album, dry_run=False)

        self.assertNotIn("spotify_track_id", item)
        self.assertIsNone(own_artist_id(item))
        self.assertEqual(album.get("spotify_album_id"), ALBUM_ID)
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)

    def test_track_write_without_artist_on_album_that_has_one(self):
        item = FakeItem("Track 1", artist="Artist", albumartist="Artist",
                        track=1, disc=1, length=180.0)
        album = FakeAlbum("Album", "Artist", items=[item])
        spotify_album = {
            "id": ALBUM_ID, "name": "Album",
            "artists": [{"id": ARTIST_ALBUM, "name": "Artist"}],
        }
        # The matched track names an artist but exposes no usable ID.
        tracks = [{
            "id": TRACK_ID_1, "name": "Track 1",
            "artists": [{"name": "Artist"}],
            "disc_number": 1, "track_number": 1, "duration_ms": 180000,
        }]

        with mock.patch.object(self.plugin.matcher, "find_best_album_match",
                               return_value=(spotify_album, [])):
            with mock.patch.object(self.plugin.client, "get_album_tracks",
                                   return_value=tracks):
                self.plugin._process_single_album(
                    album, dry_run=False, interactive=False, force=False,
                    provided_album_id=None,
                )

        self.assertEqual(item.get("spotify_track_id"), TRACK_ID_1)
        self.assertIsNone(own_artist_id(item))
        self.assertEqual(album.get("spotify_artist_id"), ARTIST_ALBUM)

    def test_dry_run_malformed_album_artist_id_warns_once_not_per_item(self):
        item1 = FakeItem("Track 1", track=1, disc=1)
        item2 = FakeItem("Track 2", track=2, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        album["spotify_album_id"] = ALBUM_ID
        album["spotify_artist_id"] = ""

        with self.assertLogs("beets.spotify_album_match", level="WARNING") as captured:
            self.plugin._clear_malformed_stored_ids(album, dry_run=True)

        warnings = [
            record.getMessage() for record in captured.records
            if "Spotify artist ID" in record.getMessage()
        ]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("'Album'", warnings[0])
