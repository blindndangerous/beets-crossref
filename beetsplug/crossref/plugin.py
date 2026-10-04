"""beet crossref: find each album on other services, then fetch what they know.

    beet crossref resolve [-s SOURCES] [-p] [QUERY]   fill missing album/track IDs
    beet crossref fetch   [-s SOURCES] [-p] [QUERY]   fill fields from stored IDs

Writes to the beets database only, never to files.  Run spotifysync, mbsync
and friends afterwards for what they own.
"""

from __future__ import annotations

import logging
from collections import Counter

from beets import ui
from beets.dbcore import types
from beets.plugins import BeetsPlugin

from . import evidence
from .cache import Cache
from .http import SourceUnavailable
from .musicbrainz import MusicBrainz
from .sources import REGISTRY, lastfm, load
from .sources.base import Source, Updates
from .tracks import item_isrcs, match_tracks

log = logging.getLogger("beets.crossref")

# Fixed beets fields crossref may fill, only while they are empty.  Album
# values are mirrored onto the items so `beet write` sees them.
FILL_IF_EMPTY = ("style", "label", "catalognum", "barcode")

# An ISRC or fuzzy match must line up at least this share of the album's
# items before its album ID is trusted.  MusicBrainz and barcode evidence
# identify the release itself and are accepted on their own.
MIN_TRACK_SHARE = 0.5
ISRC_PROBES = 3  # ISRCs looked up per album when searching by ISRC


_DEEZER_TYPES = {
    "deezer_bpm": types.FLOAT,
    "deezer_gain": types.FLOAT,
    "deezer_rank": types.INTEGER,
    "deezer_explicit": types.INTEGER,
}


