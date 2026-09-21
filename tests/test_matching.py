"""Tests for matching.py: AlbumMatcher candidate selection."""
from beets import plugins
from beets.library import Album
from beets.test.helper import PluginTestCase


class SpotifyPluginTestCase(PluginTestCase):
    """beets' own harness: temp library, temp confuse config, real plugin.

    `preload_plugin` is left at its default True, so beets loads the plugin
    exactly as a real run does (registering the three flexible field types) and
    the test picks the resulting instance out of `find_plugins()`. Every test
    patches `plugin.client`, so nothing ever reaches the network.
    """

    plugin = "spotify_album_match"

    def setUp(self):
        super().setUp()
        self.plugin = next(
            p for p in plugins.find_plugins() if p.name == "spotify_album_match"
        )


class AlbumMatcherSelectionTests(SpotifyPluginTestCase):
    def setUp(self):
        super().setUp()
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
        local_album = Album(album="Local", albumartist="Artist")
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
        local_album = Album(album="Local", albumartist="Artist")
        first = self._candidate(score=0.60, album={"id": "a1", "name": "A1", "artists": [{"name": "Artist"}]})
        second = self._candidate(
            score=0.55, popularity=9,
            album={"id": "a2", "name": "A2", "artists": [{"name": "Artist"}]},
        )
        self.config["spotify_album_match"]["related_artist_threshold"] = 0.65

        self.matcher.prompter = lambda *args, **kwargs: second
        selected, supplemental = self.matcher._select_album_candidate(
            local_album, [], [first, second], interactive=True,
        )
        self.assertEqual(selected["id"], "a2")
        self.assertEqual(len(supplemental), 1)
        self.assertEqual(supplemental[0]["album"]["id"], "a1")

    def test_prefers_standard_release_when_eligible(self):
        local_album = Album(album="Local", albumartist="Artist")
        self.config["spotify_album_match"].set(
            {"match_threshold": 0.7, "certainty_margin": 0.15},
        )
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
        local_album = Album(album="Local", albumartist="Artist")
        self.config["spotify_album_match"].set(
            {"match_threshold": 0.7, "certainty_margin": 0.15},
        )
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
