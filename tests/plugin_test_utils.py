"""Test infrastructure: stub beets/spotipy so the plugin can be imported
without the real packages, then load the package once per process.
"""
import importlib
import pathlib
import sys
import types

# ---------------------------------------------------------------------------
# Stub beets and spotipy
# ---------------------------------------------------------------------------

class ConfigTypeError(Exception):
    """Stands in for confuse.ConfigTypeError."""


class _ConfigValue:
    """A confuse view over one key.

    Casts raise like confuse does: a value that cannot be cast is a
    configuration error, not a silent 0.0, which would leave the suite running
    against thresholds no real run could ever have.
    """

    def __init__(self, data, key):
        self._data = data
        self._key = key

    def get(self, cast=None):
        value = self._data.get(self._key)
        if cast is None:
            return value
        if cast is bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        try:
            return cast(value)
        except (TypeError, ValueError) as exc:
            raise ConfigTypeError(f"{self._key}: {value!r} is not a {cast.__name__}") from exc

    def as_number(self):
        value = self._data.get(self._key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigTypeError(f"{self._key}: {value!r} is not a number")
        return float(value)


class _Config:
    def __init__(self):
        self.data = {}

    def add(self, values):
        for key, value in values.items():
            self.data.setdefault(key, value)

    def __getitem__(self, key):
        return _ConfigValue(self.data, key)


def _build_stub_modules():
    beets_module = types.ModuleType("beets")
    beets_plugins = types.ModuleType("beets.plugins")
    beets_ui = types.ModuleType("beets.ui")
    beets_dbcore = types.ModuleType("beets.dbcore")
    beets_dbcore_types = types.ModuleType("beets.dbcore.types")

    class _StringType:
        pass

    beets_dbcore_types.STRING = _StringType()
    beets_dbcore.types = beets_dbcore_types

    class BeetsPlugin:
        def __init__(self, name=None):
            self.name = name
            self.config = _Config()

    class _Parser:
        def __init__(self):
            self.options = []

        def add_option(self, *args, **kwargs):
            self.options.append((args, kwargs))

    class Subcommand:
        def __init__(self, name, help=None):
            self.name = name
            self.help = help
            self.parser = _Parser()
            self.func = None

    def decargs(args):
        if args is None:
            return None
        if isinstance(args, list):
            return " ".join(str(arg) for arg in args)
        return str(args)

    def colorize(_color, text):
        return text

    def print_(*args, **kwargs):
        # mirror real beets.ui.print_ semantics for tests; route to print()
        print(*args, **kwargs)

    beets_plugins.BeetsPlugin = BeetsPlugin
    beets_ui.Subcommand = Subcommand
    beets_ui.decargs = decargs
    beets_ui.colorize = colorize
    beets_ui.print_ = print_
    beets_module.plugins = beets_plugins
    beets_module.ui = beets_ui
    beets_module.dbcore = beets_dbcore

    spotipy_module = types.ModuleType("spotipy")
    spotipy_oauth2 = types.ModuleType("spotipy.oauth2")
    spotipy_exceptions = types.ModuleType("spotipy.exceptions")
    spotipy_cache_handler = types.ModuleType("spotipy.cache_handler")

    class Spotify:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class SpotifyClientCredentials:
        def __init__(self, client_id=None, client_secret=None, cache_handler=None):
            self.client_id = client_id
            self.client_secret = client_secret
            # Real spotipy defaults this to CacheFileHandler(), which writes the
            # token to ".cache" in the current directory (oauth2.py:182,
            # cache_handler.py:69).
            self.cache_handler = cache_handler

    class SpotifyException(Exception):
        def __init__(self, http_status=None, code=None, msg="", headers=None):
            super().__init__(msg)
            self.http_status = http_status
            self.code = code
            self.msg = msg
            self.headers = headers or {}

    class MemoryCacheHandler:
        def __init__(self, token_info=None):
            self.token_info = token_info

    spotipy_module.Spotify = Spotify
    spotipy_oauth2.SpotifyClientCredentials = SpotifyClientCredentials
    spotipy_exceptions.SpotifyException = SpotifyException
    spotipy_cache_handler.MemoryCacheHandler = MemoryCacheHandler

    return {
        "beets": beets_module,
        "beets.plugins": beets_plugins,
        "beets.ui": beets_ui,
        "beets.dbcore": beets_dbcore,
        "beets.dbcore.types": beets_dbcore_types,
        "spotipy": spotipy_module,
        "spotipy.oauth2": spotipy_oauth2,
        "spotipy.exceptions": spotipy_exceptions,
        "spotipy.cache_handler": spotipy_cache_handler,
    }


# Stubs are installed once on first import; subsequent loads reuse them.
_STUBS_INSTALLED = False
_LOADED_PACKAGE = None


def _ensure_stubs():
    global _STUBS_INSTALLED
    if _STUBS_INSTALLED:
        return
    sys.modules.update(_build_stub_modules())
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    _STUBS_INSTALLED = True


def load_package():
    """Return the spotify_album_match package, loading it once per process."""
    global _LOADED_PACKAGE
    if _LOADED_PACKAGE is not None:
        return _LOADED_PACKAGE
    _ensure_stubs()
    _LOADED_PACKAGE = importlib.import_module("beetsplug.spotify_album_match")
    return _LOADED_PACKAGE


def fresh_plugin():
    """Return a freshly-instantiated plugin (config + client + matcher + repairer)."""
    pkg = load_package()
    return pkg.SpotifyAlbumMatchPlugin()

