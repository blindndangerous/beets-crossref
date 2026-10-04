"""The URL relationships of a MusicBrainz release: links to the same album
on other services, which make the strongest evidence crossref has."""

from __future__ import annotations

import re

from .cache import MISSING, Cache
from .http import JsonClient

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class MusicBrainz:
    def __init__(self, cache: Cache):
        self.cache = cache
        # MusicBrainz allows one request per second per client.
        self.client = JsonClient(
            "musicbrainz", "https://musicbrainz.org/ws/2", min_interval=1.0,
            headers={"Accept": "application/json"},
        )

    def release_urls(self, mbid: str) -> list[str]:
        """Every URL MusicBrainz links to this release; [] for none or a bad ID."""
        if not mbid or not UUID.match(mbid):
            return []

        def fetch():
            data = self.client.get(f"release/{mbid}", inc="url-rels", fmt="json")
            if data is MISSING:
                return MISSING
            return [
                rel["url"]["resource"]
                for rel in (data or {}).get("relations", [])
                if rel.get("url", {}).get("resource")
            ]

        urls = self.cache.remember("mb-urls", mbid, fetch)
        return [] if urls is MISSING else urls
