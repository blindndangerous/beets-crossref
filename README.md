# beets-crossref

A beets plugin that finds each album in your library on other services, then
fetches what those services know about it.

- `beet crossref resolve` fills in album and track IDs for Spotify, Deezer,
  Apple (iTunes) and Discogs.
- `beet crossref fetch` uses those IDs to fill in fields: Discogs style, label
  and catalogue number, Deezer BPM, gain and rank, Apple genre and explicit or
  clean flags, and Last.fm popularity for tracks, albums and artists.

It writes to the beets database only, never to your audio files. Run
`beet write` if you want the standard fields in your tags.

Afterwards, beets' own sync commands fill in the rest from the new IDs:
`beet spotifysync` (popularity and audio features), `beet mbsync`, and so on.

## Install

```bash
uv pip install -e .
```

Run that inside the environment beets uses. Or copy the `beetsplug/crossref/` folder into your beets plugin path (for
example `~/.config/beets/beetsplug/crossref/`) and set `pluginpath`.

Add `crossref` to `plugins` in your beets config.

## Configure

```yaml
crossref:
    sources: [spotify, deezer, itunes, discogs, lastfm]
    cache_days: 30          # how long answers are reused; 0 keeps them forever
    cache: ""               # default: crossref_cache.db in the beets config folder
    spotify:
        client_id: YOUR_ID
        client_secret: YOUR_SECRET
    discogs:
        token: YOUR_PERSONAL_ACCESS_TOKEN
    itunes:
        country: US
    lastfm:
        apikey: YOUR_KEY    # or set LASTFM_API_KEY
        min_interval: 0.5
        match_ratio: 0.85
        prefer_mbid: yes
        max_age_days: 30    # skip tracks, albums and artists fetched more recently
```

Deezer and Apple need no credentials. A source without its credentials is
skipped with a warning.

- Spotify: create an app at https://developer.spotify.com/dashboard. crossref
  only searches and reads albums with it. Audio features come from beets'
  `spotifysync`, which uses beets' own built-in keys, so leave beets' `spotify`
  section without a client ID.
- Discogs: generate a personal access token under Settings, Developers on
  discogs.com.
- Last.fm: register a key at https://www.last.fm/api/account/create.

## Use

```bash
beet crossref resolve                     # every album, every source
beet crossref resolve -s deezer,itunes    # chosen sources only
beet crossref resolve -p artist:Muse      # pretend: show what would change
beet crossref fetch
beet crossref fetch -s lastfm added:2026-10-01..
beet spotifysync
```

Both commands take a normal beets album query. Answers are cached, so running
again costs almost no requests. Stop with Ctrl+C at any time; every album is
saved as soon as it is done.

## How IDs are found

For each album and source, crossref tries these in order and stops at the
first that works:

1. MusicBrainz: the release's links to the same album on that service.
2. Barcode: a lookup of the album's UPC or EAN.
3. ISRC: tracks with the album's recording codes (Spotify and Deezer).
4. Fuzzy search by artist and title (Spotify only).

ISRC and fuzzy matches are kept only when at least half the album's tracks
line up by disc, position and length. Track IDs are matched by ISRC, checked
against disc and position or length (within 3 seconds), and otherwise by disc,
position and length.

Each ID records how it was found in a sibling field, for example
`deezer_album_id_source: barcode`. A stored ID is replaced only by stronger
evidence, in the order above. IDs stored by anything else count as fuzzy.

Known limit: one barcode can match several albums, such as an explicit and a
clean edition. crossref takes the first. A MusicBrainz link avoids this.

## Fields

Album IDs: `spotify_album_id`, `deezer_album_id`, `itunes_album_id`,
`discogs_album_id`, plus `spotify_artist_id`, each with a `_source` field.

Track IDs: `spotify_track_id`, `deezer_track_id`, `itunes_track_id`, each with
a `_source` field.

Fetched, on albums:

- `style`, `label`, `catalognum`, `barcode`: filled only when empty, and copied
  to the album's tracks.
- `discogs_genre`, `deezer_record_type`, `deezer_explicit`, `itunes_genre`,
  `itunes_copyright`, `itunes_explicit`.
- `lastfm_album_playcount`, `lastfm_album_listeners`, `lastfm_artist_playcount`,
  `lastfm_artist_listeners`, with `lastfm_album_updated` and
  `lastfm_artist_updated`.

Fetched, on tracks:

- `deezer_bpm`, `deezer_gain`, `deezer_rank`, `deezer_explicit`,
  `itunes_explicit`.
- `lastfm_playcount`, `lastfm_listeners`, `lastfm_match`, `lastfm_updated`.

The beets `genre` field is never touched; it belongs to your genre plugin.
Numeric fields support ranges, for example `beet ls deezer_bpm:120..130` or
`beet ls lastfm_artist_listeners:1000000..`.

## Licence

MIT
