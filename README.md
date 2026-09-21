# beets-spotify-album-match

A [beets](https://beets.io/) plugin that matches the albums and tracks in your
library to Spotify and stores the resulting Spotify IDs on your items.

For each album it searches Spotify (by title and artist, or by barcode first if
you turn on the experimental `use_upc_lookup`), scores the candidates with a weighted fuzzy match, picks a
winner, and then maps each local track onto a Spotify track. On later runs it
verifies the IDs it already stored, repairs the ones that drifted (bonus tracks,
deluxe editions), and clears the ones that are wrong.

## Fields written

- `spotify_album_id` — album-level flexible field, the 22-character Spotify album ID
- `spotify_track_id` — item-level flexible field, the 22-character Spotify track ID
- `spotify_artist_id` — written on both albums and items, the 22-character Spotify ID
  of the primary artist. On an album it is the primary artist of the matched Spotify
  album; on an item it is the primary artist of the matched Spotify track, so tracks on
  a compilation each keep their own artist. Only the primary artist is stored, never a
  list.

All three are registered as beets `STRING` types, so you can query and use them in
path formats, for example `beet ls spotify_album_id::.` or
`beet ls -a spotify_album_id:4aawyAB9vmqN3uQ7FjRGTy`.

An artist ID is only ever written alongside the album or track ID it belongs to, and is
deleted whenever that ID is cleared. If Spotify returns no usable primary artist, the
album or track ID is still stored and any artist ID already on that album or track is
removed, so a stored artist ID always belongs to the album or track ID sitting beside it.

Holding that invariant needs one thing from beets: by default `album.store()` copies
every flexible field the album just changed into each of its items, and cascades
deletions the same way, which would give every track the album's artist ID. The plugin
therefore stores albums with `album.store(inherit=False)`, so an album write only ever
touches the album. This is why beets 2.2 or newer is required: earlier releases have no
`inherit` argument. Item-level queries such as
`beet ls spotify_album_id:4aawyAB9vmqN3uQ7FjRGTy` still work, because beets lets an item
read its album's value for a field the item does not have.

If your library was matched by an earlier version of this plugin, some tracks already own
a copy of their album's artist ID, written before this was fixed. Nothing heals those
automatically: the plugin cannot tell a copied value from one a track earned. Run once
with `-f` to re-match and rewrite them.

## Backfilling artist IDs

Albums matched before `spotify_artist_id` existed are filled in without being
re-matched. When an album has a stored `spotify_album_id` or stored
`spotify_track_id`s but is missing the matching artist IDs of its own, the plugin looks
up the stored album and its track list and writes the artist IDs from them, then logs
one line such as
`Backfilled artist IDs for 'Portishead - Dummy': album=yes, tracks=12`. Tracks are
backfilled with their own artist, never with the album's: beets lets a track read its
album's value for a field it does not have, and the backfill ignores that so a
compilation still ends up with one artist ID per track. A track that is not on the stored
album (a bonus track matched from a related release, say) is resolved in a single bulk
request. No search is performed, nothing is re-matched, and an album that already has all
of its artist IDs costs zero API calls. Under `--dry-run` nothing is written.

Backfilling only avoids a fresh search for the IDs that are already stored; it is not a
substitute for a run. Once it is done the normal match path continues as usual, so an
album that is not yet fully matched is still searched, matched and repaired in the same
run.

On a later run a stored album ID is re-checked by title and artist, and each
stored track ID by its position on the album. ISRCs are compared only where
Spotify supplies them, which is on track search results: the track list of an
album comes back as simplified objects with no ISRC. Durations are used when
scoring a match, not when verifying one.

IDs are validated before they are used or stored. Anything that is not exactly
22 base62 characters is treated as absent: it is never sent to the Spotify API,
and a stored value that fails validation is deleted from the library rather than
left behind as an empty string.

## Install

Requires beets 2.2 or newer.

From a checkout:

```bash
pip install -e .
```

Or copy the package directory into your beets plugin path:

```bash
cp -r beetsplug/spotify_album_match ~/.config/beets/beetsplug/
```

Then enable it in `config.yaml`:

```yaml
plugins: spotify_album_match
spotify_album_match:
  client_id: YOUR_SPOTIFY_CLIENT_ID
  client_secret: YOUR_SPOTIFY_CLIENT_SECRET
```

Credentials come from a Spotify developer application; the plugin uses the
client-credentials flow, so no user login or redirect URI is needed.

## Commands

The plugin adds one subcommand:

```bash
beet spotify-album-match [OPTIONS] [QUERY]
```

`QUERY` is an ordinary beets query limiting which albums are processed. With no
query, every album in the library is processed.

Options:

- `-d`, `--dry-run` — show what would change without writing anything
- `-i`, `--interactive` — prompt you to choose when a match is uncertain
- `-f`, `--force` — overwrite Spotify IDs that are already stored
- `-s ID`, `--sid ID` — use a specific Spotify album ID, URI, or open.spotify.com URL for the matched album. This always overwrites the album's stored IDs, with or without `-f`
- `--resume` — skip albums already recorded as done in the progress file
- `--progress-file PATH` — path to the progress file (must end in `.json`)
- `--clear-progress` — delete the progress file and start fresh
- `--debug` — verbose debug logging for this plugin

The progress file defaults to `$BEETSDIR/spotify_album_match_progress.json`,
falling back to `~/.config/beets/spotify_album_match_progress.json` when
`BEETSDIR` is unset.

In interactive mode you get a numbered list of candidates and can pick a number,
`s` to skip the album, `b` to abort the run, or `i` to type a Spotify album
ID/URL yourself.

## Configuration

All keys go under `spotify_album_match:` in `config.yaml`. Defaults are shown in
parentheses.

Authentication:

- `client_id` (none) — Spotify API client ID, required
- `client_secret` (none) — Spotify API client secret, required

API and rate limiting:

- `min_request_interval` (`3.0`) — minimum seconds between Spotify API calls
- `max_retries` (`5`) — retry attempts on transient errors: HTTP 5xx, timeouts and connection failures
- `retry_delay` (`5`) — base seconds between retries
- `stop_on_rate_limit` (`true`) — abort the run on HTTP 429; set `false` to honour `Retry-After` and continue
- `cache_ttl` (`600`) — seconds to keep Spotify responses in the in-memory cache

Album matching:

- `match_threshold` (`0.90`) — minimum composite score for an automatic match
- `certainty_margin` (`0.15`) — score gap that makes the top candidate a clear winner even below `match_threshold`
- `max_album_candidates` (`3`) — how many search results are scored in detail
- `max_album_popularity_checks` (`1`) — how many candidates get a full details fetch so their popularity is known (0 disables). Candidates are ranked by score, and popularity only separates two scores that are exactly equal
- `related_artist_threshold` (`0.90`) — minimum artist score for a result to count as a related or variant release
- `min_related_release_artist_score` (`0.85`) — minimum artist score when collecting related releases for bonus tracks
- `min_preliminary_artist_score` (`0.20`) — hard floor at the quick-filter stage, applied before any track-level API calls
- `use_upc_lookup` (`false`) — **experimental.** Search by the album's `barcode` field before
  searching by title and artist. The single hit is accepted only if it scores at least
  `existing_album_validation_threshold` on title and artist, but that gate is weaker than it
  looks: title scoring uses a partial ratio, so a superset title such as "Greatest Hits"
  scores a perfect match against "Hits", and an album with an empty `albumartist` scores 0
  on artist and can never pass. Try it with `--dry-run` before trusting it

Track matching:

- `track_match_threshold` (`0.90`) — minimum score for a track-to-track match
- `min_track_artist_score` (`0.90`) — minimum artist score when matching tracks inside an album
- `duration_tolerance` (`3`) — acceptable duration difference in seconds
- `duration_mismatch_penalty_threshold` (`10`) — duration difference in seconds above which a penalty applies
- `duration_mismatch_penalty` (`0.10`) — score penalty applied past that threshold

Verifying IDs that are already stored:

- `verify_existing_ids` (`true`) — re-validate stored Spotify IDs on each run
- `existing_album_validation_threshold` (`0.90`) — minimum title and artist score for a stored album ID to be trusted
- `existing_id_mismatch_threshold` (`0.3`) — fraction of mismatched track IDs that triggers a fresh album search
- `existing_album_repair_strategy` (`related_release`) — either `strict` or `related_release`

Clearing stale IDs:

- `clear_unmatched_track_ids` (`true`) — clear `spotify_track_id` on tracks left unmatched after a successful album match
- `clear_on_no_match` (`true`) — clear album and track IDs when nothing matches at all

## Development

Run the test suite from the repository root:

```bash
python -m pytest -q
```

The suite stubs `beets`, `spotipy`, and `cachetools`, so it runs without beets
installed and without touching the network. `requests` and `thefuzz` are used
for real.

Lint with the repository's own rule set:

```bash
python -m ruff check beetsplug tests
```

CI runs both on Python 3.10 and 3.13, and additionally imports the package
against the real dependencies so a broken import cannot pass on stubs alone.

## License

MIT. See `LICENSE`.
