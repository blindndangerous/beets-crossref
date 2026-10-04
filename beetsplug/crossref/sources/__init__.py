"""Source adapters, by the name used in config and on the command line."""

from __future__ import annotations

from importlib import import_module

# name -> "module:Class", imported only when the source is used.
REGISTRY = {
    "spotify": "spotify:SpotifySource",
    "deezer": "deezer:DeezerSource",
    "itunes": "itunes:ItunesSource",
    "discogs": "discogs:DiscogsSource",
    "lastfm": "lastfm:LastfmSource",
}


def load(name: str):
    module, cls = REGISTRY[name].split(":")
    return getattr(import_module(f"{__name__}.{module}"), cls)
