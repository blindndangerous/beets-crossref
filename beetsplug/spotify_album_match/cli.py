"""Subcommand registration, interactive prompt, progress tracking.

Keeps user-facing IO (input/print) and persistence concerns out of the matching/repair layers.
"""
import json
import logging
import os
import re

from beets.ui import Subcommand, decargs, colorize

try:
    from beets.ui import print_ as _ui_print
except ImportError:
    _ui_print = print  # tests stub beets.ui without print_

log = logging.getLogger("beets.spotify_album_match")


class UserAbort(Exception):
    """Raised when the user requests an abort from an interactive prompt."""


def build_subcommand(run_func):
    """Build the `spotify-album-match` Subcommand bound to *run_func*."""
    cmd = Subcommand(
        'spotify-album-match',
        help='Fetch Spotify track IDs by album (use --sid to supply a Spotify album ID)',
    )
    cmd.parser.add_option(
        '-d', '--dry-run', action='store_true',
        help='show matches but do not apply changes',
    )
    cmd.parser.add_option(
        '-i', '--interactive', action='store_true',
        help='ask for confirmation when uncertain',
    )
    cmd.parser.add_option(
        '-f', '--force', action='store_true',
        help='overwrite existing Spotify IDs',
    )
    cmd.parser.add_option(
        '-s', '--sid', dest='spotify_album_id',
        help='Spotify album URL/URI/ID to use for the matched album',
    )
    cmd.parser.add_option(
        '--debug', action='store_true',
        help='enable debug logging for spotify-album-match',
    )
    cmd.parser.add_option(
        '--resume', action='store_true',
        help='skip albums already processed in a previous run (uses a progress file)',
    )
    cmd.parser.add_option(
        '--progress-file', dest='progress_file', default=None, metavar='PATH',
        help='path to the progress file used by --resume',
    )
    cmd.parser.add_option(
        '--clear-progress', action='store_true',
        help='delete the progress file and start fresh',
    )
    cmd.func = run_func
    return cmd


def parse_args(args):
    return decargs(args) or None


SPOTIFY_ID_RE = re.compile(r"[A-Za-z0-9]{22}")
SPOTIFY_URI_RE = re.compile(r"spotify:album:([A-Za-z0-9]{22})")
SPOTIFY_URL_RE = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]{22})")


def extract_spotify_album_id(text):
    """Pull a 22-char Spotify album ID from a URI, URL, or bare string."""
    if not text:
        return None
    text = text.strip()
    match = SPOTIFY_URI_RE.search(text)
    if match:
        return match.group(1)
    match = SPOTIFY_URL_RE.search(text)
    if match:
        return match.group(1)
    if SPOTIFY_ID_RE.fullmatch(text):
        return text
    return None


# ---------------------------------------------------------------------------
# Progress file
# ---------------------------------------------------------------------------

def default_progress_path():
    """Return the default progress file path under the beets config dir.

    Honors $BEETSDIR (which beets itself uses) and falls back to ~/.config/beets/.
    """
    beetsdir = os.environ.get('BEETSDIR')
    if beetsdir:
        base = os.path.expanduser(beetsdir)
    else:
        base = os.path.expanduser(os.path.join('~', '.config', 'beets'))
    return os.path.join(base, 'spotify_album_match_progress.json')


def sanitize_progress_file_path(path):
    """Resolve and validate a progress file path.

    Rejects paths that don't end with .json to prevent accidental overwrites
    or reads of unrelated files via --progress-file. Returns the resolved
    absolute path, or None if invalid.
    """
    if not path:
        return None
    resolved = os.path.abspath(os.path.expanduser(path))
    if not resolved.lower().endswith('.json'):
        return None
    return resolved


