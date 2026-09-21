# spotify_album_match

A beets plugin that matches local albums and tracks to Spotify, writing `spotify_album_id`, `spotify_track_id` and `spotify_artist_id` fields to the beets library.

## User

- Blind, screen reader user
- No `|` tables — use lists
- Output from tools and docs must be plain, line-oriented, color-free text

## Stack

- Python 3 (developed against 3.13, `requires-python >= 3.10`, the floor beets 2.2 needs)
- beets plugin API (`BeetsPlugin`, `Subcommand`); beets >= 2.2 (needs `Album.store(inherit=...)`)
- Spotify Web API via `spotipy`
- `thefuzz` for fuzzy matching (a hard dep)
- `pytest` for tests; `beets` and `spotipy` are stubbed in the suite, `requests`
  and `thefuzz` are real
- `ruff` for lint, configured in `pyproject.toml`: `python -m ruff check beetsplug tests`

## Run

Install for development:

```bash
pip install -e .
```

Or drop the `beetsplug/spotify_album_match/` directory into your beets plugin path
(e.g. `~/.config/beets/beetsplug/spotify_album_match/`).

Configuration keys, defaults, CLI options and the scoring weights are documented in README.md; keep that file authoritative.

## Test

```bash
python -m pytest -q
```

`beets` does not need to be installed: `tests/conftest.py` installs stub
`beets` and `spotipy` modules before collection.

## Layout

- `beetsplug/spotify_album_match/` — the plugin package
- `tests/` — pytest suite, one test file per module
- `pyproject.toml` — packaging (`beets-spotify-album-match`) and pytest config
- `README.md` — user-facing docs
- `LICENSE` — MIT
- `CLAUDE.md` — this file

Package modules:

- `__init__.py` — exposes `SpotifyAlbumMatchPlugin`
- `plugin.py` — slim orchestrator: config, top-level workflow, ID clearing
- `cli.py` — Subcommand registration, options, progress file, interactive prompter
- `matching.py` — `AlbumMatcher`: search, candidate building/selection, track matching
- `repair.py` — `AlbumRepairer`: verify existing IDs, repair from related releases
- `client.py` — `SpotifyClient`: caching, rate-limiting, retries (single-threaded)
- `helpers.py` — beets-free helpers (importable without beets): fuzzy scoring, query
  building, `clean_spotify_id`, and the artist-ID field writers

Test files:

- `test_plugin.py`, `test_matching.py`, `test_repair.py`, `test_cli.py`,
  `test_client.py`, `test_helpers.py`, `test_spotify_id_safety.py`,
  `test_artist_id.py`
- `fakes.py` — shared `FakeItem` / `FakeAlbum`, modelling the beets album fallback
  (an item's `get` / `in` / `[]` fall through to its album; `del` does not)
- `plugin_test_utils.py` — beets/spotipy stubs
- `conftest.py` — installs stubs before collection

## How Matching Works

1. Album search — build multiple query variants (full title+artist, stripped title, primary artist) and collect distinct results.
2. Candidate scoring — weighted composite (see README.md).
3. Selection — accept if `score >= match_threshold` or clear winner by `certainty_margin`. Prefer standard editions over variants when scores close. `--interactive` to pick manually.
4. Track matching — match each local track to the winning album's tracks via fuzzy title+artist+duration.
5. Related-release repair — unmatched tracks (e.g. bonus) searched against variant/deluxe editions.
6. Verification on subsequent runs — validate stored album by title+artist; if wrong, clear
   and re-search. Otherwise verify track positions and repair only what's off. Durations are
   scored, not verified.

## Key Design Notes

- Matching uses fuzzy title scoring + artist set scoring; see `helpers.py`
- `RateLimitAbort` raised by client when Spotify rate-limits; caller should back off
- Album-level match written first; track-level match is a second pass
- `AlbumMatcher.prompter` is a callable injected at construction; in production it is
  `cli.InteractivePrompter`, in tests it is replaced with a stub
- `AlbumRepairer` calls back into the plugin via the `id_clearer` interface
  (`clear_all_ids` / `clear_track_ids`) — keeps the dependency one-way

## Spotify ID Hygiene

- `helpers.clean_spotify_id(value)` is the single validator: it strips whitespace and
  returns the value only if it matches `SPOTIFY_ID_PATTERN` (22 base62 chars), else `None`.
- `SpotifyClient` cleans every ID before an API call. Single lookups return `[]`/`None`
  and log a warning; `get_albums_bulk` drops and dedupes malformed IDs.
- Clearing an ID `del`s the flexible field rather than storing `''`, so beets does not
  keep a blank attribute around that later looks like a stored ID.
- `SpotifyAlbumMatchPlugin._clear_malformed_stored_ids` runs at the top of every album's
  processing, deleting blank/malformed stored IDs before they reach Spotify.
- Store albums with `album.store(inherit=False)`. A plain `album.store()` pushes every
  flexible field the album just changed into each of its items and cascades deletions
  (beets 2.2.0 `library.py:1494-1527`), which would overwrite each track's own
  `spotify_artist_id` with the album's. Items are stored with plain `item.store()` --
  `Item.store()` has no `inherit` parameter.
- `spotify_artist_id` is registered on albums AND items, and beets makes an item read
  its album's value for a field the item does not have (`get`, `in`, `[]` — but not
  `del`). So never test an item's artist ID with `item.get(...)` or `in`: use
  `helpers.own_artist_id(obj)`, which reads the object's own `_values_flex`. Deleting
  goes through `helpers.discard_artist_id(obj)`, which swallows the `KeyError` beets
  raises when the value seen through `in` actually belongs to the album.
