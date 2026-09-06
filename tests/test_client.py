"""Tests for SpotifyClient retry/throttle behavior."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plugin_test_utils import fresh_plugin, load_package


class SpotifyClientRetryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pkg = load_package()
        cls.client_module = __import__(
            "beetsplug.spotify_album_match.client", fromlist=["SpotifyException"],
        )

    def setUp(self):
        self.plugin = fresh_plugin()
        self.client = self.plugin.client
        self.client._spotify = __import__("types").SimpleNamespace()
        self.client.min_request_interval = 0
        self.client.max_retries = 3
        self.client.retry_delay = 1
        self.client.stop_on_rate_limit = True

    def _spotify_exc(self, **kwargs):
        SpotifyException = __import__("spotipy.exceptions", fromlist=["SpotifyException"]).SpotifyException
        return SpotifyException(**kwargs)

    def test_retry_request_aborts_on_rate_limit_when_configured(self):
        def always_429():
            raise self._spotify_exc(
                http_status=429, code=0, msg="rate limited",
                headers={"Retry-After": "7"},
            )

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with self.assertRaises(self.pkg.plugin.RateLimitAbort):
                self.client._retry_request(always_429)

        self.assertTrue(self.client._abort_requested)

    def test_retry_request_retries_after_429_when_not_stopping(self):
        self.client.stop_on_rate_limit = False
        results = [
            self._spotify_exc(
                http_status=429, code=0, msg="rate limited",
                headers={"Retry-After": "2"},
            ),
            "ok",
        ]

        def flaky():
            current = results.pop(0)
            if isinstance(current, Exception):
                raise current
            return current

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with mock.patch.object(self.client, "_set_rate_limit") as set_rate_limit:
                response = self.client._retry_request(flaky)

        self.assertEqual(response, "ok")
        set_rate_limit.assert_called_once_with(2)

    def test_retry_request_retries_500_then_succeeds(self):
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise self._spotify_exc(http_status=500, code=0, msg="server error")
            return "ok"

        import time as time_module
        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with mock.patch.object(time_module, "sleep") as sleep_mock:
                response = self.client._retry_request(flaky)

        self.assertEqual(response, "ok")
        sleep_mock.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
