"""Tests for SpotifyClient retry/throttle behavior."""
import types
import unittest
from unittest import mock

import requests
from spotipy.exceptions import SpotifyException

from beetsplug.spotify_album_match.client import RateLimitAbort, SpotifyClient


class SpotifyClientRetryTests(unittest.TestCase):

    def setUp(self):
        # client_id=None leaves _spotify None (and logs an error), which is
        # what every test here wants: the transport is a stand-in.
        self.client = SpotifyClient(
            client_id=None, client_secret=None,
            max_retries=3, retry_delay=1,
            stop_on_rate_limit=True, min_request_interval=0,
        )
        self.client._spotify = types.SimpleNamespace()

    def test_retry_request_aborts_on_rate_limit_when_configured(self):
        def always_429():
            raise SpotifyException(
                http_status=429, code=0, msg="rate limited",
                headers={"Retry-After": "7"},
            )

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with self.assertRaises(RateLimitAbort):
                self.client._retry_request(always_429)

        self.assertTrue(self.client._abort_requested)

    def test_retry_request_retries_after_429_when_not_stopping(self):
        self.client.stop_on_rate_limit = False
        results = [
            SpotifyException(
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

    def test_retry_request_retries_503_then_succeeds(self):
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise SpotifyException(http_status=503, code=0, msg="unavailable")
            return "ok"

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with mock.patch("time.sleep") as sleep_mock:
                response = self.client._retry_request(flaky)

        self.assertEqual(response, "ok")
        sleep_mock.assert_called_once_with(1)

    def test_retry_request_retries_connection_errors_then_succeeds(self):
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise requests.exceptions.ConnectionError("connection reset")
            return "ok"

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with mock.patch("time.sleep") as sleep_mock:
                response = self.client._retry_request(flaky)

        self.assertEqual(response, "ok")
        sleep_mock.assert_called_once_with(1)

    def test_retry_request_raises_after_max_retries_on_connection_error(self):
        attempts = {"count": 0}

        def always_timeout():
            attempts["count"] += 1
            raise requests.exceptions.ReadTimeout("too slow")

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with mock.patch("time.sleep"):
                with self.assertRaises(requests.exceptions.RequestException):
                    self.client._retry_request(always_timeout)

        self.assertEqual(attempts["count"], self.client.max_retries)

    def test_retry_request_retries_500_then_succeeds(self):
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise SpotifyException(http_status=500, code=0, msg="server error")
            return "ok"

        with mock.patch.object(self.client, "_wait_for_request_slot", return_value=None):
            with mock.patch("time.sleep") as sleep_mock:
                response = self.client._retry_request(flaky)

        self.assertEqual(response, "ok")
        sleep_mock.assert_called_once_with(1)


class SpotifyClientCacheAndPaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = SpotifyClient(
            client_id=None, client_secret=None, min_request_interval=0,
        )

    def test_search_results_are_cached_per_query(self):
        calls = []

        def search(**kwargs):
            calls.append(kwargs)
            return {"albums": {"items": []}}

        self.client._spotify = types.SimpleNamespace(search=search)

        first = self.client.search(q="album:A", type="album", limit=3)
        second = self.client.search(q="album:A", type="album", limit=3)
        self.client.search(q="album:B", type="album", limit=3)

        self.assertEqual(first, second)
        self.assertEqual([c["q"] for c in calls], ["album:A", "album:B"])

    def test_get_album_tracks_follows_pagination(self):
        pages = [
            {"items": [{"id": "t1"}], "next": "page2"},
            {"items": [{"id": "t2"}], "next": None},
        ]
        self.client._spotify = types.SimpleNamespace(
            album_tracks=lambda album_id: pages[0],
            next=lambda results: pages[1],
        )

        tracks = self.client.get_album_tracks("A" * 22)

        self.assertEqual([t["id"] for t in tracks], ["t1", "t2"])
        # Second call is served from the cache.
        self.assertEqual(self.client.get_album_tracks("A" * 22), tracks)


class SpotifyClientTransportTests(unittest.TestCase):
    """The spotipy object must not install urllib3's Retry adapter.

    With spotipy's default status_forcelist (429, 500, 502, 503, 504) every
    5xx arrives as a synthetic SpotifyException(429) carrying no headers, so
    the 5xx branch of _retry_request is unreachable and Retry-After is never
    read. Handing spotipy an already-built requests.Session skips the adapter
    (spotipy client.py:188).
    """

    def test_token_is_cached_in_memory_not_on_disk(self):
        """spotipy defaults to CacheFileHandler, which writes ".cache" in the CWD.

        (spotipy oauth2.py:182 and cache_handler.py:69.) The client-credentials
        flow has nothing worth persisting, so the token stays in memory.
        """
        from spotipy.cache_handler import MemoryCacheHandler

        client = SpotifyClient(client_id="an-id", client_secret="a-secret")
        self.assertIsInstance(
            client._spotify.auth_manager.cache_handler, MemoryCacheHandler,
        )

    def test_client_is_built_with_a_plain_requests_session(self):
        client = SpotifyClient(client_id="an-id", client_secret="a-secret")
        session = client._spotify._session
        self.assertIsInstance(session, requests.Session)
        # spotipy only mounts its urllib3 Retry adapter in _build_session(),
        # which it skips for a session it was handed (client.py:188).
        adapter = session.get_adapter("https://api.spotify.com/v1/")
        self.assertEqual(adapter.max_retries.total, 0)