class CrossrefPlugin(BeetsPlugin):
    # Numeric types make range queries such as `deezer_bpm:120..130` work.
    # Album fields are registered on items too: an item query sees its
    # album's value, and needs the type to compare it as a number.
    album_types = {**lastfm.ALBUM_TYPES, "deezer_explicit": types.INTEGER}
    item_types = {**lastfm.ITEM_TYPES, **lastfm.ALBUM_TYPES, **_DEEZER_TYPES}

    def __init__(self):
        super().__init__("crossref")
        self.config.add({
            "sources": ["spotify", "deezer", "itunes", "discogs", "lastfm"],
            "cache": "",  # default: crossref_cache.db in the beets config dir
            "cache_days": 30,
        })

    def commands(self):
        cmd = ui.Subcommand("crossref", help="find albums on other services and fetch what they know")
        cmd.parser.usage = "%prog resolve|fetch [options] [QUERY]"
        cmd.parser.add_option("-s", "--source", dest="sources", default="",
                              help="comma-separated sources (default: all configured)")
        cmd.parser.add_option("-p", "--pretend", action="store_true", help="show changes, write nothing")
        cmd.func = self._command
        return [cmd]

    # --- setup ----------------------------------------------------------------

    def _cache(self) -> Cache:
        from beets import config

        path = self.config["cache"].as_filename() if self.config["cache"].get() else None
        return Cache(path or f"{config.config_dir()}/crossref_cache.db", self.config["cache_days"].get(float))

    def _sources(self, names: str, cache: Cache) -> list[Source]:
        wanted = [n.strip() for n in names.split(",") if n.strip()] or self.config["sources"].as_str_seq()
        unknown = [n for n in wanted if n not in REGISTRY]
        if unknown:
            raise ui.UserError(f"unknown source(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}")
        sources = []
        for name in wanted:
            source = load(name)(self.config[name], cache)
            if source.ready:
                sources.append(source)
            else:
                log.warning("crossref: skipping %s, not configured", name)
        return sources

    def _command(self, lib, opts, args):
        if not args or args[0] not in ("resolve", "fetch"):
            raise ui.UserError("usage: beet crossref resolve|fetch [-s SOURCES] [-p] [QUERY]")
        action, query = args[0], args[1:] or None
        cache = self._cache()
        try:
            sources = self._sources(opts.sources, cache)
            albums = list(lib.albums(query))
            log.info("crossref %s: %d albums, sources %s", action, len(albums),
                     ", ".join(s.name for s in sources))
            if action == "resolve":
                self._resolve(lib, albums, [s for s in sources if s.resolves], cache, opts.pretend)
            else:
                self._fetch(lib, albums, [s for s in sources if s.fetches], opts.pretend)
        finally:
            cache.close()

    # --- resolve --------------------------------------------------------------

    def _resolve(self, lib, albums, sources, cache, pretend):
        mb = MusicBrainz(cache)
        stats = Counter()
        live = list(sources)
        for index, album in enumerate(albums, 1):
            if index % 50 == 0:
                log.info("crossref resolve: %d/%d albums", index, len(albums))
            items = list(album.items())
            for source in list(live):
                try:
                    method = self._resolve_one(lib, mb, source, album, items, pretend)
                except SourceUnavailable as exc:
                    log.warning("crossref: %s unavailable for the rest of this run: %s", source.name, exc)
                    live.remove(source)
                    continue
                stats[f"{source.name}: {method or 'unchanged'}"] += 1
        for key, count in sorted(stats.items()):
            ui.print_(f"{key}: {count}")

    def _resolve_one(self, lib, mb, source: Source, album, items, pretend) -> str | None:
        """Resolve one album on one source; returns the method used, if any."""
        current = evidence.stored_method(album, source.album_field)
        for method in evidence.ORDER:
            if not evidence.can_improve(current, method):
                return None
            # Every candidate is scored: the same recordings sit on the
            # standard and the deluxe edition, and the one whose tracklist
            # covers the most of this album's tracks is the right one.
            best = None
            for album_id in dict.fromkeys(self._candidates(mb, source, album, items, method)):
                tracks = source.album_tracks(album_id)
                if tracks is None:
                    continue
                matched = match_tracks(items, tracks)
                if best is None or len(matched) > len(best[1]):
                    best = (album_id, matched)
            if best is None:
                continue
            trusted = method in ("musicbrainz", "barcode")
            if not trusted and len(best[1]) < MIN_TRACK_SHARE * max(1, len(items)):
                continue
            if current and not self._covers_stored(source, album, items, best):
                continue
            self._apply_ids(source, album, items, best[0], best[1], method, pretend)
            return method
        return None

    @staticmethod
    def _covers_stored(source: Source, album, items, best) -> bool:
        """A replacement must line up at least as many tracks as the stored ID.

        Stronger evidence can still name a smaller edition: an ISRC shared by
        the standard and deluxe releases must not swap a correct deluxe ID
        for the standard one.
        """
        stored_id = album._values_flex.get(source.album_field)
        if stored_id == best[0]:
            return True
        stored_tracks = source.album_tracks(stored_id)
        return stored_tracks is None or len(best[1]) >= len(match_tracks(items, stored_tracks))

    def _candidates(self, mb, source: Source, album, items, method):
        if method == "musicbrainz":
            for url in mb.release_urls(album.mb_albumid):
                album_id = source.album_id_from_url(url)
                if album_id:
                    yield album_id
        elif method == "barcode":
            if album.barcode:
                album_id = source.album_by_barcode(album.barcode)
                if album_id:
                    yield album_id
        elif method == "isrc":
            # Probe the last tracks first: bonus tracks sit at the end and
            # exist only on the expanded edition, while the opening tracks
            # also lead to the standard one.
            ordered = sorted(items, key=lambda i: (i.disc or 1, i.track or 0), reverse=True)
            probes = ordered[:ISRC_PROBES - 1] + ordered[-1:]
            votes = Counter()
            codes = [min(item_isrcs(item)) for item in dict.fromkeys(probes) if item_isrcs(item)]
            for code in codes:
                votes.update(set(source.album_ids_by_isrc(code)))
            for album_id, _ in votes.most_common(ISRC_PROBES):
                yield album_id
        else:
            album_id = source.album_fuzzy(album, items)
            if album_id:
                yield album_id

    def _apply_ids(self, source, album, items, album_id, matched, method, pretend):
        field = source.album_field
        changes = {field: album_id, evidence.source_field(field): method, **source.album_extras(album_id)}
        ui.print_(f"{album.albumartist} - {album.album}: {source.name} {album_id} ({method}, "
                  f"{len(matched)}/{len(items)} tracks)")
        if pretend:
            return
        for key, value in changes.items():
            album[key] = value
        album.store(inherit=False)  # inherit=True would copy album flex fields onto items
        if not source.track_field:
            return
        for item in items:
            hit = matched.get(item.id)
            if hit is None:
                continue
            if not evidence.can_improve(evidence.stored_method(item, source.track_field), method) \
                    and item._values_flex.get(source.track_field) != hit.id:
                continue
            item[source.track_field] = hit.id
            item[evidence.source_field(source.track_field)] = method
            item.store()

    # --- fetch ----------------------------------------------------------------

    def _fetch(self, lib, albums, sources, pretend):
        live = list(sources)
        for index, album in enumerate(albums, 1):
            if index % 50 == 0:
                log.info("crossref fetch: %d/%d albums", index, len(albums))
            items = list(album.items())
            for source in list(live):
                try:
                    updates = source.fetch(album, items)
                except SourceUnavailable as exc:
                    log.warning("crossref: %s unavailable for the rest of this run: %s", source.name, exc)
                    live.remove(source)
                    continue
                self._apply_updates(source, album, items, updates, pretend)

    def _apply_updates(self, source, album, items, updates: Updates, pretend):
        album_changes = {
            key: value for key, value in updates.album.items()
            if value not in (None, "") and not (key in FILL_IF_EMPTY and album.get(key))
            and album.get(key) != value
        }
        by_id = {item.id: item for item in items}
        item_changes: dict[int, dict] = {}
        for item_id, fields in updates.items.items():
            item = by_id.get(item_id)
            if item is None:
                continue
            changed = {
                key: value for key, value in fields.items()
                if value not in (None, "") and not (key in FILL_IF_EMPTY and item.get(key))
                and item._values_flex.get(key, item.get(key)) != value
            }
            if changed:
                item_changes[item_id] = changed
        # Fixed album fields belong on every item as well.
        for key in FILL_IF_EMPTY:
            if key in album_changes:
                for item in items:
                    if not item.get(key):
                        item_changes.setdefault(item.id, {})[key] = album_changes[key]
        if not album_changes and not item_changes:
            return
        ui.print_(f"{album.albumartist} - {album.album}: {source.name} sets "
                  f"{', '.join(sorted(album_changes)) or 'no album fields'}; "
                  f"{len(item_changes)} tracks updated")
        if pretend:
            return
        if album_changes:
            album.update(album_changes)
            album.store(inherit=False)
        for item_id, fields in item_changes.items():
            item = by_id[item_id]
            item.update(fields)
            item.store()
