"""Rate-limited JSON over HTTP, shared by every source.

One client per service.  Requests are spaced by `min_interval`; 429 and 5xx
are retried with backoff, honouring Retry-After up to `max_wait`.  A wait
longer than that (a spent daily quota) raises SourceUnavailable so the run
moves on instead of sleeping for hours.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from . import __version__
from .cache import MISSING

log = logging.getLogger("beets.crossref")

USER_AGENT = f"beets-crossref/{__version__} ( https://github.com/blindndangerous/beets-crossref )"


class SourceUnavailable(RuntimeError):
    """The service will not answer again soon; skip it for the rest of the run."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when a refusal caused it


class JsonClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        min_interval: float = 0.0,
        headers: dict[str, str] | None = None,
        attempts: int = 5,
        max_wait: float = 120.0,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.attempts = attempts
        self.max_wait = max_wait
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers.update(headers or {})
        self._last = 0.0
        self.calls = 0

    def _space(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path: str, **params: Any) -> Any:
        """Decoded JSON; None for 404; MISSING when the request kept failing."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        delay = 2.0
        for _ in range(self.attempts):
            self._space()
            self.calls += 1
            try:
                response = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                # Not the exception text: it contains the URL, and some services
                # (Last.fm) take the API key as a query parameter.
                log.warning("%s: %s failed (%s), retrying in %.0f s",
                            self.name, path, type(exc).__name__, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                asked = _retry_after(response)
                reason = _reason(response) if response.status_code == 429 else ""
                why = f" ({reason})" if reason else ""
                if reason == "QUOTA_EXCEEDED":
                    # Quota does not recover in seconds, whatever Retry-After says.
                    log.warning("%s: HTTP 429%s on %s, giving up for this run", self.name, why, path)
                    raise SourceUnavailable(f"{self.name} quota exceeded{why} on {path}", status=429)
                if asked and asked > self.max_wait:
                    raise SourceUnavailable(f"{self.name} asks to wait {asked:.0f} s on {path}{why}")
                wait = asked or min(delay, self.max_wait)
                log.warning("%s: HTTP %d%s on %s, waiting %.0f s",
                            self.name, response.status_code, why, path, wait)
                time.sleep(wait)
                delay *= 2
                continue
            if response.status_code in (401, 403):
                raise SourceUnavailable(
                    f"{self.name} refused {path}: HTTP {response.status_code}", status=response.status_code
                )
            try:
                # Some services (Last.fm) explain a 4xx in a JSON body; the
                # source reads its own error format.
                return response.json()
            except ValueError:
                log.warning("%s: HTTP %d on %s with no JSON body", self.name, response.status_code, path)
                return MISSING
        log.warning("%s: gave up on %s", self.name, path)
        return MISSING


def _retry_after(response: requests.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def _reason(response: requests.Response) -> str:
    """The `reason` of a 429 JSON body (Spotify: QUOTA_EXCEEDED), or "" when absent."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        reason = body.get("reason")
        if reason is None and isinstance(body.get("error"), dict):
            reason = body["error"].get("reason")
        if isinstance(reason, str):
            return reason.strip()[:40]
    return ""
