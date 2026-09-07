"""Beets plugin entry point and top-level per-album workflow."""
import logging

from beets.plugins import BeetsPlugin
from beets.dbcore import types as beets_types

from .helpers import (
    artist_set_score,
    clean_spotify_id,
    discard_artist_id,
    fuzzy_title_score,
    own_artist_id,
    primary_artist_id,
    set_artist_id,
)
from .client import RateLimitAbort, SpotifyClient
from .cli import (
    InteractivePrompter,
    UserAbort,
    build_subcommand,
    default_progress_path,
    extract_spotify_album_id,
    load_progress,
    parse_args,
    sanitize_progress_file_path,
    save_progress,
)
from .matching import AlbumMatcher
from .repair import AlbumRepairer

log = logging.getLogger("beets.spotify_album_match")


CONFIG_DEFAULTS = {
    'client_id': None,
    'client_secret': None,
    'max_retries': 5,
    'retry_delay': 5,
    'match_threshold': 0.90,
    'certainty_margin': 0.15,
    'duration_tolerance': 3,                  # seconds
    'cache_ttl': 600,                         # cache API results for 10 minutes
    'track_match_threshold': 0.90,
    'min_track_artist_score': 0.90,
    'min_request_interval': 3.0,              # seconds between Spotify API calls
    'max_album_candidates': 3,
    'max_album_popularity_checks': 1,
    'stop_on_rate_limit': True,
    'verify_existing_ids': True,
    'existing_id_mismatch_threshold': 0.3,
    'existing_album_repair_strategy': 'related_release',
    'min_related_release_artist_score': 0.85,
    'related_artist_threshold': 0.90,
    'min_no_album_track_artist_score': 0.75,
    'min_preliminary_artist_score': 0.20,
    'clear_unmatched_track_ids': True,
    'clear_on_no_match': True,
    'fallback_album_validation_threshold': 0.90,  # min title+artist score for consensus album
    'fallback_consensus_ratio': 0.6,              # fraction of matched tracks needed for consensus
    'duration_mismatch_penalty_threshold': 10,    # seconds; diff above this applies a score penalty
    'duration_mismatch_penalty': 0.10,            # penalty subtracted from score on large diff
    'existing_album_validation_threshold': 0.90,  # min score for stored album to pass identity check
    'use_track_fallback': False,                  # enable track-level search when no album match
    'max_track_search_queries': 3,                # max query variants per track (0 = unlimited)
}


def _needs_own_artist_id(obj, id_field):
    """True when obj stores a valid ID in id_field but no artist ID of its own.

    A beets Item with no artist ID reads its album's through ``get``/``in``, so
    the album's value must never count as the item's.
    """
    return bool(clean_spotify_id(obj.get(id_field))) and not own_artist_id(obj)


