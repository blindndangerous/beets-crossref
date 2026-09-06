"""Tests for cli.py: extract_spotify_album_id, InteractivePrompter, progress file."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plugin_test_utils import load_package
from fakes import FakeAlbum


class ExtractSpotifyAlbumIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pkg = load_package()
        cls.cli = __import__("beetsplug.spotify_album_match.cli", fromlist=["x"])

    def test_from_uri(self):
        self.assertEqual(
            self.cli.extract_spotify_album_id("spotify:album:1234567890123456789012"),
            "1234567890123456789012",
        )

    def test_from_url(self):
        self.assertEqual(
            self.cli.extract_spotify_album_id("https://open.spotify.com/album/1234567890123456789012"),
            "1234567890123456789012",
        )

    def test_from_bare_id(self):
        self.assertEqual(
            self.cli.extract_spotify_album_id("1234567890123456789012"),
            "1234567890123456789012",
        )

    def test_invalid_returns_none(self):
        self.assertIsNone(self.cli.extract_spotify_album_id("not-an-id"))
        self.assertIsNone(self.cli.extract_spotify_album_id(""))
        self.assertIsNone(self.cli.extract_spotify_album_id(None))


class ProgressFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()
        cls.cli = __import__("beetsplug.spotify_album_match.cli", fromlist=["x"])

    def test_sanitize_rejects_non_json(self):
        self.assertIsNone(self.cli.sanitize_progress_file_path("/tmp/foo.txt"))
        self.assertIsNone(self.cli.sanitize_progress_file_path(""))
        self.assertIsNone(self.cli.sanitize_progress_file_path(None))

    def test_sanitize_accepts_json_path(self):
        result = self.cli.sanitize_progress_file_path("foo.json")
        self.assertIsNotNone(result)
        self.assertTrue(result.lower().endswith(".json"))

    def test_load_progress_missing_file_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "absent.json"
            self.assertEqual(self.cli.load_progress(str(path)), set())

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "p.json"
            self.cli.save_progress(str(path), {"a", "b"})
            loaded = self.cli.load_progress(str(path))
            self.assertEqual(loaded, {"a", "b"})
            with open(path) as fh:
                self.assertEqual(sorted(json.load(fh)), ["a", "b"])

    def test_save_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = pathlib.Path(tmpdir) / "nested" / "dir" / "p.json"
            self.cli.save_progress(str(nested), {"x"})
            self.assertTrue(nested.exists())

    def test_default_progress_path_uses_beetsdir_when_set(self):
        with mock.patch.dict("os.environ", {"BEETSDIR": "/tmp/beetsdir"}):
            path = self.cli.default_progress_path()
        self.assertIn("beetsdir", path)
        self.assertTrue(path.endswith("spotify_album_match_progress.json"))


class InteractivePrompterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_package()
        cls.cli = __import__("beetsplug.spotify_album_match.cli", fromlist=["x"])

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

    def test_b_raises_user_abort_and_calls_on_abort(self):
        on_abort = mock.Mock()
        prompter = self.cli.InteractivePrompter(on_abort=on_abort)
        local_album = FakeAlbum("Album", "Artist")
        with mock.patch("builtins.input", return_value="b"):
            with self.assertRaises(self.cli.UserAbort):
                prompter([self._candidate()], local_album, [], lambda *a, **kw: None)
        on_abort.assert_called_once()

    def test_ctrl_c_raises_user_abort(self):
        on_abort = mock.Mock()
        prompter = self.cli.InteractivePrompter(on_abort=on_abort)
        local_album = FakeAlbum("Album", "Artist")
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(self.cli.UserAbort):
                prompter([self._candidate()], local_album, [], lambda *a, **kw: None)
        on_abort.assert_called_once()

    def test_skip_returns_none(self):
        prompter = self.cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        with mock.patch("builtins.input", return_value="s"):
            result = prompter([self._candidate()], local_album, [], lambda *a, **kw: None)
        self.assertIsNone(result)

    def test_numeric_choice_returns_candidate(self):
        prompter = self.cli.InteractivePrompter()
        local_album = FakeAlbum("Album", "Artist")
        c1 = self._candidate()
        c2 = self._candidate(album={"id": "a2", "name": "A2", "artists": [{"name": "X"}]})
        with mock.patch("builtins.input", return_value="2"):
            result = prompter([c1, c2], local_album, [], lambda *a, **kw: None)
        self.assertIs(result, c2)


if __name__ == "__main__":
    unittest.main()
