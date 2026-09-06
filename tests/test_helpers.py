from dataclasses import dataclass
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from beetsplug.spotify_album_match.helpers import (
    artist_set_score,
    build_album_search_queries,
    build_track_search_queries,
    escape_query_value,
    find_matching_spotify_track,
    is_variant_title,
    normalize_title,
    split_artist_tokens,
    strip_version_tokens,
)


@dataclass
class DummyItem:
    title: str
    artist: str = ""
    albumartist: str = ""
    track: int = 0
    disc: int = 0
    isrc: str = ""
    length: float = 0.0


class SpotifyAlbumMatchHelpersTests(unittest.TestCase):
    def test_escape_query_value_escapes_quotes_and_backslashes(self):
        escaped = escape_query_value('The "Best" \\ Album')
        self.assertEqual(escaped, 'The \\"Best\\" \\\\ Album')

    def test_album_queries_escape_quoted_metadata(self):
        queries = build_album_search_queries('The "Best" Album', 'Artist "Name"')
        self.assertTrue(any('album:"The \\"Best\\" Album"' in query for query in queries))
        self.assertTrue(any('artist:"Artist \\"Name\\""' in query for query in queries))

    def test_track_queries_escape_quoted_metadata(self):
        queries = build_track_search_queries(
            'Song "A"',
            'Artist "B"',
            'Album "C"',
        )
        self.assertTrue(any('track:"Song \\"A\\""' in query for query in queries))
        self.assertTrue(any('artist:"Artist \\"B\\""' in query for query in queries))
        self.assertTrue(any('album:"Album \\"C\\""' in query for query in queries))

    def test_artist_split_does_not_break_acdc_or_x_ambassadors(self):
        self.assertEqual(split_artist_tokens("AC/DC"), ["ac dc"])
        self.assertEqual(split_artist_tokens("X Ambassadors"), ["x ambassadors"])

    def test_artist_split_supports_spaced_x_and_slash_collabs(self):
        self.assertEqual(split_artist_tokens("Artist x Producer"), ["artist", "producer"])
        self.assertEqual(split_artist_tokens("Artist / Producer"), ["artist", "producer"])

    def test_track_matching_prefers_title_over_track_number_only(self):
        item = DummyItem(
            title="Song A",
            artist="Artist",
            albumartist="Artist",
            track=2,
            disc=1,
            length=200,
        )
        spotify_tracks = [
            {
                "id": "wrong",
                "name": "Completely Different",
                "artists": [{"name": "Artist"}],
                "track_number": 2,
                "disc_number": 1,
                "duration_ms": 200000,
            },
            {
                "id": "good",
                "name": "Song A",
                "artists": [{"name": "Artist"}],
                "track_number": 5,
                "disc_number": 1,
                "duration_ms": 200000,
            },
        ]

        match = find_matching_spotify_track(item, spotify_tracks, duration_tolerance=3, match_threshold=0.7)
        self.assertEqual(match["id"], "good")

    def test_track_matching_rejects_low_confidence_position_only_match(self):
        item = DummyItem(
            title="Song A",
            artist="Artist",
            albumartist="Artist",
            track=2,
            disc=1,
            length=200,
        )
        spotify_tracks = [
            {
                "id": "wrong",
                "name": "Completely Different",
                "artists": [{"name": "Artist"}],
                "track_number": 2,
                "disc_number": 1,
                "duration_ms": 200000,
            }
        ]

        match = find_matching_spotify_track(item, spotify_tracks, duration_tolerance=3, match_threshold=0.7)
        self.assertIsNone(match)

    def test_track_matching_rejects_different_artist_even_with_near_title(self):
        item = DummyItem(
            title="Scream at the Walls",
            artist="10 Years",
            albumartist="10 Years",
            track=1,
            disc=1,
            length=210,
        )
        spotify_tracks = [
            {
                "id": "wrong",
                "name": "I Scream at the Walls",
                "artists": [{"name": "Skella"}],
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 210000,
            }
        ]

        match = find_matching_spotify_track(
            item,
            spotify_tracks,
            duration_tolerance=3,
            match_threshold=0.7,
            min_artist_score=0.55,
        )
        self.assertIsNone(match)

    # --- strip_version_tokens ---

    def test_strip_version_tokens_removes_bracketed_keyword(self):
        self.assertEqual(strip_version_tokens("Album (Deluxe Edition)"), "Album")

    def test_strip_version_tokens_removes_square_bracket_keyword(self):
        self.assertEqual(strip_version_tokens("Album [Remastered]"), "Album")

    def test_strip_version_tokens_removes_trailing_dash_keyword(self):
        self.assertEqual(strip_version_tokens("Album - Remastered"), "Album")

    def test_strip_version_tokens_removes_inline_keyword(self):
        result = strip_version_tokens("Remastered Album")
        self.assertNotIn("Remastered", result)

    def test_strip_version_tokens_no_change_without_keywords(self):
        self.assertEqual(strip_version_tokens("Simple Album Title"), "Simple Album Title")

    def test_strip_version_tokens_empty_returns_empty(self):
        self.assertEqual(strip_version_tokens(""), "")
        self.assertEqual(strip_version_tokens(None), "")

    # --- normalize_title ---

    def test_normalize_title_strips_version_tokens_and_brackets(self):
        self.assertEqual(normalize_title("Album (Deluxe Edition)"), "album")

    def test_normalize_title_strips_feat_clause(self):
        result = normalize_title("Song feat. Someone Else")
        self.assertNotIn("feat", result)
        self.assertNotIn("someone", result)

    def test_normalize_title_lowercases_and_removes_punctuation(self):
        self.assertEqual(normalize_title("Hello, World!"), "hello world")

    def test_normalize_title_empty_returns_empty(self):
        self.assertEqual(normalize_title(""), "")

    # --- is_variant_title ---

    def test_is_variant_title_true_for_deluxe(self):
        self.assertTrue(is_variant_title("Album (Deluxe Edition)"))

    def test_is_variant_title_true_for_remastered(self):
        self.assertTrue(is_variant_title("Some Album Remastered"))

    def test_is_variant_title_false_for_plain_title(self):
        self.assertFalse(is_variant_title("Plain Album Title"))

    def test_is_variant_title_false_for_empty(self):
        self.assertFalse(is_variant_title(""))
        self.assertFalse(is_variant_title(None))

    # --- artist_set_score ---

    def test_artist_set_score_exact_match_is_high(self):
        score = artist_set_score("The Beatles", "The Beatles")
        self.assertGreaterEqual(score, 0.95)

    def test_artist_set_score_primary_artist_in_list(self):
        score = artist_set_score("Drake", ["Drake", "21 Savage"])
        self.assertGreaterEqual(score, 0.7)

    def test_artist_set_score_empty_inputs_return_zero(self):
        self.assertEqual(artist_set_score("", "Artist"), 0.0)
        self.assertEqual(artist_set_score("Artist", ""), 0.0)
        self.assertEqual(artist_set_score("", []), 0.0)

    # --- find_matching_spotify_track ISRC ---

    def test_track_matching_isrc_bypasses_fuzzy(self):
        item = DummyItem(
            title="Something Completely Different",
            artist="Wrong Artist",
            isrc="USRC12345678",
        )
        spotify_tracks = [
            {
                "id": "isrc_match",
                "name": "Actual Song Name",
                "artists": [{"name": "Real Artist"}],
                "external_ids": {"isrc": "USRC12345678"},
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 200000,
            },
            {
                "id": "fuzzy_candidate",
                "name": "Something Completely Different",
                "artists": [{"name": "Wrong Artist"}],
                "external_ids": {},
                "track_number": 2,
                "disc_number": 1,
                "duration_ms": 200000,
            },
        ]
        match = find_matching_spotify_track(item, spotify_tracks, duration_tolerance=3)
        self.assertEqual(match["id"], "isrc_match")

    def test_track_matching_non_matching_isrc_falls_through_to_fuzzy(self):
        item = DummyItem(
            title="Song A",
            artist="Artist",
            isrc="NOMATCH00000",
        )
        spotify_tracks = [
            {
                "id": "fuzzy_match",
                "name": "Song A",
                "artists": [{"name": "Artist"}],
                "external_ids": {"isrc": "DIFFERENT00000"},
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 200000,
            },
        ]
        match = find_matching_spotify_track(item, spotify_tracks, duration_tolerance=3, match_threshold=0.5)
        self.assertEqual(match["id"], "fuzzy_match")

    # --- find_matching_spotify_track duration penalty ---

    def test_track_matching_large_duration_diff_applies_penalty(self):
        # Both tracks have identical title/artist; the one with wildly different duration
        # should score lower due to the penalty.
        item = DummyItem(title="Song", artist="Artist", length=200.0)
        tracks = [
            {
                "id": "close_duration",
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "external_ids": {},
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 201000,  # 1 second off → bonus
            },
            {
                "id": "far_duration",
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "external_ids": {},
                "track_number": 2,
                "disc_number": 1,
                "duration_ms": 260000,  # 60 seconds off → penalty
            },
        ]
        match = find_matching_spotify_track(
            item, tracks, duration_tolerance=3,
            duration_mismatch_penalty_threshold=10, duration_mismatch_penalty=0.10,
        )
        self.assertEqual(match["id"], "close_duration")

    def test_track_matching_large_duration_diff_can_cause_no_match(self):
        # If only a far-duration track exists, penalty can push score below threshold.
        item = DummyItem(title="Song", artist="Artist", length=200.0)
        tracks = [
            {
                "id": "only_track",
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "external_ids": {},
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 380000,  # 180 seconds off
            },
        ]
        # With a generous threshold of 0.5 but heavy penalty, should still match.
        match_loose = find_matching_spotify_track(
            item, tracks, duration_tolerance=3, match_threshold=0.5,
            duration_mismatch_penalty_threshold=10, duration_mismatch_penalty=0.10,
        )
        self.assertIsNotNone(match_loose)  # still matches at low threshold

        # With tighter threshold and max penalty, should not match.
        match_strict = find_matching_spotify_track(
            item, tracks, duration_tolerance=3, match_threshold=0.99,
            duration_mismatch_penalty_threshold=10, duration_mismatch_penalty=0.10,
        )
        self.assertIsNone(match_strict)

    def test_track_matching_small_duration_diff_gives_bonus(self):
        item = DummyItem(title="Song", artist="Artist", length=200.0)
        tracks_bonus = [
            {
                "id": "close",
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "external_ids": {},
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 202000,  # 2s off → within tolerance → +0.05 bonus
            },
        ]
        tracks_no_bonus = [
            {
                "id": "mid",
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "external_ids": {},
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 206000,  # 6s off → outside tolerance, inside penalty threshold → no adjustment
            },
        ]
        # title(1.0)*0.6 + artist(1.0)*0.25 = 0.85 base.
        # With bonus (+0.05) = 0.90; without = 0.85.
        # A threshold of 0.88 sits between those two values.
        match_bonus = find_matching_spotify_track(
            item, tracks_bonus, duration_tolerance=3, match_threshold=0.88,
            duration_mismatch_penalty_threshold=10, duration_mismatch_penalty=0.10,
        )
        match_no_bonus = find_matching_spotify_track(
            item, tracks_no_bonus, duration_tolerance=3, match_threshold=0.88,
            duration_mismatch_penalty_threshold=10, duration_mismatch_penalty=0.10,
        )
        self.assertIsNotNone(match_bonus)
        self.assertIsNone(match_no_bonus)


if __name__ == "__main__":
    unittest.main()
