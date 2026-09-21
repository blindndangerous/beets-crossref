"""Tests for repair.py: AlbumRepairer evaluation and repair strategies."""
from unittest import mock

from beets import plugins
from beets.library import Album
from beets.test.helper import PluginTestCase

from beetsplug.spotify_album_match.repair import AlbumRepairer


class SpotifyPluginTestCase(PluginTestCase):
    """See test_plugin.py for why the plugin is loaded rather than constructed."""

    plugin = "spotify_album_match"

    def setUp(self):
        super().setUp()
        self.plugin = next(
            p for p in plugins.find_plugins() if p.name == "spotify_album_match"
        )

    def add_tracked_album(self, album_name, albumartist, tracks):
        """Add an album whose items carry the flexible fields in *tracks*.

        Each entry in *tracks* is a dict of item fields; a "flex" key holds the
        flexible fields to store on that item. Returns (album, items).
        """
        items = []
        for fields in tracks:
            flex = fields.pop("flex", {})
            item = self.add_item(album=album_name, albumartist=albumartist, **fields)
            for key, value in flex.items():
                item[key] = value
            item.store()
            items.append(item)
        return self.lib.add_album(items), items


class GetRepairStrategyTests(SpotifyPluginTestCase):
    def test_unknown_falls_back_to_related_release(self):
        self.config["spotify_album_match"]["existing_album_repair_strategy"] = "nonsense"
        self.assertEqual(self.plugin.repairer.get_repair_strategy(), "related_release")

    def test_valid_values(self):
        for val in ("strict", "related_release"):
            self.config["spotify_album_match"]["existing_album_repair_strategy"] = val
            self.assertEqual(self.plugin.repairer.get_repair_strategy(), val)


class EvaluateExistingTrackIdsTests(SpotifyPluginTestCase):
    def _eval(self, album, tracks):
        return AlbumRepairer.evaluate_existing_track_ids(album, tracks)

    def test_all_match(self):
        album, items = self.add_tracked_album("X", "Y", [
            {"title": "A", "track": 1, "disc": 1, "flex": {"spotify_track_id": "t1"}},
            {"title": "B", "track": 2, "disc": 1, "flex": {"spotify_track_id": "t2"}},
        ])
        tracks = [
            {"id": "t1", "disc_number": 1, "track_number": 1},
            {"id": "t2", "disc_number": 1, "track_number": 2},
        ]
        mismatched, missing, matched, total = self._eval(album, tracks)
        self.assertEqual(mismatched, [])
        self.assertEqual(missing, [])
        self.assertEqual(matched, 2)
        self.assertEqual(total, 2)

    def test_wrong_disc_is_mismatched(self):
        album, (item,) = self.add_tracked_album("X", "Y", [
            {"title": "A", "track": 1, "disc": 1, "flex": {"spotify_track_id": "t1"}},
        ])
        tracks = [{"id": "t1", "disc_number": 2, "track_number": 1}]
        mismatched, _missing, matched, _total = self._eval(album, tracks)
        self.assertEqual([i.id for i in mismatched], [item.id])
        self.assertEqual(matched, 0)

    def test_missing_id_goes_to_missing_not_mismatched(self):
        album, (item,) = self.add_tracked_album("X", "Y", [
            {"title": "A", "track": 1, "disc": 1},
        ])
        tracks = [{"id": "t1", "disc_number": 1, "track_number": 1}]
        mismatched, missing, _matched, total = self._eval(album, tracks)
        self.assertEqual(mismatched, [])
        self.assertEqual([i.id for i in missing], [item.id])
        self.assertEqual(total, 0)

    def test_duration_mismatch_does_not_flag(self):
        # Duration differences between local files and Spotify are normal
        # and do NOT indicate a wrong track ID — only disc/track mismatches do.
        album, _items = self.add_tracked_album("X", "Y", [
            {
                "title": "A", "track": 1, "disc": 1, "length": 200.0,
                "flex": {"spotify_track_id": "t1"},
            },
        ])
        tracks = [{
            "id": "t1", "disc_number": 1, "track_number": 1,
            "external_ids": {}, "duration_ms": 240000,
        }]
        mismatched, _missing, matched, _total = self._eval(album, tracks)
        self.assertEqual(mismatched, [])
        self.assertEqual(matched, 1)


class RepairFromRelatedReleasesTests(SpotifyPluginTestCase):
    def setUp(self):
        super().setUp()
        self.repairer = self.plugin.repairer

    def test_promotes_album_and_does_not_remap_full_album(self):
        album, items = self.add_tracked_album("Album", "Artist", [
            {"title": "Bonus 1", "artist": "Artist", "track": 11, "disc": 1},
            {"title": "Bonus 2", "artist": "Artist", "track": 12, "disc": 1},
        ])
        album["spotify_album_id"] = "base_album"
        album.store(inherit=False)

        related_candidates = [{
            "album": {"id": "deluxe_album", "name": "Album (Deluxe)"},
            "tracks": [{"id": "t1"}, {"id": "t2"}],
            "score": 0.9, "popularity": 20,
            "artist_score": 1.0, "base_title_score": 0.95,
        }]

        with mock.patch.object(self.plugin.matcher, "get_related_release_candidates",
                               return_value=related_candidates):
            with mock.patch.object(self.plugin.matcher, "match_items_to_tracks",
                                   return_value=[]) as match_tracks_mock:
                # If the test code accidentally re-runs authoritative mapping, this would fire.
                with mock.patch.object(
                    self.plugin, "_apply_authoritative_album_mapping",
                ) as authoritative_mock:
                    # beets Models route attribute assignment into their field
                    # storage, so store() can only be spied on via the class.
                    with mock.patch.object(
                        Album, "store", autospec=True, side_effect=Album.store,
                    ) as store_mock:
                        remaining = self.repairer.repair_from_related_releases(
                            album, items, dry_run=False,
                        )

        self.assertEqual(remaining, [])
        self.assertEqual(album.get("spotify_album_id"), "deluxe_album")
        store_mock.assert_called_once_with(album, inherit=False)
        self.assertTrue(match_tracks_mock.called)
        # Promotion must NOT trigger a re-mapping over the whole album — that
        # would overwrite already-correct IDs from the primary release.
        authoritative_mock.assert_not_called()
