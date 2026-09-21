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
- `cachetools` for TTL caches, `thefuzz` for fuzzy matching (both hard deps)
- `pytest` for tests; `beets`/`spotipy`/`cachetools` are stubbed in the suite, `requests`
  and `thefuzz` are real
- `ruff` for lint, configured in `pyproject.toml`: `python -m ruff check beetsplug tests`

## Run

Install for development:

```bash
pip install -e .
```

Or drop the `beetsplug/spotify_album_match/` directory into your beets plugin path
(e.g. `~/.config/beets/beetsplug/spotify_album_match/`).

Enable and configure in `config.yaml`:

```yaml
plugins: spotify_album_match
spotify_album_match:
  client_id: ...
  client_secret: ...
```

Then run:

```bash
beet spotify-album-match [OPTIONS] [QUERY]
  -d / --dry-run       show changes without writing
  -i / --interactive   prompt on uncertain matches
  -f / --force         overwrite existing Spotify IDs
  -s / --sid ID        supply a specific Spotify album ID/URL
  --resume             skip albums already processed in a previous run
  --progress-file PATH path to the progress file used by --resume
  --clear-progress     delete the progress file and start fresh
```

Verbose logging comes from beets: `beet -v spotify-album-match`.

Progress file defaults to `$BEETSDIR/spotify_album_match_progress.json` (or
`~/.config/beets/spotify_album_match_progress.json` if `BEETSDIR` is unset).

## Test

```bash
python -m pytest -q
```

`beets` does not need to be installed: `tests/conftest.py` installs stub
`beets`/`spotipy`/`cachetools` modules before collection.

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
- `client.py` — `SpotifyClient`: caching, rate-limiting, retries
- `helpers.py` — beets-free helpers (importable without beets): fuzzy scoring, query
  building, `clean_spotify_id`, and the artist-ID field writers

Test files:

- `test_plugin.py`, `test_matching.py`, `test_repair.py`, `test_cli.py`,
  `test_client.py`, `test_helpers.py`, `test_spotify_id_safety.py`,
  `test_artist_id.py`
- `fakes.py` — shared `FakeItem` / `FakeAlbum`, modelling the beets album fallback
  (an item's `get` / `in` / `[]` fall through to its album; `del` does not)
- `plugin_test_utils.py` — beets/spotipy/cachetools stubs
- `conftest.py` — installs stubs before collection

## Configuration Reference

All keys go under `spotify_album_match:` in `config.yaml`.

### Authentication

- `client_id` — required. Spotify API client ID.
- `client_secret` — required. Spotify API client secret.

### API / Rate Limiting

- `min_request_interval` (default `3.0`) — minimum seconds between Spotify API calls.
- `max_retries` (default `5`) — retry attempts on transient errors (5xx, timeouts,
  connection failures). The client is built with an explicit `requests.Session` so spotipy
  installs no Retry adapter of its own; without that every 5xx arrives as a header-less 429.
- `retry_delay` (default `5`) — base seconds between retries.
- `stop_on_rate_limit` (default `true`) — on HTTP 429, abort the run. Set `false` to wait Retry-After and continue.
- `cache_ttl` (default `600`) — seconds to cache Spotify API responses in memory.

### Album Matching

- `match_threshold` (default `0.90`) — minimum composite score (0–1) for automatic acceptance. Below this needs `--interactive` or is skipped.
- `certainty_margin` (default `0.15`) — score gap between top two candidates that declares a clear winner even if below `match_threshold`.
- `max_album_candidates` (default `3`) — max album search results scored in detail.
- `max_album_popularity_checks` (default `1`) — how many candidates to fetch full details
  for so popularity is known (0 disables). Sorting is by score first, so popularity only
  decides an exact tie.
- `related_artist_threshold` (default `0.90`) — minimum artist fuzzy score for a result to count as a related/variant release rather than filtered.
- `min_related_release_artist_score` (default `0.85`) — minimum artist score when collecting supplemental related releases for bonus tracks.
- `min_preliminary_artist_score` (default `0.20`) — hard floor at the quick-filter stage; below this is dropped before track-level API calls.

### Track Matching (album-level)

- `track_match_threshold` (default `0.90`) — minimum score (0–1) for a track-to-track fuzzy match within an album.
- `min_track_artist_score` (default `0.90`) — minimum artist score when matching tracks against an album's track list.
- `duration_tolerance` (default `3`) — seconds of acceptable duration difference.
- `duration_mismatch_penalty_threshold` (default `10`) — duration difference above which a penalty applies.
- `duration_mismatch_penalty` (default `0.10`) — score penalty when diff exceeds threshold.

### Composite Scoring (100% = perfect)

Album score weights (sum to 1.0):

- title (0.45) — average fuzzy of local-vs-Spotify track titles
- album_title (0.20) — fuzzy of album name
- artist (0.15) — fuzzy artist-set score
- track_count (0.10) — 1 − relative count delta
- album_type (0.05) — 1.0 album, 0.7 comp, 0.5 single, 0.6 other
- year (0.05) — 1.0 same year, 0.5 within ±1, else 0

### Verification of Existing IDs

- `verify_existing_ids` (default `true`) — validate stored Spotify IDs before accepting.
- `existing_album_validation_threshold` (default `0.90`) — minimum title*0.6 + artist*0.4 score for stored album to be trusted.
- `existing_id_mismatch_threshold` (default `0.3`) — max fraction of mismatched track IDs before whole album is re-searched.
- `existing_album_repair_strategy` (default `related_release`) — either `strict` or `related_release`.

### Clearing Stale IDs

- `clear_unmatched_track_ids` (default `true`) — clear `spotify_track_id` for tracks that could not be matched after a successful album match.
- `clear_on_no_match` (default `true`) — clear all Spotify IDs (album + tracks) when no match at all.

## How Matching Works

1. Album search — build multiple query variants (full title+artist, stripped title, primary artist) and collect distinct results.
2. Candidate scoring — weighted composite (see Composite Scoring above).
3. Selection — accept if `score >= match_threshold` or clear winner by `certainty_margin`. Prefer standard editions over variants when scores close. `--interactive` to pick manually.
4. Track matching — match each local track to the winning album's tracks via fuzzy title+artist+duration.
5. Related-release repair — unmatched tracks (e.g. bonus) searched against variant/deluxe editions.
6. Verification on subsequent runs — validate stored album by title+artist; if wrong, clear
   and re-search. Otherwise verify track positions and repair only what's off. ISRC comparison
   is written for full track objects; an album's track list is simplified objects with no
   `external_ids`, so on that path only position is checked. Durations are scored, not verified.

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
  and log a warning; bulk lookups drop and dedupe malformed IDs.
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
