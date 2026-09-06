"""pytest collection-time setup: install beets/spotipy/cachetools stubs once.

Importing beetsplug.spotify_album_match triggers __init__.py, which loads the
full plugin module. That requires beets/spotipy/cachetools at import time. We
inject lightweight stubs before pytest collects any test module that imports
from the package.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from plugin_test_utils import _ensure_stubs

_ensure_stubs()