class SpotifyAlbumMatchPlugin(BeetsPlugin):
    item_types = {
        'spotify_track_id': beets_types.STRING,
        'spotify_artist_id': beets_types.STRING,
    }

    album_types = {
        'spotify_album_id': beets_types.STRING,
        'spotify_artist_id': beets_types.STRING,
    }

    def __init__(self):
        super().__init__('spotify_album_match')
        self.config.add(CONFIG_DEFAULTS)

        self.client = SpotifyClient(
            client_id=self.config['client_id'].get(),
            client_secret=self.config['client_secret'].get(),
            max_retries=self.config['max_retries'].get(int),
            retry_delay=self.config['retry_delay'].get(int),
            stop_on_rate_limit=self.config['stop_on_rate_limit'].get(bool),
            min_request_interval=self.config['min_request_interval'].get(float),
            cache_ttl=self.config['cache_ttl'].get(int),
        )

        self._abort_requested = False
        self._prompter = InteractivePrompter(on_abort=self._handle_user_abort)
        self.matcher = AlbumMatcher(self.client, self.config, prompter=self._prompter)
        self.repairer = AlbumRepairer(self.client, self.config, self.matcher, id_clearer=self)

    # ------------------------------------------------------------------
    # Beets plugin hooks
    # ------------------------------------------------------------------

    def commands(self):
        return [build_subcommand(self._run_spotify_match)]

    # ------------------------------------------------------------------
    # Top-level workflow
    # ------------------------------------------------------------------

    def _run_spotify_match(self, lib, opts, args):
        if not self.client.is_ready:
            return
        self._abort_requested = False
        self.client.reset()
        self._configure_debug_logging(opts)
        if opts.force:
            log.info("Force mode enabled. Existing Spotify IDs will be updated.")

        progress_file = sanitize_progress_file_path(
            getattr(opts, 'progress_file', None) or default_progress_path(),
        )
        if progress_file is None:
            log.error("Invalid --progress-file path. Must end with .json and be a valid file path.")
            return

        if getattr(opts, 'clear_progress', False):
            self._clear_progress_file(progress_file)

        use_resume = getattr(opts, 'resume', False)
        # Always load existing progress so we can append to it even without --resume.
        completed_ids = load_progress(progress_file)
        if use_resume and completed_ids:
            log.info(f"Resuming: {len(completed_ids)} album(s) already completed.")

        query = parse_args(args)
        provided_album_id = self._resolve_provided_album_id(opts)
        if provided_album_id is False:
            return  # invalid --sid value

        albums = list(lib.albums(query))
        if provided_album_id and len(albums) > 1:
            log.error(
                "Spotify album ID was provided, but the query matched "
                f"{len(albums)} albums. Please narrow the query to one album."
            )
            return

        if opts.interactive:
            log.info("Interactive mode enabled. Processing albums sequentially.")
        for album in albums:
            if self._abort_requested:
                log.error("Aborting run.")
                break

            album_key = str(album.id)
            if use_resume and album_key in completed_ids:
                log.debug(f"Skipping already-processed album: {album.albumartist} - {album.album}")
                continue

            try:
                self._process_single_album(
                    album, opts.dry_run, opts.interactive, opts.force, provided_album_id,
                )
                if not opts.dry_run:
                    completed_ids.add(album_key)
                    save_progress(progress_file, completed_ids)
            except RateLimitAbort as exc:
                log.error(str(exc))
                log.info("Progress saved. Re-run with --resume to continue where you left off.")
                break
            except UserAbort as exc:
                self._abort_requested = True
                self.client.abort()
                log.error(str(exc))
                break
            except Exception as exc:
                log.error(f"Error processing album '{album.album}': {exc}")

    def _process_single_album(self, album, dry_run, interactive, force, provided_album_id=None):
        self._clear_malformed_stored_ids(album, dry_run)
        if provided_album_id:
            self._apply_provided_album_id(album, provided_album_id, dry_run, force)
            return

        if self._needs_artist_id_backfill(album):
            self._backfill_artist_ids(album, dry_run)

        verify_existing = self.config['verify_existing_ids'].get(bool)
        if not force and not verify_existing and all(item.get('spotify_track_id') for item in album.items()):
            log.debug(f"Skipping album with all tracks matched: {album.album}")
            return

        log_prefix = self._log_prefix(dry_run)
        log.info(f"Processing album: {album.albumartist} - {album.album}")
        self.client.reset_call_count()

        existing_album_id = album.get('spotify_album_id')
        if not force and verify_existing and existing_album_id:
            if self.repairer.try_verify_existing_album_id(album, existing_album_id, dry_run, log_prefix):
                log.debug(f"API calls for album '{album.album}': {self.client.api_call_count}")
                return
            # Stored album ID was wrong or unverifiable — clear all stale IDs before fresh search.
            # try_verify_existing_album_id clears IDs internally on the title/artist mismatch path,
            # but NOT on the track mismatch ratio path. Always clearing here ensures stale track IDs
            # from the wrong album are gone before the authoritative mapping writes new ones,
            # preventing a loop where unmatched tracks with stale IDs re-trigger 40%+ mismatch
            # on every subsequent run.
            self.clear_all_ids(album, dry_run)

        spotify_album, supplemental_candidates = self.matcher.find_best_album_match(album, interactive)
        if not spotify_album:
            self._handle_no_album_match(album, dry_run, interactive, force, log_prefix)
            return

        log.info(f"{log_prefix}Found best match: '{spotify_album['name']}' ({spotify_album['id']})")

        if not dry_run:
            album['spotify_album_id'] = spotify_album['id']
            set_artist_id(album, spotify_album, label=album.album)
            # inherit=False: a plain album.store() copies the album's flexible
            # fields into every item and cascades deletes, which would replace
            # each track's own spotify_artist_id with the album's.
            album.store(inherit=False)

        unmatched_items = self._apply_authoritative_album_mapping(album, spotify_album['id'], dry_run)
        if unmatched_items:
            log.info(
                f"{log_prefix}Album match left {len(unmatched_items)} unmatched track(s). "
                "Trying related releases."
            )
            unmatched_items = self.repairer.repair_from_related_releases(
                album, unmatched_items, dry_run, related_candidates=supplemental_candidates,
            )
            if unmatched_items:
                log.warning(
                    f"{log_prefix}Remaining unmatched track(s): {len(unmatched_items)}. "
                    "No related-release matches found."
                )
                self.clear_track_ids(unmatched_items, dry_run)

        log.debug(f"API calls for album '{album.album}': {self.client.api_call_count}")

    def _handle_no_album_match(self, album, dry_run, interactive, force, log_prefix):
        if interactive:
            log.warning(f"Could not find a good Spotify match for '{album.album}'. Skipping.")
            return

        if self.config['use_track_fallback'].get(bool):
            log.warning(
                f"Could not find a good Spotify match for '{album.album}'. "
                "Falling back to track-level search."
            )
            fallback_result = self.repairer.fallback_track_search(
                album, list(album.items()), dry_run, overwrite=force, strict_artist=True,
            )
            consensus_album_obj = fallback_result.get("consensus_album_obj")
            if consensus_album_obj:
                title_score = fuzzy_title_score(album.album, consensus_album_obj.get('name', ''))
                artist_score = artist_set_score(
                    album.albumartist,
                    [a.get('name', '') for a in consensus_album_obj.get('artists', [])],
                )
                validation_score = (title_score * 0.6) + (artist_score * 0.4)
                threshold = self.config['fallback_album_validation_threshold'].as_number()
                if validation_score < threshold:
                    log.warning(
                        f"{log_prefix}Fallback tracks converged on unrelated album "
                        f"'{consensus_album_obj.get('name', '')}' "
                        f"(score {validation_score:.2f} < {threshold:.2f}). "
                        "Clearing all Spotify IDs."
                    )
                    self.clear_all_ids(album, dry_run)
                    return
                current_album_id = fallback_result.get("new_album_id")
                if current_album_id:
                    self._apply_authoritative_album_mapping(album, current_album_id, dry_run)
            else:
                if self.config['clear_on_no_match'].get(bool):
                    log.warning(
                        f"{log_prefix}No Spotify match found for '{album.album}'. "
                        "Clearing all Spotify IDs (clear_on_no_match=true)."
                    )
                    self.clear_all_ids(album, dry_run)
                else:
                    log.warning(
                        f"{log_prefix}No Spotify match found for '{album.album}'. "
                        "Existing IDs unchanged (set clear_on_no_match: yes to clear them)."
                    )
        else:
            if self.config['clear_on_no_match'].get(bool):
                log.warning(
                    f"{log_prefix}No Spotify album match found for '{album.album}'. "
                    "Clearing all Spotify IDs (clear_on_no_match=true)."
                )
                self.clear_all_ids(album, dry_run)
            else:
                log.warning(
                    f"{log_prefix}No Spotify album match found for '{album.album}'. "
                    "Skipping (enable use_track_fallback: yes or clear_on_no_match: yes to change behavior)."
                )

    def _apply_provided_album_id(self, album, album_id, dry_run, force):
        log_prefix = self._log_prefix(dry_run)
        local_items = list(album.items())
        candidate = self.matcher.build_candidate_from_album_id(album_id, album, local_items)
        if not candidate:
            log.warning(f"{log_prefix}Could not load Spotify album ID {album_id}. Skipping.")
            return

        spotify_album = candidate["album"]
        log.info(
            f"{log_prefix}Using provided Spotify album ID: "
            f"'{spotify_album.get('name', '')}' ({spotify_album.get('id', album_id)})"
        )

        if not dry_run:
            album['spotify_album_id'] = spotify_album.get('id', album_id)
            set_artist_id(album, spotify_album, label=album.album)
            album.store(inherit=False)

        effective_album_id = spotify_album.get('id', album_id)
        unmatched_items = self._apply_authoritative_album_mapping(album, effective_album_id, dry_run)
        if unmatched_items:
            log.warning(
                f"{log_prefix}Remaining unmatched track(s): {len(unmatched_items)}. "
                "Authoritative album mapping could not match them."
            )
            self.clear_track_ids(unmatched_items, dry_run)

    def _apply_authoritative_album_mapping(self, album, album_id, dry_run):
        log_prefix = self._log_prefix(dry_run)
        spotify_tracks = self.client.get_album_tracks(album_id)
        if not spotify_tracks:
            log.warning(
                f"{log_prefix}Could not load tracks for authoritative album ID {album_id}."
            )
            return list(album.items())
        return self.matcher.match_items_to_tracks(
            list(album.items()), spotify_tracks, dry_run, overwrite=True,
        )

    # ------------------------------------------------------------------
    # Artist ID backfill for albums matched before spotify_artist_id existed
    # ------------------------------------------------------------------

    @staticmethod
    def _needs_artist_id_backfill(album):
        """True when a stored album/track ID has no own spotify_artist_id beside it."""
        if _needs_own_artist_id(album, 'spotify_album_id'):
            return True
        return any(
            _needs_own_artist_id(item, 'spotify_track_id') for item in album.items()
        )

    def _backfill_artist_ids(self, album, dry_run):
        """Fill missing artist IDs from the already-stored album/track IDs.

        Never re-matches: the stored IDs are trusted and only looked up.
        """
        log_prefix = self._log_prefix(dry_run)
        album_id = clean_spotify_id(album.get('spotify_album_id'))

        # Pick the pending items BEFORE the album's artist ID is stored: once it
        # is, every item without its own reads the album's through the fallback
        # and would look already filled.
        pending_items = [
            item for item in album.items()
            if _needs_own_artist_id(item, 'spotify_track_id')
        ]

        album_filled = False
        if _needs_own_artist_id(album, 'spotify_album_id'):
            artist_id = primary_artist_id(self.client.get_album(album_id))
            if artist_id:
                album_filled = True
                if not dry_run:
                    album['spotify_artist_id'] = artist_id
                    album.store(inherit=False)

        tracks_filled = 0
        if pending_items:
            album_track_map = {}
            if album_id:
                for track in self.client.get_album_tracks(album_id) or []:
                    track_id = track.get('id')
                    if track_id:
                        album_track_map[track_id] = track
            # Tracks matched from a related release are not on this album; resolve
            # them in one bulk request rather than one lookup per track.
            missing_ids = [
                track_id for track_id in (
                    clean_spotify_id(item.get('spotify_track_id')) for item in pending_items
                )
                if track_id not in album_track_map
            ]
            if missing_ids:
                album_track_map.update(self.client.get_tracks_bulk(missing_ids))

            for item in pending_items:
                track_id = clean_spotify_id(item.get('spotify_track_id'))
                artist_id = primary_artist_id(album_track_map.get(track_id))
                if not artist_id:
                    log.debug(f"No usable primary Spotify artist ID for '{item.title}'.")
                    continue
                tracks_filled += 1
                if not dry_run:
                    item['spotify_artist_id'] = artist_id
                    item.store()

        summary = (
            f"{log_prefix}Backfilled artist IDs for "
            f"'{album.albumartist} - {album.album}': "
            f"album={'yes' if album_filled else 'no'}, tracks={tracks_filled}"
        )
        if album_filled or tracks_filled:
            log.info(summary)
        else:
            log.debug(summary)

    # ------------------------------------------------------------------
    # ID clearing (IdClearer interface used by AlbumRepairer)
    # ------------------------------------------------------------------

    def clear_all_ids(self, album, dry_run):
        """Clear spotify_album_id and every spotify_track_id for the album.

        Called when fallback evidence points to the wrong album, or when
        clear_on_no_match is enabled. Unlike clear_track_ids this is NOT
        gated on a config flag.
        """
        log_prefix = self._log_prefix(dry_run)
        if 'spotify_album_id' in album:
            log.info(f"  -> {log_prefix}Clearing Spotify album ID for '{album.album}'")
            if not dry_run:
                del album['spotify_album_id']
                discard_artist_id(album)
                album.store(inherit=False)
        for item in album.items():
            if 'spotify_track_id' in item:
                log.info(f"  -> {log_prefix}Clearing Spotify track ID for: '{item.title}'")
                if not dry_run:
                    del item['spotify_track_id']
                    discard_artist_id(item)
                    item.store()

    def clear_track_ids(self, items, dry_run):
        """Clear spotify_track_id for items that could not be matched.

        Only runs when ``clear_unmatched_track_ids`` is True.
        """
        if not self.config['clear_unmatched_track_ids'].get(bool):
            return
        log_prefix = self._log_prefix(dry_run)
        for item in items:
            if 'spotify_track_id' in item:
                log.info(
                    f"  -> {log_prefix}Cleared orphaned Spotify track ID for: '{item.title}'"
                )
                if not dry_run:
                    del item['spotify_track_id']
                    discard_artist_id(item)
                    item.store()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_provided_album_id(self, opts):
        """Return the validated album ID, None if not provided, or False on error."""
        raw = getattr(opts, 'spotify_album_id', None)
        if not raw:
            return None
        album_id = extract_spotify_album_id(raw)
        if not album_id:
            log.error("Could not parse Spotify album ID from --sid.")
            return False
        log.info(f"Using Spotify album ID from --sid: {album_id}")
        return album_id

    def _clear_malformed_stored_ids(self, album, dry_run):
        """Remove blank or malformed stored IDs before they reach Spotify APIs."""
        log_prefix = self._log_prefix(dry_run)
        if 'spotify_album_id' in album:
            album_id = album.get('spotify_album_id')
            if not clean_spotify_id(album_id):
                log.warning(
                    f"{log_prefix}Clearing blank/malformed Spotify album ID "
                    f"for '{album.album}': {album_id!r}"
                )
                if not dry_run:
                    del album['spotify_album_id']
                    discard_artist_id(album)
                    album.store(inherit=False)
        self._clear_malformed_artist_id(album, album.album, dry_run, is_album=True)

        for item in album.items():
            if 'spotify_track_id' in item:
                track_id = item.get('spotify_track_id')
                if not clean_spotify_id(track_id):
                    log.warning(
                        f"{log_prefix}Clearing blank/malformed Spotify track ID "
                        f"for '{item.title}': {track_id!r}"
                    )
                    if not dry_run:
                        del item['spotify_track_id']
                        discard_artist_id(item)
                        item.store()
            self._clear_malformed_artist_id(item, item.title, dry_run)

    def _clear_malformed_artist_id(self, obj, label, dry_run, *, is_album=False):
        """Remove a blank or malformed stored artist ID left on its own.

        Reads the object's OWN value: an item that only sees its album's artist
        ID through the fallback has nothing of its own to clear. `is_album`
        selects the album store signature, which must not inherit.
        """
        artist_id = own_artist_id(obj)
        if artist_id is None or clean_spotify_id(artist_id):
            return
        log.warning(
            f"{self._log_prefix(dry_run)}Clearing blank/malformed Spotify artist ID "
            f"for '{label}': {artist_id!r}"
        )
        if not dry_run:
            del obj['spotify_artist_id']
            if is_album:
                obj.store(inherit=False)
            else:
                obj.store()

    @staticmethod
    def _clear_progress_file(progress_file):
        import os
        if os.path.exists(progress_file):
            os.remove(progress_file)
            log.info("Progress file cleared.")
        else:
            log.info("No progress file to clear.")

    @staticmethod
    def _configure_debug_logging(opts):
        if not getattr(opts, 'debug', False):
            return
        log.setLevel(logging.DEBUG)
        if not log.handlers:
            handler = logging.StreamHandler()
            root_logger = logging.getLogger()
            if root_logger.handlers:
                handler.setFormatter(root_logger.handlers[0].formatter)
            handler.setLevel(logging.DEBUG)
            log.addHandler(handler)
        else:
            for handler in log.handlers:
                handler.setLevel(logging.DEBUG)
        log.propagate = False
        log.info("Debug logging enabled for spotify-album-match.")

    def _handle_user_abort(self):
        self._abort_requested = True
        self.client.abort()

    @staticmethod
    def _log_prefix(dry_run):
        return "[DRY RUN] " if dry_run else ""
