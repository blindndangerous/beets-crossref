"""Shared fake beets items/albums used across test modules.

These mirror the parts of real beets (2.2.0) semantics that this plugin
depends on. In particular an Item does NOT stand alone: for a flexible field
it has no value of its own, `get`, `in` and `[]` all fall through to the
item's album (beets does this via `Item._cached_album`), while `del` only ever
touches the item's own storage and raises `KeyError` when the item has no own
value. An Album has no such fallback -- it is a plain model.

Own values live in `_values_flex`, the same attribute real beets uses for
flexible fields, so production code can read an object's own value honestly.
"""


class _FakeModel:
    """Plain beets-model semantics: own flexible values only, no fallback."""

    def __init__(self):
        self._values_flex = {}
        self.store_calls = 0

    def get(self, key, default=None):
        return self._values_flex.get(key, default)

    def __getitem__(self, key):
        return self._values_flex[key]

    def __setitem__(self, key, value):
        self._values_flex[key] = value

    def __delitem__(self, key):
        if key not in self._values_flex:
            raise KeyError(f"no such field {key!r}")
        del self._values_flex[key]

    def __contains__(self, key):
        return key in self._values_flex

    def store(self):
        self.store_calls += 1


class FakeItem(_FakeModel):
    """Beets Item: flexible-field reads fall back to the item's album."""

    def __init__(self, title, artist="", albumartist="", track=0, disc=0, isrc="", length=0.0):
        super().__init__()
        self.title = title
        self.artist = artist
        self.albumartist = albumartist
        self.track = track
        self.disc = disc
        self.isrc = isrc
        self.length = length
        # Set by FakeAlbum when the item is attached; beets calls it _cached_album.
        self._album = None

    def get(self, key, default=None):
        if key in self._values_flex:
            return self._values_flex[key]
        if self._album is not None:
            return self._album.get(key, default)
        return default

    def __getitem__(self, key):
        try:
            return self._values_flex[key]
        except KeyError:
            if self._album is None:
                raise
            return self._album[key]

    def __contains__(self, key):
        if key in self._values_flex:
            return True
        return self._album is not None and key in self._album

    # __delitem__ is deliberately NOT overridden: beets deletes only the item's
    # own value and raises KeyError when the value belongs to the album.


class FakeAlbum(_FakeModel):
    _next_id = 1

    def __init__(self, album, albumartist, year=0, items=None):
        super().__init__()
        self.id = FakeAlbum._next_id
        FakeAlbum._next_id += 1
        self.album = album
        self.albumartist = albumartist
        self.year = year
        self._items = []
        for item in items or []:
            self.add_item(item)

    def add_item(self, item):
        """Attach an item and wire the back-reference beets sets on load."""
        self._items.append(item)
        item._album = self
        return item

    def items(self):
        return list(self._items)
