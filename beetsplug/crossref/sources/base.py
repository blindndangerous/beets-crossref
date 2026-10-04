"""What every source adapter provides.

A source that resolves IDs fills `album_field` (and `track_field` when it
has per-track IDs).  Each lookup method answers with an ID or nothing; the
plugin decides which evidence to trust (see evidence.py) and checks the
tracklist before writing.  Lookups go through `self.cache` so a re-run costs
no requests.

fetch() reads the IDs already stored and returns new field values; the
plugin applies the write policy.  It never sets IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..cache import Cache
from ..tracks import TrackHit


@dataclass
class Updates:
    album: dict[str, object] = field(default_factory=dict)
    items: dict[int, dict[str, object]] = field(default_factory=dict)  # item.id -> fields


class Source:
    name = ""
    album_field = ""
    track_field: str | None = None
    resolves = True
    fetches = True

    def __init__(self, config, cache: Cache):
        """`config` is this source's confuse subview, e.g. config['crossref']['deezer']."""
        self.config = config
        self.cache = cache

    @property
    def ready(self) -> bool:
        """False when credentials are missing; the plugin then skips the source."""
        return True

    # --- resolve: each returns the service's album ID, or None -------------

    def album_id_from_url(self, url: str) -> str | None:
        """The album ID in a MusicBrainz URL relationship, if it points here."""
        return None

    def album_by_barcode(self, barcode: str) -> str | None:
        return None

    def album_ids_by_isrc(self, isrc: str) -> list[str]:
        """Albums carrying a recording with this ISRC (several is normal)."""
        return []

    def album_fuzzy(self, album, items) -> str | None:
        """Last resort: search by artist and title."""
        return None

    def album_tracks(self, album_id: str) -> list[TrackHit] | None:
        """The album's tracklist, or None when the album cannot be read."""
        return None

    def album_extras(self, album_id: str) -> dict[str, object]:
        """Other fields worth storing alongside a resolved album ID."""
        return {}

    # --- fetch --------------------------------------------------------------

    def fetch(self, album, items) -> Updates:
        return Updates()
