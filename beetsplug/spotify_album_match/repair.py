"""Verify and repair existing Spotify IDs on a beets album.

The AlbumRepairer class owns:
    - Verifying that a stored spotify_album_id is still correct
    - Detecting which tracks need re-mapping vs. fresh tagging
    - Trying related releases (deluxe / remaster / etc.) to fill orphans
"""
import logging

from .helpers import (
    artist_set_score,
    fuzzy_title_score,
    set_artist_id,
)

log = logging.getLogger("beets.spotify_album_match")

VALID_REPAIR_STRATEGIES = {"strict", "related_release"}


class AlbumRepairer:
    """Verify stored Spotify IDs and repair them from related releases."""

    def __init__(self, client, config, matcher, id_clearer):
        """
        :param id_clearer: object with `clear_all_ids(album, dry_run)` and
            `clear_track_ids(items, dry_run)` methods. Avoids a circular dep on the plugin.
        """
        self.client = client
        self.config = config
        self.matcher = matcher
        self.id_clearer = id_clearer

    def get_repair_strategy(self):
        strategy = self.config['existing_album_repair_strategy'].get()
        strategy = (str(strategy).strip().lower() if strategy is not None else "")
        if strategy in VALID_REPAIR_STRATEGIES:
            return strategy
        log.warning(
            f"Unknown existing_album_repair_strategy='{strategy}'. "
            "Falling back to 'related_release'."
        )
        return "related_release"

    def try_verify_existing_album_id(self, album, existing_album_id, dry_run, log_prefix):
        """Verify the stored Spotify album ID, repairing track inconsistencies if valid.

        Returns True when processing for this album is complete (verified or repaired).
        Returns False when the album ID was wrong or unconfirmable — caller should re-search.
        """
        # Step 1: Validate that the stored Spotify album matches by title/artist.
        # If album details are unavailable, skip this and proceed to track-level sync.
        spotify_album_details = self.client.get_album(existing_album_id)
        if spotify_album_details and isinstance(spotify_album_details, dict):
            album_validation_threshold = self.config['existing_album_validation_threshold'].as_number()
            title_score = fuzzy_title_score(album.album, spotify_album_details.get('name', ''))
            artist_score = artist_set_score(
                album.albumartist,
                [artist.get('name', '') for artist in spotify_album_details.get('artists', [])],
            )
            album_validation_score = (title_score * 0.6) + (artist_score * 0.4)
            if album_validation_score < album_validation_threshold:
                log.warning(
                    f"{log_prefix}Stored Spotify album '{spotify_album_details.get('name', '')}' "
                    f"does not match local album '{album.album}' "
                    f"(score {album_validation_score:.2f} < {album_validation_threshold:.2f}). "
                    "Clearing Spotify IDs and re-searching."
                )
                self.id_clearer.clear_all_ids(album, dry_run)
                return False

        # Step 2: Album identity confirmed (or unverifiable). Check track-level consistency.
        spotify_tracks = self.client.get_album_tracks(existing_album_id)
        if not spotify_tracks:
            return False

        mismatched_items, missing_items, matched_ids, total_ids = self.evaluate_existing_track_ids(
            album, spotify_tracks,
        )
        mismatch_ratio = 0.0
        if total_ids > 0:
            mismatch_ratio = 1.0 - (matched_ids / total_ids)

        mismatch_threshold = self.config['existing_id_mismatch_threshold'].get(float)

        # All existing IDs verified; fill any items that were never tagged.
        if mismatch_ratio <= mismatch_threshold and not mismatched_items:
            if missing_items:
                log.info(
                    f"{log_prefix}Existing Spotify IDs verified. "
                    f"Completing {len(missing_items)} untagged track(s)."
                )
                still_missing = self.matcher.match_items_to_tracks(
                    missing_items, spotify_tracks, dry_run,
                )
                if still_missing:
                    log.info(
                        f"{log_prefix}{len(still_missing)} track(s) not found on stored album. "
                        "Trying repair strategy."
                    )
                    self.apply_repair_strategy(album, still_missing, existing_album_id, dry_run)
            else:
                log.info(f"{log_prefix}Existing Spotify IDs verified. Skipping album.")
            return True

        # Existing IDs are mostly good but some items need re-mapping.
        if mismatch_ratio <= mismatch_threshold:
            log.info(f"{log_prefix}Existing Spotify IDs out of sync. Repairing using stored album ID.")
            items_to_repair = mismatched_items + missing_items
            unresolved = self.matcher.match_items_to_tracks(
                items_to_repair, spotify_tracks, dry_run, overwrite=True,
            )
            if unresolved:
                self.apply_repair_strategy(album, unresolved, existing_album_id, dry_run)
            return True

        log.warning(
            f"{log_prefix}Existing album ID looks mismatched (ratio {mismatch_ratio:.0%}). "
            "Searching for a better match."
        )
        return False

    @staticmethod
    def evaluate_existing_track_ids(album, spotify_tracks):
        """Classify items into (mismatched, missing, matched_count, total_count).

        - mismatched: stored ID is on this album but disc/track disagree, OR not on this album
        - missing: item has no spotify_track_id stored

        Position is the only signal available here: an album's track list comes
        back as simplified objects, which carry no external_ids at all.
        """
        track_map = {
            track['id']: (track.get('disc_number'), track.get('track_number'))
            for track in spotify_tracks if track.get('id')
        }

        mismatched_items = []
        missing_items = []
        matched_ids = 0
        total_ids = 0
        for item in album.items():
            existing_id = item.get('spotify_track_id')
            if existing_id:
                total_ids += 1
                if existing_id in track_map:
                    disc_number, track_number = track_map[existing_id]
                    if item.disc and disc_number and item.disc != disc_number:
                        mismatched_items.append(item)
                        continue
                    if item.track and track_number and item.track != track_number:
                        mismatched_items.append(item)
                        continue
                    matched_ids += 1
                else:
                    mismatched_items.append(item)
            else:
                missing_items.append(item)

        return mismatched_items, missing_items, matched_ids, total_ids

    def apply_repair_strategy(self, album, unresolved, existing_album_id, dry_run):
        """Apply the configured repair strategy to items not matched by the stored album."""
        log_prefix = "[DRY RUN] " if dry_run else ""
        if self.get_repair_strategy() == "related_release":
            log.info(
                f"{log_prefix}Remaining unmatched track(s): {len(unresolved)}. "
                "Trying related releases by albumartist/title."
            )
            unresolved = self.repair_from_related_releases(album, unresolved, dry_run)
            if unresolved:
                log.warning(
                    f"{log_prefix}Remaining unmatched track(s): {len(unresolved)}. "
                    "No related-release matches found."
                )
                self.id_clearer.clear_track_ids(unresolved, dry_run)
        else:
            log.warning(
                f"{log_prefix}Remaining unmatched track(s): {len(unresolved)}. "
                "Skipping related releases in strict repair mode."
            )
            self.id_clearer.clear_track_ids(unresolved, dry_run)

    def repair_from_related_releases(self, album, items, dry_run, *, related_candidates=None):
        existing_album_id = album.get('spotify_album_id')
        if related_candidates is None:
            related_candidates = self.matcher.get_related_release_candidates(
                album, exclude_album_id=existing_album_id,
            )
        else:
            related_candidates = [
                c for c in related_candidates
                if c.get("album", {}).get("id") != existing_album_id
            ]
        if not related_candidates:
            return items

        remaining = list(items)
        log_prefix = "[DRY RUN] " if dry_run else ""
        for candidate in related_candidates:
            if not remaining:
                break
            release = candidate["album"]
            release_id = release.get("id")
            if not release_id:
                continue
            tracks = candidate.get("tracks") or self.client.get_album_tracks(release_id)
            if not tracks:
                continue
            log.info(
                f"{log_prefix}Checking related release: '{release.get('name', '')}' ({release_id})"
            )
            context = f"(release: {release.get('name', '')})"
            remaining = self.matcher.match_items_to_tracks(
                remaining, tracks, dry_run, context=context, overwrite=True,
            )
            if not remaining:
                if release_id != existing_album_id:
                    log.info(
                        f"{log_prefix}Promoting related release as authoritative album: "
                        f"{existing_album_id} -> {release_id}"
                    )
                    if not dry_run:
                        album['spotify_album_id'] = release_id
                        set_artist_id(album, release, label=album.album)
                        # inherit=False: the items were matched (and given their
                        # own artist IDs) just above; an inheriting store would
                        # overwrite every one of them with this release's artist.
                        album.store(inherit=False)
                # Already matched in match_items_to_tracks above; do NOT re-run authoritative
                # mapping over the whole album — that would overwrite already-correct IDs
                # from the primary release.
                return []

        return remaining
