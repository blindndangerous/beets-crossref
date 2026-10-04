# Changelog

## 0.3.0, 2026-10-04

First public release, under the name beets-crossref. It replaces the earlier
beets-spotify-album-match plugin and carries over none of its settings.

- `beet crossref resolve` finds album and track IDs on Spotify, Deezer,
  Apple's iTunes store and Discogs. It uses MusicBrainz links, barcodes and
  ISRCs, plus a fuzzy search on Spotify.
- `beet crossref fetch` fills Discogs style, label and catalogue number,
  Deezer BPM, gain, rank and explicit flags, Apple genre, copyright and
  explicit flags, and Last.fm play counts for tracks, albums and artists.
- `beet crossref` runs both.
- Every ID records how it was found. crossref replaces a stored ID only with
  stronger evidence that lines up at least as many tracks.
- crossref caches answers and keeps to each service's rate limit. When a
  service refuses requests or asks for a long wait, crossref skips it for the
  rest of the run and carries on with the others.
- crossref writes to the beets database only. It writes no tags and moves no
  files.
