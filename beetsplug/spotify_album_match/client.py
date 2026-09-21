"""Spotify Web API client wrapper.

All Spotify API concerns (authentication, throttling, pagination, caching)
live here so the matching layer can focus on music logic.
"""
import logging
import threading
import time

import requests
from cachetools import TTLCache
from spotipy import Spotify
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyClientCredentials

from .helpers import clean_spotify_id

log = logging.getLogger("beets.spotify_album_match")


class RateLimitAbort(Exception):
    pass


class SpotifyClient:
    """Wraps spotipy.Spotify with caching, rate-limiting, and retry logic.

    The cache and request locks are held for a possible future concurrent
    caller; every caller today is single-threaded.
    """

    def __init__(
        self,
        *,
        client_id,
        client_secret,
        max_retries=5,
        retry_delay=5,
        stop_on_rate_limit=True,
        min_request_interval=5.0,
        cache_ttl=600,
    ):
        self._spotify = self._build_spotify(client_id, client_secret)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.stop_on_rate_limit = stop_on_rate_limit
        self.min_request_interval = max(0.0, min_request_interval)

        self._album_tracks_cache = TTLCache(maxsize=512, ttl=cache_ttl)
        self._album_details_cache = TTLCache(maxsize=512, ttl=cache_ttl)
        # Search results cached with a shorter TTL to deduplicate repeated queries
        # within a single run (related-release repair re-runs the same album searches).
        self._search_cache = TTLCache(maxsize=256, ttl=min(cache_ttl, 300))
        self._cache_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_request_time = 0.0
        self._rate_limit_until = 0.0
        self._abort_requested = False
        self._api_call_count = 0

    @property
    def is_ready(self):
        return self._spotify is not None

    def abort(self):
        self._abort_requested = True

    def reset(self):
        self._abort_requested = False

    def reset_call_count(self):
        self._api_call_count = 0

    @property
    def api_call_count(self):
        return self._api_call_count

    # ------------------------------------------------------------------
    # High-level API
    # ------------------------------------------------------------------

    def get_album_tracks(self, album_id):
        album_id = clean_spotify_id(album_id)
        if not album_id:
            log.warning("Skipping Spotify album track lookup for blank/malformed album ID.")
            return []
        with self._cache_lock:
            cached = self._album_tracks_cache.get(album_id)
        if cached is not None:
            log.debug(f"Cache HIT for album ID: {album_id}")
            return cached

        log.debug(f"Cache MISS for album ID: {album_id}. Fetching from Spotify.")
        all_tracks = []
        try:
            results = self._retry_request(self._spotify.album_tracks, album_id)
            if not results:
                return []
            all_tracks.extend(results['items'])
            while results and results['next']:
                results = self._retry_request(self._spotify.next, results)
                if results:
                    all_tracks.extend(results['items'])
            with self._cache_lock:
                self._album_tracks_cache[album_id] = all_tracks
            return all_tracks
        except SpotifyException as e:
            log.warning(f"Could not fetch tracks for album ID {album_id}: {e}")
            return []

    def get_album(self, album_id):
        album_id = clean_spotify_id(album_id)
        if not album_id:
            log.warning("Skipping Spotify album lookup for blank/malformed album ID.")
            return None
        return self._cached_api_get(
            self._album_details_cache,
            album_id,
            lambda: self._retry_request(self._spotify.album, album_id),
            "album details for album ID",
        )

    def get_albums_bulk(self, album_ids):
        """Return a {album_id: album_dict} map for many IDs (batched 20 at a time).

        Checks _album_details_cache first so IDs already fetched earlier in a run
        are never re-fetched via the API.
        """
        if not album_ids:
            return {}
        unique_ids = list(
            dict.fromkeys(
                clean_id for aid in album_ids
                if (clean_id := clean_spotify_id(aid))
            )
        )

        details_by_id = {}
        uncached_ids = []
        with self._cache_lock:
            for album_id in unique_ids:
                cached = self._album_details_cache.get(album_id)
                if cached is not None:
                    details_by_id[album_id] = cached
                else:
                    uncached_ids.append(album_id)

        if uncached_ids:
            log.debug(f"Bulk-fetching {len(uncached_ids)} album(s) not in cache.")
        for i in range(0, len(uncached_ids), 20):
            chunk = uncached_ids[i:i + 20]
            try:
                results = self._retry_request(self._spotify.albums, chunk)
            except SpotifyException as e:
                log.warning(f"Could not fetch album details for IDs {chunk}: {e}")
                continue

            albums = results.get('albums', []) if isinstance(results, dict) else []
            for album in albums:
                if not album or not isinstance(album, dict):
                    continue
                album_id = album.get('id')
                if not album_id:
                    continue
                details_by_id[album_id] = album
                with self._cache_lock:
                    self._album_details_cache[album_id] = album
                tracks = album.get('tracks', {})
                if tracks and isinstance(tracks, dict):
                    items = tracks.get('items', [])
                    if items and not tracks.get('next'):
                        with self._cache_lock:
                            self._album_tracks_cache[album_id] = items
        return details_by_id

    def search(self, **kwargs):
        """Call spotify.search with retry and TTL-cached deduplication."""
        cache_key = "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        with self._cache_lock:
            cached = self._search_cache.get(cache_key)
        if cached is not None:
            log.debug(f"Search cache HIT for key: {cache_key!r}")
            return cached
        result = self._retry_request(self._spotify.search, **kwargs)
        if result is not None:
            with self._cache_lock:
                self._search_cache[cache_key] = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_spotify(client_id, client_secret):
        if not client_id or not client_secret:
            log.error("Spotify client_id and client_secret must be set.")
            return None
        try:
            # MemoryCacheHandler: spotipy otherwise writes the access token to
            # a ".cache" file in whatever directory beet was launched from
            # (oauth2.py:182, cache_handler.py:69). The client-credentials flow
            # needs no persistence -- a new token is cheap to fetch.
            auth_manager = SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                cache_handler=MemoryCacheHandler(),
            )
            # Hand spotipy an already-built session so it installs no urllib3
            # Retry adapter (spotipy client.py:188). With one, every status in
            # spotipy's default forcelist (429, 500, 502, 503, 504) is raised
            # as a synthetic SpotifyException(429) with no headers, which hides
            # real 5xx errors and the Retry-After value from _retry_request.
            return Spotify(auth_manager=auth_manager, requests_session=requests.Session())
        except Exception as e:
            log.error(f"Failed to initialize Spotify client: {e}")
            return None

    def _retry_request(self, func, *args, **kwargs):
        for attempt in range(self.max_retries):
            self._wait_for_request_slot()
            self._api_call_count += 1
            try:
                return func(*args, **kwargs)
            except SpotifyException as e:
                if e.http_status == 429:
                    retry_after = self.retry_delay
                    headers = getattr(e, "headers", None)
                    if isinstance(headers, dict):
                        header_val = headers.get('Retry-After')
                        if header_val is not None:
                            try:
                                retry_after = int(header_val)
                            except (TypeError, ValueError):
                                retry_after = self.retry_delay
                    if self.stop_on_rate_limit:
                        self._abort_requested = True
                        raise RateLimitAbort(
                            f"Rate limited by Spotify. Aborting run to avoid longer bans "
                            f"(waiting {retry_after}s would be required)."
                        ) from e
                    log.warning(f"Rate limited. Retrying in {retry_after} seconds...")
                    self._set_rate_limit(retry_after)
                    continue
                elif e.http_status >= 500:
                    log.warning(f"Spotify server error ({e.http_status}). Retrying...")
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    raise
            except requests.exceptions.RequestException as e:
                # Timeouts and connection errors are as transient as a 5xx, and
                # spotipy lets them through untouched. Back off the same way.
                if attempt == self.max_retries - 1:
                    log.error(f"Request failed after {self.max_retries} attempts: {e}")
                    raise
                log.warning(f"Spotify request failed ({e}). Retrying...")
                time.sleep(self.retry_delay * (attempt + 1))
        log.error(f"Request failed after {self.max_retries} retries.")
        raise SpotifyException(http_status=0, code=-1, msg="Max retries exceeded.") from None

    def _wait_for_request_slot(self):
        while True:
            if self._abort_requested:
                raise RateLimitAbort("Aborting run due to rate limit.")
            with self._request_lock:
                now = time.monotonic()
                wait_until = max(self._rate_limit_until, self._next_request_time)
                if now >= wait_until:
                    self._next_request_time = now + self.min_request_interval
                    return
                sleep_for = wait_until - now
            if sleep_for > 0:
                log.debug(f"Throttling Spotify requests for {sleep_for:.2f}s.")
                time.sleep(sleep_for)

    def _set_rate_limit(self, retry_after):
        if retry_after <= 0:
            return
        with self._request_lock:
            until = time.monotonic() + retry_after
            if until > self._rate_limit_until:
                self._rate_limit_until = until

    def _cached_api_get(self, cache, key, fetch_fn, error_label):
        with self._cache_lock:
            cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            result = fetch_fn()
        except SpotifyException as e:
            log.warning(f"Could not fetch {error_label} {key}: {e}")
            return None
        if result is not None:
            with self._cache_lock:
                cache[key] = result
        return result
