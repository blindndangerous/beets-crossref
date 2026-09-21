"""Tests for matching.py: AlbumMatcher candidate selection and track scoring."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fakes import FakeAlbum
from plugin_test_utils import fresh_plugin, load_package


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

    def test_upc_lookup_is_skipped_when_the_flag_is_off(self):
        """use_upc_lookup defaults to False: the barcode is not consulted at all."""
        local_album = FakeAlbum("Album", "Artist", barcode="0123456789012")

        with mock.patch.object(self.matcher.client, "search") as search:
            with mock.patch.object(self.matcher, "_search_album_candidates",
                                   return_value={}) as text_search:
                selected, _ = self.matcher.find_best_album_match(
                    local_album, interactive=False,
                )

        self.assertIsNone(selected)
        search.assert_not_called()
        text_search.assert_called_once()

    def test_upc_search_reads_the_beets_barcode_field(self):
        """beets albums carry 'barcode', never 'upc' (beets 2.2.0 library.py:1188)."""
        self.plugin.config.data["use_upc_lookup"] = True
        local_album = FakeAlbum("Album", "Artist", barcode="0123456789012")
        sp_album = {
            "id": "b" * 22, "name": "Album",
            "artists": [{"id": "c" * 22, "name": "Artist"}],
        }
        results = {"albums": {"items": [sp_album]}}

        with mock.patch.object(self.matcher.client, "search",
                               return_value=results) as search:
            selected, supplemental = self.matcher.find_best_album_match(
                local_album, interactive=False,
            )

        self.assertEqual(selected, sp_album)
        self.assertEqual(supplemental, [])
        search.assert_called_once_with(q="upc:0123456789012", type="album", limit=1)

    def test_upc_hit_that_does_not_match_title_and_artist_is_rejected(self):
        """A mistagged barcode must not be accepted without a sanity check."""
        self.plugin.config.data["use_upc_lookup"] = True
        local_album = FakeAlbum("Album", "Artist", barcode="0123456789012")
        sp_album = {
            "id": "b" * 22, "name": "A Completely Different Record",
            "artists": [{"id": "c" * 22, "name": "Another Band"}],
        }
        results = {"albums": {"items": [sp_album]}}

        with mock.patch.object(self.matcher.client, "search", return_value=results):
            with mock.patch.object(self.matcher, "_search_album_candidates",
                                   return_value={}) as text_search:
                selected, _ = self.matcher.find_best_album_match(
                    local_album, interactive=False,
                )

        self.assertIsNone(selected)
        text_search.assert_called_once()

    def test_non_interactive_uncertain_returns_none(self):
        local_album = FakeAlbum("Local", "Artist")
        candidates = [
            self._candidate(score=0.60, album={"id": "a1", "name": "A1", "artists": [{"name": "Artist"}]}),
            self._candidate(
                score=0.55, popularity=9,
                album={"id": "a2", "name": "A2", "artists": [{"name": "Artist"}]},
            ),
        ]
        selected, supplemental = self.matcher._select_album_candidate(
            local_album, [], candidates, interactive=False,
        )
        self.assertIsNone(selected)
        self.assertEqual(supplemental, [])

    def test_interactive_uses_prompter_choice(self):
        local_album = FakeAlbum("Local", "Artist")
        first = self._candidate(score=0.60, album={"id": "a1", "name": "A1", "artists": [{"name": "Artist"}]})
        second = self._candidate(
            score=0.55, popularity=9,
            album={"id": "a2", "name": "A2", "artists": [{"name": "Artist"}]},
        )
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


if __name__ == "__main__":
    unittest.main()
