# beets-crossref

A beets plugin that finds each album in your library on Spotify, Deezer,
Apple's iTunes store and Discogs, then fetches what those services and
Last.fm know about it.

- `beet crossref resolve` fills in album and track IDs.
- `beet crossref fetch` uses those IDs to fill in fields: Discogs style, label
  and catalogue number, Deezer BPM, gain and rank, Apple genre and explicit or
  clean flags, and Last.fm play counts for tracks, albums and artists.
- `beet crossref` on its own does both, in that order.

It writes to the beets database only and never touches your audio files. Run
`beet write` if you want the standard fields in your tags.

Once the IDs are in, beets' own sync commands can fill in more. `beet
spotifysync` adds Spotify popularity, and `beet mbsync` refreshes
MusicBrainz data. The Spotify note further down explains what happened to
audio features.

## Requirements

- beets 2.2 or later
- Python 3.10 or later
- `requests` and `thefuzz`, which the install commands below pull in

## Install

Install into the Python environment beets runs in:

```bash
pip install git+https://github.com/blindndangerous/beets-crossref
```

If beets lives in a uv project, use this instead:

```bash
uv add git+https://github.com/blindndangerous/beets-crossref
```

You can also copy the `beetsplug/crossref/` folder into your beets plugin
path, for example `~/.config/beets/beetsplug/crossref/`, and set
`pluginpath`. In that case install `requests` and `thefuzz` into beets'
environment yourself.

Then add `crossref` to `plugins` in your beets config.

## Configure

```yaml
crossref:
    sources: [spotify, deezer, itunes, discogs, lastfm]   # the default; remove one to switch it off
    cache_days: 30          # how long answers are reused; 0 keeps them forever
    cache: ""               # default: crossref_cache.db in the beets config folder
    spotify:
        client_id: YOUR_ID
        client_secret: YOUR_SECRET
    discogs:
        token: YOUR_PERSONAL_ACCESS_TOKEN   # or, instead:
        key: YOUR_APP_CONSUMER_KEY
        secret: YOUR_APP_CONSUMER_SECRET
    itunes:
        country: US
    lastfm:
        apikey: YOUR_KEY    # or set the LASTFM_API_KEY environment variable
        min_interval: 0.5
        match_ratio: 0.85
        prefer_mbid: yes
        max_age_days: 30    # skip tracks, albums and artists fetched more recently
```

Deezer and Apple need no credentials. crossref skips a source that has no
credentials, or still has one of the `YOUR_...` placeholders above, prints a
warning, and carries on with the others.

Where to get credentials:

- Spotify: create an app at https://developer.spotify.com/dashboard and copy
  its client ID and secret. Since 2026, Spotify only runs development-mode
  apps for owners with a Premium account, and all of an owner's
  development-mode apps share one request quota. When Spotify says that quota
  is used up, crossref stops asking Spotify for the rest of the run.
- Discogs: on discogs.com, open Settings, then Developers. Generate a personal
  access token, or create an app and use its consumer key and secret.
- Last.fm: register a key at https://www.last.fm/api/account/create.

crossref only uses Spotify to search and read albums. `beet spotifysync` is
beets' own command. It fills popularity, and audio features where Spotify
still serves them. Spotify withdrew audio features from apps created after
27 November 2024, so if you give beets' `spotify` plugin keys from a newer
app, expect popularity only.

## Rate limits

Each source waits between requests to stay under the service's limit, and
honours `Retry-After`.

- MusicBrainz: 1 request a second
- Deezer: about 8 a second
- Apple: 1 every 3 seconds
- Discogs: 1 a second
- Last.fm: 2 a second
- Spotify: 1 a second

If a service asks crossref to wait more than two minutes, or refuses its
credentials, crossref stops using that source for the rest of the run and
carries on with the others. Apple is by far the slowest, and a large library
takes many hours. MusicBrainz counts requests per address, so don't run two
programs that query it from the same network at once.

## Use

```bash
beet crossref                             # resolve, then fetch, every album
beet crossref resolve                     # IDs only
beet crossref resolve -s deezer,itunes    # only these sources this time
beet crossref resolve -p artist:Muse      # pretend: show what would change
beet crossref fetch
beet crossref fetch -s lastfm added:2026-10-01..
```

Every command takes a normal beets album query. crossref caches answers, so a
second run costs almost no requests, and opening the cache deletes expired
answers. You can stop with Ctrl+C at any time. crossref saves each album as
soon as it finishes it.

## How IDs are found

For each album and source, crossref tries these in order and stops at the
first that works:

1. MusicBrainz links from the release to the same album on that service
2. A lookup of the album's barcode, UPC or EAN
3. Tracks with the album's ISRC recording codes, on Spotify and Deezer
4. A fuzzy search by artist and title, on Spotify only

crossref compares each candidate's tracklist with your tracks by disc,
position and length, allowing 3 seconds either way, and by ISRC where both
sides have one. It keeps an ISRC or fuzzy match only when at least half your
tracks line up, and a barcode match only when at least one does. When several
candidates qualify, the one that lines up the most tracks wins, so a deluxe
edition beats the standard one.

Each ID records how it was found in a sibling field, for example
`deezer_album_id_source: barcode`. crossref replaces a stored ID only with
stronger evidence, in the order above, and only with an album that lines up
at least as many of your tracks. IDs stored by anything else count as fuzzy.

One barcode can belong to several releases, such as an explicit and a clean
edition, and each service returns just one of them. A MusicBrainz link avoids
the problem.

## Fields

Album IDs are `spotify_album_id`, `deezer_album_id`, `itunes_album_id` and
`discogs_album_id`, each with a `_source` field. Spotify matches also set
`spotify_artist_id`, which has no `_source` field.

Track IDs are `spotify_track_id`, `deezer_track_id` and `itunes_track_id`,
each with a `_source` field.

Fetched on albums:

- `style`, `label`, `catalognum` and `barcode`, filled only when empty and
  then copied to the album's tracks. Discogs and Deezer can both supply
  `label` and `barcode`, and whichever runs first fills an empty field.
- `discogs_genre`, `deezer_record_type`, `deezer_explicit`, `itunes_genre`,
  `itunes_copyright` and `itunes_explicit`.
- `lastfm_album_playcount`, `lastfm_album_listeners`, `lastfm_artist_playcount`
  and `lastfm_artist_listeners`, with `lastfm_album_updated` and
  `lastfm_artist_updated`.

Fetched on tracks:

- `deezer_bpm`, `deezer_gain`, `deezer_rank`, `deezer_explicit` and
  `itunes_explicit`.
- `lastfm_playcount`, `lastfm_listeners`, `lastfm_match` and `lastfm_updated`.

crossref never touches the beets `genre` field, which belongs to your genre
plugin. Numeric fields support ranges, for example `beet ls deezer_bpm:120..130`
or `beet ls lastfm_artist_listeners:1000000..`.

## Using the services

crossref calls Spotify, Discogs and Last.fm with your own credentials, and
Deezer, the Apple iTunes Search API and MusicBrainz without any. You are
responsible for following each service's terms of use. The cache holds
answers from these services, so keep it private.

## Development

```bash
uv sync --extra test
uv run pytest -q
uv run ruff check beetsplug tests
```

No test touches the network.

## Feedback

Report bugs and ideas at
https://github.com/blindndangerous/beets-crossref/issues. CHANGELOG.md lists
the changes in each release.

## Licence

MIT
