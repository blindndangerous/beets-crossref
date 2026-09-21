"""Tests for repair.py: AlbumRepairer evaluation, strategies, fallback consensus."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fakes import FakeAlbum, FakeItem
from plugin_test_utils import fresh_plugin, load_package


class GetRepairStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()

    def test_unknown_falls_back_to_related_release(self):
        self.plugin.config.data["existing_album_repair_strategy"] = "nonsense"
        self.assertEqual(self.plugin.repairer.get_repair_strategy(), "related_release")

    def test_valid_values(self):
        for val in ("strict", "related_release"):
            self.plugin.config.data["existing_album_repair_strategy"] = val
            self.assertEqual(self.plugin.repairer.get_repair_strategy(), val)


class EvaluateExistingTrackIdsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()
        cls.repair_module = __import__("beetsplug.spotify_album_match.repair", fromlist=["x"])

    def _eval(self, album, tracks):
        return self.repair_module.AlbumRepairer.evaluate_existing_track_ids(album, tracks)

    def test_all_match(self):
        item1 = FakeItem("A", track=1, disc=1)
        item1["spotify_track_id"] = "t1"
        item2 = FakeItem("B", track=2, disc=1)
        item2["spotify_track_id"] = "t2"
        album = FakeAlbum("X", "Y", items=[item1, item2])
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
        item = FakeItem("A", track=1, disc=1)
        item["spotify_track_id"] = "t1"
        album = FakeAlbum("X", "Y", items=[item])
        tracks = [{"id": "t1", "disc_number": 2, "track_number": 1}]
        mismatched, _missing, matched, _total = self._eval(album, tracks)
        self.assertIn(item, mismatched)
        self.assertEqual(matched, 0)

    def test_missing_id_goes_to_missing_not_mismatched(self):
        item = FakeItem("A", track=1, disc=1)
        album = FakeAlbum("X", "Y", items=[item])
        tracks = [{"id": "t1", "disc_number": 1, "track_number": 1}]
        mismatched, missing, _matched, total = self._eval(album, tracks)
        self.assertEqual(mismatched, [])
        self.assertIn(item, missing)
        self.assertEqual(total, 0)

    def test_isrc_mismatch_flagged(self):
        item = FakeItem("A", track=1, disc=1, isrc="USRC11111111")
        item["spotify_track_id"] = "t1"
        album = FakeAlbum("X", "Y", items=[item])
        tracks = [{
            "id": "t1", "disc_number": 1, "track_number": 1,
            "external_ids": {"isrc": "USRC99999999"}, "duration_ms": 200000,
        }]
        mismatched, _missing, matched, _total = self._eval(album, tracks)
        self.assertIn(item, mismatched)
        self.assertEqual(matched, 0)

    def test_matching_isrc_verifies(self):
        item = FakeItem("A", track=1, disc=1, isrc="USRC11111111")
        item["spotify_track_id"] = "t1"
        album = FakeAlbum("X", "Y", items=[item])
        tracks = [{
            "id": "t1", "disc_number": 1, "track_number": 1,
            "external_ids": {"isrc": "USRC11111111"}, "duration_ms": 200000,
        }]
        mismatched, _missing, matched, _total = self._eval(album, tracks)
        self.assertEqual(mismatched, [])
        self.assertEqual(matched, 1)

    def test_duration_mismatch_does_not_flag(self):
        # Duration differences between local files and Spotify are normal
        # and do NOT indicate a wrong track ID — only disc/track/ISRC mismatches do.
        item = FakeItem("A", track=1, disc=1, length=200.0)
        item["spotify_track_id"] = "t1"
        album = FakeAlbum("X", "Y", items=[item])
        tracks = [{
            "id": "t1", "disc_number": 1, "track_number": 1,
            "external_ids": {}, "duration_ms": 240000,
        }]
        mismatched, _missing, matched, _total = self._eval(album, tracks)
        self.assertEqual(mismatched, [])
        self.assertEqual(matched, 1)


class RepairFromRelatedReleasesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        self.repairer = self.plugin.repairer

    def test_promotes_album_and_does_not_remap_full_album(self):
        item1 = FakeItem("Bonus 1", artist="Artist", albumartist="Artist", track=11, disc=1)
        item2 = FakeItem("Bonus 2", artist="Artist", albumartist="Artist", track=12, disc=1)
        album = FakeAlbum("Album", "Artist", items=[item1, item2])
        album["spotify_album_id"] = "base_album"

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
                    remaining = self.repairer.repair_from_related_releases(
                        album, [item1, item2], dry_run=False,
                    )

        self.assertEqual(remaining, [])
        self.assertEqual(album.get("spotify_album_id"), "deluxe_album")
        self.assertEqual(album.store_calls, 1)
        self.assertTrue(match_tracks_mock.called)
        # Promotion must NOT trigger a re-mapping over the whole album — that
        # would overwrite already-correct IDs from the primary release.
        authoritative_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
