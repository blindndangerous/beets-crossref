"""Tests for matching.py: AlbumMatcher candidate selection and track scoring."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plugin_test_utils import fresh_plugin, load_package
from fakes import FakeAlbum, FakeItem


class AlbumMatcherSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        self.matcher = self.plugin.matcher
        # Defaults sometimes need overriding for individual tests.

    def _candidate(self, **overrides):
        base = {
            "score": 0.6,
            "album": {"id": "a", "name": "A", "artists": [{"name": "Artist"}]},
            "track_count": 10,
            "is_variant": False,
            "popularity": 1,
            "base_title_score": 0.9,
            "artist_score": 0.8,
            "tracks": [],
        }
        base.update(overrides)
        return base

    def test_non_interactive_uncertain_returns_none(self):
        local_album = FakeAlbum("Local", "Artist")
        candidates = [
            self._candidate(score=0.60, album={"id": "a1", "name": "A1", "artists": [{"name": "Artist"}]}),
            self._candidate(score=0.55, album={"id": "a2", "name": "A2", "artists": [{"name": "Artist"}]}, popularity=9),
        ]
        selected, supplemental = self.matcher._select_album_candidate(
            local_album, [], candidates, interactive=False,
        )
        self.assertIsNone(selected)
        self.assertEqual(supplemental, [])

    def test_interactive_uses_prompter_choice(self):
        local_album = FakeAlbum("Local", "Artist")
        first = self._candidate(score=0.60, album={"id": "a1", "name": "A1", "artists": [{"name": "Artist"}]})
        second = self._candidate(score=0.55, album={"id": "a2", "name": "A2", "artists": [{"name": "Artist"}]}, popularity=9)
        self.plugin.config.data["related_artist_threshold"] = 0.65

        self.matcher.prompter = lambda *args, **kwargs: second
        selected, supplemental = self.matcher._select_album_candidate(
            local_album, [], [first, second], interactive=True,
        )
        self.assertEqual(selected["id"], "a2")
        self.assertEqual(len(supplemental), 1)
        self.assertEqual(supplemental[0]["album"]["id"], "a1")

    def test_prefers_standard_release_when_eligible(self):
        local_album = FakeAlbum("Local", "Artist")
        self.plugin.config.data["match_threshold"] = 0.7
        self.plugin.config.data["certainty_margin"] = 0.15
        candidates = [
            self._candidate(
                score=0.86, album={"id": "variant", "name": "Local (Deluxe)"},
                track_count=12, is_variant=True, popularity=90,
                base_title_score=0.95, artist_score=0.9,
            ),
            self._candidate(
                score=0.84, album={"id": "standard", "name": "Local"},
                track_count=10, is_variant=False, popularity=20,
                base_title_score=0.9, artist_score=0.9,
            ),
        ]
        selected, _supp = self.matcher._select_album_candidate(
            local_album, [], candidates, interactive=False,
        )
        self.assertEqual(selected["id"], "standard")

    def test_score_beats_popularity(self):
        # Score must be the primary sort key — a popular but inaccurate album
        # should not beat a better-scored one.
        local_album = FakeAlbum("Local", "Artist")
        self.plugin.config.data["match_threshold"] = 0.7
        self.plugin.config.data["certainty_margin"] = 0.15
        candidates = [
            self._candidate(
                score=0.90, album={"id": "accurate_low_pop", "name": "Local"},
                popularity=5, base_title_score=0.95, artist_score=0.9,
            ),
            self._candidate(
                score=0.76, album={"id": "popular_inaccurate", "name": "Local"},
                popularity=99, base_title_score=0.8, artist_score=0.8,
            ),
        ]
        selected, _supp = self.matcher._select_album_candidate(
            local_album, [], candidates, interactive=False,
        )
        self.assertEqual(selected["id"], "accurate_low_pop")


class SelectBestTrackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        self.matcher = self.plugin.matcher
        self.plugin.config.data["min_track_artist_score"] = 0.55

    def test_rejects_wrong_artist_near_title(self):
        item = FakeItem(
            "Scream at the Walls", artist="10 Years", albumartist="10 Years",
            track=1, disc=1, length=210,
        )
        results = {
            "tracks": {
                "items": [{
                    "id": "wrong",
                    "name": "I Scream at the Walls",
                    "artists": [{"name": "Skella"}],
                    "duration_ms": 210000,
                    "album": {"album_type": "album"},
                }],
            },
        }
        self.assertIsNone(self.matcher.select_best_track(item, results))

    def test_min_artist_score_override_is_honored(self):
        # With a strict floor, even a near-artist match should be rejected.
        item = FakeItem("Song", artist="Real Artist", track=1, disc=1, length=180)
        results = {
            "tracks": {
                "items": [{
                    "id": "near",
                    "name": "Song",
                    "artists": [{"name": "Almost Real Artst"}],
                    "duration_ms": 180000,
                    "album": {"album_type": "album"},
                }],
            },
        }
        self.assertIsNone(self.matcher.select_best_track(item, results, min_artist_score=0.99))


class ScoreTrackCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()

    def setUp(self):
        self.plugin = fresh_plugin()
        self.matcher = self.plugin.matcher
        self.plugin.config.data["duration_tolerance"] = 3

    def test_duration_bonus(self):
        item = FakeItem("Song", artist="Artist", length=200.0)
        track_within = {
            "name": "Song", "artists": [{"name": "Artist"}],
            "duration_ms": 201000, "album": {"album_type": "single"},
        }
        track_outside = {
            "name": "Song", "artists": [{"name": "Artist"}],
            "duration_ms": 210000, "album": {"album_type": "single"},
        }
        self.assertGreater(
            self.matcher.score_track_candidate(item, track_within),
            self.matcher.score_track_candidate(item, track_outside),
        )

    def test_album_type_bonus(self):
        item = FakeItem("Song", artist="Artist", length=0)
        track_album = {
            "name": "Song", "artists": [{"name": "Artist"}],
            "duration_ms": 0, "album": {"album_type": "album"},
        }
        track_single = {
            "name": "Song", "artists": [{"name": "Artist"}],
            "duration_ms": 0, "album": {"album_type": "single"},
        }
        self.assertGreater(
            self.matcher.score_track_candidate(item, track_album),
            self.matcher.score_track_candidate(item, track_single),
        )

    def test_capped_at_one(self):
        self.plugin.config.data["duration_tolerance"] = 10
        item = FakeItem("Song", artist="Artist", length=200.0)
        track = {
            "name": "Song", "artists": [{"name": "Artist"}],
            "duration_ms": 200000, "album": {"album_type": "album"},
        }
        self.assertLessEqual(self.matcher.score_track_candidate(item, track), 1.0)


if __name__ == "__main__":
    unittest.main()