def load_progress(progress_file):
    """Return the set of album IDs already processed, read from *progress_file*."""
    try:
        with open(progress_file, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return set(data)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read progress file: {exc}")
    return set()


def save_progress(progress_file, completed_ids):
    """Persist *completed_ids* to *progress_file* as a JSON list."""
    parent = os.path.dirname(progress_file)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            log.warning(f"Could not create progress file directory: {exc}")
            return
    try:
        with open(progress_file, 'w', encoding='utf-8') as fh:
            json.dump(sorted(completed_ids), fh)
    except OSError as exc:
        log.warning(f"Could not write progress file: {exc}")


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------

class InteractivePrompter:
    """Callable that asks the user to pick an album candidate.

    Signature when called: (candidates, local_album, local_items, build_candidate_fn) -> candidate | None.

    Raises UserAbort if the user picks "abort" or hits Ctrl+C.
    """

    def __init__(self, on_abort=None):
        """`on_abort`, if provided, is called before raising UserAbort."""
        self._on_abort = on_abort

    def __call__(self, candidates, local_album, local_items, build_candidate_fn):
        if len(candidates) == 1:
            score = candidates[0]["score"]
            log.info(
                f"Only one uncertain match found (Score: {score:.0%}). "
                "Please confirm or enter a different album ID."
            )

        _ui_print("")
        display_candidates = candidates[:5]
        for i, candidate in enumerate(display_candidates, 1):
            _ui_print(self._format_candidate_line(i, candidate))

        while True:
            prompt_str = (
                f"Choose a number (1-{len(display_candidates)}), "
                f"({colorize('red', 'S')})kip, ({colorize('red', 'B')})abort, "
                f"or ({colorize('green', 'I')})nput album ID/URL: "
            )
            try:
                choice = input(prompt_str)
            except EOFError:
                _ui_print("\nSkipping.")
                return None
            except KeyboardInterrupt:
                _ui_print("\nAborting.")
                self._abort()

            choice = choice.strip()
            if choice.lower() == 's':
                return None
            if choice.lower() == 'b':
                self._abort()
            if choice.lower().startswith('i '):
                raw = choice[2:].strip()
            elif choice.lower() == 'i':
                try:
                    raw = input("Enter Spotify album URL/URI/ID: ").strip()
                except (EOFError, KeyboardInterrupt):
                    _ui_print("\nSkipping.")
                    return None
            else:
                raw = None

            if raw is not None:
                album_id = extract_spotify_album_id(raw)
                if not album_id:
                    _ui_print(colorize('red', 'Could not parse a Spotify album ID. Please try again.'))
                    continue
                candidate = build_candidate_fn(album_id, local_album, local_items)
                if not candidate:
                    _ui_print(colorize('red', 'Could not load that album ID. Please try again.'))
                    continue
                log.info(f"Using user-provided Spotify album ID: {album_id}")
                return candidate

            try:
                num_choice = int(choice)
                if 1 <= num_choice <= len(display_candidates):
                    return display_candidates[num_choice - 1]
                _ui_print(colorize('red', 'Invalid number. Please try again.'))
            except ValueError:
                _ui_print(colorize(
                    'red',
                    f"'{choice}' is not a valid number, 's', or 'i'. Please try again.",
                ))

    @staticmethod
    def _format_candidate_line(i, candidate):
        score = candidate["score"]
        album = candidate["album"]
        track_count = candidate.get("track_count", 0)
        is_variant = candidate.get("is_variant", False)
        popularity = candidate.get("popularity")
        pop_text = f"pop {popularity}" if popularity is not None else "pop n/a"
        artist_name = album.get('artists', [{}])[0].get('name', 'Unknown artist')
        release_date = album.get('release_date', 'unknown')
        album_name = album.get('name', 'Unknown album')
        variant_tag = " [variant]" if is_variant else ""
        score_color = 'green' if score > 0.7 else 'yellow'
        return (
            f"  {colorize('blue', str(i))}: "
            f"({colorize(score_color, f'{score:.0%}')}) "
            f"{artist_name} - {album_name}{variant_tag} "
            f"[{track_count} tracks, {release_date}, {pop_text}]"
        )

    def _abort(self):
        if self._on_abort is not None:
            self._on_abort()
        raise UserAbort("Aborted by user.")
