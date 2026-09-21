"""Beets plugin entry point and top-level per-album workflow."""
import logging
import os

from beets.dbcore import types as beets_types
from beets.plugins import BeetsPlugin

from .cli import (
    InteractivePrompter,
    UserAbort,
    build_subcommand,
    default_progress_path,
    extract_spotify_album_id,
    load_progress,
    sanitize_progress_file_path,
    save_progress,
)
from .client import RateLimitAbort, SpotifyClient
from .helpers import (
    clean_spotify_id,
    discard_artist_id,
    dry_run_prefix,
    own_artist_id,
    set_artist_id,
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
    'track_match_threshold': 0.90,
    'min_track_artist_score': 0.90,
    'min_request_interval': 3.0,              # seconds between Spotify API calls
    'max_album_candidates': 3,
    'max_album_popularity_checks': 1,
    'stop_on_rate_limit': True,
    'verify_existing_ids': True,
    'existing_id_mismatch_threshold': 0.3,
    'existing_album_repair_strategy': 'related_release',
    'related_artist_threshold': 0.90,
    'min_preliminary_artist_score': 0.20,
    'clear_unmatched_track_ids': True,
    'clear_on_no_match': True,
    'duration_mismatch_penalty_threshold': 10,    # seconds; diff above this applies a score penalty
    'duration_mismatch_penalty': 0.10,            # penalty subtracted from score on large diff
    'existing_album_validation_threshold': 0.90,  # min score for stored album to pass identity check
}


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
        )

        self._abort_requested = False
        self._prompter = InteractivePrompter()
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

        # beets.ui.decargs has been a no-op since Python 3 and is deprecated in
        # beets 2.4.0, so the query arguments are passed straight through.
        query = args or None
        provided_album_id = self._resolve_provided_album_id(opts)
        if provided_album_id is False:
            return  # invalid --sid value

        albums = list(lib.albums(query))
        if not albums:
            log.info("No albums matched the query.")
            return
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
        self.client.reset_call_count()
        # The returned ID is the one to use from here on: under --dry-run a
        # malformed stored ID is reported but not deleted, and passing it on
        # would make the dry run log a clear-and-re-search the real run does
        # not perform.
        existing_album_id = self._clear_malformed_stored_ids(album, dry_run)
        if provided_album_id:
            self._apply_provided_album_id(album, provided_album_id, dry_run, force)
            return

        verify_existing = self.config['verify_existing_ids'].get(bool)
        if not force and not verify_existing and all(item.get('spotify_track_id') for item in album.items()):
            log.debug(f"Skipping album with all tracks matched: {album.album}")
            return

        log_prefix = dry_run_prefix(dry_run)
        log.info(f"Processing album: {album.albumartist} - {album.album}")
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
            self._handle_no_album_match(album, dry_run, interactive, log_prefix)
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

    def _handle_no_album_match(self, album, dry_run, interactive, log_prefix):
        if interactive:
            log.warning(f"Could not find a good Spotify match for '{album.album}'. Skipping.")
        elif self.config['clear_on_no_match'].get(bool):
            log.warning(
                f"{log_prefix}No Spotify album match found for '{album.album}'. "
                "Clearing all Spotify IDs (clear_on_no_match=true)."
            )
            self.clear_all_ids(album, dry_run)
        else:
            log.warning(
                f"{log_prefix}No Spotify album match found for '{album.album}'. "
                "Skipping (set clear_on_no_match: yes to clear stale IDs)."
            )

    def _apply_provided_album_id(self, album, album_id, dry_run, force):
        log_prefix = dry_run_prefix(dry_run)
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
        log_prefix = dry_run_prefix(dry_run)
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
    # ID clearing (IdClearer interface used by AlbumRepairer)
    # ------------------------------------------------------------------

    def clear_all_ids(self, album, dry_run):
        """Clear spotify_album_id and every spotify_track_id for the album.

        Called when fallback evidence points to the wrong album, or when
        clear_on_no_match is enabled. Unlike clear_track_ids this is NOT
        gated on a config flag.
        """
        log_prefix = dry_run_prefix(dry_run)
        if 'spotify_album_id' in album:
            self._clear_field(
                album, 'spotify_album_id', dry_run, is_album=True,
                message=f"  -> {log_prefix}Clearing Spotify album ID for '{album.album}'",
            )
        for item in album.items():
            if 'spotify_track_id' in item:
                self._clear_field(
                    item, 'spotify_track_id', dry_run,
                    message=f"  -> {log_prefix}Clearing Spotify track ID for: '{item.title}'",
                )

    def clear_track_ids(self, items, dry_run):
        """Clear spotify_track_id for items that could not be matched.

        Only runs when ``clear_unmatched_track_ids`` is True.
        """
        if not self.config['clear_unmatched_track_ids'].get(bool):
            return
        log_prefix = dry_run_prefix(dry_run)
        for item in items:
            if 'spotify_track_id' in item:
                self._clear_field(
                    item, 'spotify_track_id', dry_run,
                    message=(
                        f"  -> {log_prefix}Cleared orphaned Spotify track ID for: '{item.title}'"
                    ),
                )

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
        log.info(
            f"Using Spotify album ID from --sid: {album_id}. "
            "Existing Spotify IDs for this album will be overwritten."
        )
        return album_id

    def _clear_malformed_stored_ids(self, album, dry_run):
        """Remove blank or malformed stored IDs before they reach Spotify APIs.

        Returns the album's usable Spotify album ID, or None when there is
        none. Under --dry-run a malformed ID is left in the library but the
        return value is still None, so the rest of the run behaves as the real
        run would.
        """
        log_prefix = dry_run_prefix(dry_run)
        if 'spotify_album_id' in album:
            album_id = album.get('spotify_album_id')
            if not clean_spotify_id(album_id):
                self._clear_field(
                    album, 'spotify_album_id', dry_run, is_album=True, level=logging.WARNING,
                    message=(
                        f"{log_prefix}Clearing blank/malformed Spotify album ID "
                        f"for '{album.album}': {album_id!r}"
                    ),
                )
        self._clear_malformed_artist_id(album, album.album, dry_run, is_album=True)

        for item in album.items():
            if 'spotify_track_id' in item:
                track_id = item.get('spotify_track_id')
                if not clean_spotify_id(track_id):
                    self._clear_field(
                        item, 'spotify_track_id', dry_run, level=logging.WARNING,
                        message=(
                            f"{log_prefix}Clearing blank/malformed Spotify track ID "
                            f"for '{item.title}': {track_id!r}"
                        ),
                    )
            self._clear_malformed_artist_id(item, item.title, dry_run)

        return clean_spotify_id(album.get('spotify_album_id'))

    @staticmethod
    def _clear_field(obj, field, dry_run, *, message, is_album=False, level=logging.INFO):
        """Log *message*, then delete *field* and the artist ID beside it.

        Under --dry-run the message is still logged but nothing is written.
        `is_album` selects the album store signature, which must not inherit.
        """
        log.log(level, message)
        if dry_run:
            return
        del obj[field]
        discard_artist_id(obj)
        if is_album:
            obj.store(inherit=False)
        else:
            obj.store()

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
            f"{dry_run_prefix(dry_run)}Clearing blank/malformed Spotify artist ID "
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
        if os.path.exists(progress_file):
            os.remove(progress_file)
            log.info("Progress file cleared.")
        else:
            log.info("No progress file to clear.")

