"""Tests for cli.py: extract_spotify_album_id, InteractivePrompter, progress file."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from fakes import FakeAlbum

from beetsplug.spotify_album_match import cli


class ExtractSpotifyAlbumIdTests(unittest.TestCase):
    def test_from_uri(self):
        self.assertEqual(
            cli.extract_spotify_album_id("spotify:album:1234567890123456789012"),
            "1234567890123456789012",
        )

    def test_from_url(self):
        self.assertEqual(
            cli.extract_spotify_album_id("https://open.spotify.com/album/1234567890123456789012"),
            "1234567890123456789012",
        )

    def test_from_bare_id(self):
        self.assertEqual(
            cli.extract_spotify_album_id("1234567890123456789012"),
            "1234567890123456789012",
        )

    def test_invalid_returns_none(self):
        self.assertIsNone(cli.extract_spotify_album_id("not-an-id"))
        self.assertIsNone(cli.extract_spotify_album_id(""))
        self.assertIsNone(cli.extract_spotify_album_id(None))


class ProgressFileTests(unittest.TestCase):
    def test_sanitize_rejects_non_json(self):
        self.assertIsNone(cli.sanitize_progress_file_path("/tmp/foo.txt"))
        self.assertIsNone(cli.sanitize_progress_file_path(""))
        self.assertIsNone(cli.sanitize_progress_file_path(None))

    def test_sanitize_accepts_json_path(self):
        result = cli.sanitize_progress_file_path("foo.json")
        self.assertIsNotNone(result)
        self.assertTrue(result.lower().endswith(".json"))

    def test_load_progress_missing_file_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "absent.json"
            self.assertEqual(cli.load_progress(str(path)), set())

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "p.json"
            cli.save_progress(str(path), {"a", "b"})
            loaded = cli.load_progress(str(path))
            self.assertEqual(loaded, {"a", "b"})
            with open(path) as fh:
                self.assertEqual(sorted(json.load(fh)), ["a", "b"])

    def test_save_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = pathlib.Path(tmpdir) / "nested" / "dir" / "p.json"
            cli.save_progress(str(nested), {"x"})
            self.assertTrue(nested.exists())

    def test_default_progress_path_uses_beetsdir_when_set(self):
        with mock.patch.dict("os.environ", {"BEETSDIR": "/tmp/beetsdir"}):
            path = cli.default_progress_path()
        self.assertIn("beetsdir", path)
        self.assertTrue(path.endswith("spotify_album_match_progress.json"))


class InteractivePrompterTests(unittest.TestCase):
    def _candidate(self, **overrides):
        base = {
            "score": 0.6,
            "album": {"id": "a1", "name": "A1", "artists": [{"name": "Artist"}]},
            "track_count": 10,
            "is_variant": False,
            "popularity": 1,
        }
        base.update(overrides)
        return base

    def test_b_raises_user_abort(self):
        prompter = cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        with mock.patch("builtins.input", return_value="b"):
            with self.assertRaises(cli.UserAbort):
                prompter([self._candidate()], local_album, [], lambda *a, **kw: None)

    def test_ctrl_c_raises_user_abort(self):
        prompter = cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(cli.UserAbort):
                prompter([self._candidate()], local_album, [], lambda *a, **kw: None)

    def test_skip_returns_none(self):
        prompter = cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        with mock.patch("builtins.input", return_value="s"):
            result = prompter([self._candidate()], local_album, [], lambda *a, **kw: None)
        self.assertIsNone(result)

    def test_numeric_choice_returns_candidate(self):
        prompter = cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        c1 = self._candidate()
        c2 = self._candidate(album={"id": "a2", "name": "A2", "artists": [{"name": "X"}]})
        with mock.patch("builtins.input", return_value="2"):
            result = prompter([c1, c2], local_album, [], lambda *a, **kw: None)
        self.assertIs(result, c2)

    def test_inline_album_id_is_built_into_a_candidate(self):
        prompter = cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        entered = self._candidate(album={"id": "b" * 22, "name": "Entered", "artists": []})
        built = []

        def build_candidate(album_id, album, items):
            built.append(album_id)
            return entered

        with mock.patch("builtins.input", return_value=f"i {'b' * 22}"):
            result = prompter([self._candidate()], local_album, [], build_candidate)

        self.assertIs(result, entered)
        self.assertEqual(built, ["b" * 22])

    def test_unparsable_album_id_reprompts(self):
        prompter = cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        c1 = self._candidate()

        with mock.patch("builtins.input", side_effect=["i nonsense", "1"]):
            result = prompter([c1], local_album, [], lambda *a, **kw: None)

        self.assertIs(result, c1)

