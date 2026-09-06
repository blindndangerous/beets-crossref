"""Shared fake beets items/albums used across test modules."""


class FakeItem:
    def __init__(self, title, artist="", albumartist="", track=0, disc=0, isrc="", length=0.0):
        self.title = title
        self.artist = artist
        self.albumartist = albumartist
        self.track = track
        self.disc = disc
        self.isrc = isrc
        self.length = length
        self._data = {}
        self.store_calls = 0

    def get(self, key):
        return self._data.get(key)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def __delitem__(self, key):
        del self._data[key]

    def __contains__(self, key):
        return key in self._data

    def store(self):
        self.store_calls += 1


class FakeAlbum:
    _next_id = 1

    def __init__(self, album, albumartist, year=0, items=None):
        self.id = FakeAlbum._next_id
        FakeAlbum._next_id += 1
        self.album = album
        self.albumartist = albumartist
        self.year = year
        self._items = list(items or [])
        self._data = {}
        self.store_calls = 0

    def items(self):
        return list(self._items)

    def get(self, key):
        return self._data.get(key)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def __delitem__(self, key):
        del self._data[key]

    def __contains__(self, key):
        return key in self._data

    def store(self):
        self.store_calls += 1
