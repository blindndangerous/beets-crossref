# beets-crossref

Beets plugin: finds each album on Spotify, Deezer, Apple (iTunes) and Discogs,
then fetches fields from those services and Last.fm. Replaces the earlier
`spotify_album_match` and `lastfmpop` plugins.

## User

- Blind, screen reader user
- No `|` tables — use lists
- Output from tools and docs must be plain, line-oriented, color-free text
- No backward compatibility: old config keys and fields are not migrated

## Stack

- Python >= 3.10, beets >= 2.2 (developed against beets 2.14.1)
- `requests` for every service, through `http.JsonClient`
- `thefuzz` for Spotify's fuzzy fallback (`helpers.py`)
- `pytest`, `ruff` (config in `pyproject.toml`)

## Test

```bash
uv sync --extra test
uv run pytest -q
uv run ruff check beetsplug tests
```

No test touches the network: sources get their `client.get` monkeypatched.
`beets.library.Library(":memory:")` fails on beets 2.14.1 (its migration
backup opens a file), so tests use a library file under `tmp_path`.

## Layout

- `beetsplug/crossref/plugin.py` — command, resolve and fetch loops, write policy, field types
- `evidence.py` — evidence ranking and the `<field>_source` record
- `tracks.py` — `TrackHit`, `match_tracks` (ISRC + slot/duration, then slot + duration)
- `musicbrainz.py` — release URL relationships
- `cache.py` — SQLite cache; `MISSING` means a failed request and is never stored
- `http.py` — rate-limited `JsonClient`, `SourceUnavailable` (has `.status`)
- `helpers.py` — Spotify ID validation and fuzzy scoring
- `sources/base.py` — the `Source` contract and `Updates`
- `sources/spotify.py`, `deezer.py`, `itunes.py`, `discogs.py`, `lastfm.py`
- `tests/` — one file per module or source

## Design rules

- Database only: never write tags or move files (tag writes change file mtimes, which other tools watch).
- Store albums with `album.store(inherit=False)`; a plain `store()` copies album
  flexible fields onto every item. Fill fixed fields on items explicitly.
- Read an object's own flexible value with `obj._values_flex.get(...)`: an
  item's `get()` falls back to its album's value.
- Evidence order: musicbrainz > barcode > isrc > fuzzy. A stored ID is replaced
  only by stronger evidence. ISRC and fuzzy need >= half the tracks to line up.
- Fixed fields `style`, `label`, `catalognum`, `barcode` are filled only when
  empty. Never set `genre`.
- Spotify: no batch endpoints (`/albums?ids=`), which Spotify is removing for
  development-mode apps. Audio features are beets' `spotifysync`'s job.
- Cache `None` for "not on this service"; never cache `MISSING`.
