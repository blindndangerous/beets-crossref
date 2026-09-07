"""Verify and repair existing Spotify IDs on a beets album.

The AlbumRepairer class owns:
    - Verifying that a stored spotify_album_id is still correct
    - Detecting which tracks need re-mapping vs. fresh tagging
    - Falling back to per-track search and computing album consensus
    - Trying related releases (deluxe / remaster / etc.) to fill orphans
"""
import logging

from .helpers import (
    artist_set_score,
    fuzzy_title_score,
    set_artist_id,
)

log = logging.getLogger("beets.spotify_album_match")

VALID_REPAIR_STRATEGIES = {"strict", "related_release", "global_fallback"}


class AlbumRepairer:
    """Verify stored Spotify IDs and repair via related releases or per-track fallback."""

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

        - mismatched: stored ID is on this album but disc/track/ISRC disagree, OR not on this album
        - missing: item has no spotify_track_id stored

        The ISRC comparison only has anything to compare when *spotify_tracks*
        holds full track objects: an album's track list is made of simplified
        objects, which carry no external_ids. Position is the signal that
        actually runs here.
        """
        track_map = {}
        for track in spotify_tracks:
            track_id = track.get('id')
            if not track_id:
                continue
            track_isrc = track.get('external_ids', {}).get('isrc', '') or ''
            track_map[track_id] = (
                track.get('disc_number'),
                track.get('track_number'),
                track_isrc.lower(),
            )

        mismatched_items = []
        missing_items = []
        matched_ids = 0
        total_ids = 0
        for item in album.items():
            existing_id = item.get('spotify_track_id')
            if existing_id:
                total_ids += 1
                if existing_id in track_map:
                    disc_number, track_number, track_isrc = track_map[existing_id]
                    if item.disc and disc_number and item.disc != disc_number:
                        mismatched_items.append(item)
                        continue
                    if item.track and track_number and item.track != track_number:
                        mismatched_items.append(item)
                        continue
                    item_isrc = (item.isrc or '').lower() if hasattr(item, 'isrc') else ''
                    if item_isrc and track_isrc and item_isrc != track_isrc:
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
        strategy = self.get_repair_strategy()
        if strategy == "global_fallback":
            if not self.config['use_track_fallback'].get(bool):
                log.warning(
                    f"{log_prefix}Remaining unmatched track(s): {len(unresolved)}. "
                    "Track fallback is disabled (use_track_fallback=false). Skipping."
                )
                return
            log.info(
                f"{log_prefix}Remaining unmatched track(s): {len(unresolved)}. "
                "Falling back to track-level search."
            )
            fallback_result = self.fallback_track_search(
                album, unresolved, dry_run, overwrite=True,
            )
            current_album_id = fallback_result.get("new_album_id") or album.get('spotify_album_id')
            if current_album_id and fallback_result.get("album_id_changed"):
                log.info(
                    f"{log_prefix}Consensus changed album ID. "
                    "Filling any remaining untagged tracks from new album."
                )
                self.matcher.match_items_to_tracks(
                    list(album.items()),
                    self.client.get_album_tracks(current_album_id) or [],
                    dry_run,
                    overwrite=False,
                )
        elif strategy == "related_release":
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
                "Skipping track fallback in strict repair mode."
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

    def fallback_track_search(self, album, items, dry_run, *, overwrite=False, strict_artist=False):
        """Search Spotify track-by-track and compute album consensus.

        Returns a dict with keys:
            matched: number of tracks that found a Spotify match
            album_id_changed: True if consensus identified a different album than stored
            new_album_id: the album ID after consensus (may equal existing)
            consensus_album_obj: the Spotify album object from the consensus track (or None)
        """
        if not items:
            return {
                "matched": 0,
                "album_id_changed": False,
                "new_album_id": album.get('spotify_album_id'),
                "consensus_album_obj": None,
            }

        log_prefix = "[DRY RUN] " if dry_run else ""
        log.info(f"{log_prefix}Attempting track-level search fallback for '{album.album}'.")

        min_artist_override = None
        if strict_artist:
            min_artist_override = self.config['min_no_album_track_artist_score'].as_number()

        album_hits = {}  # {album_id: {'count': N, 'obj': album_dict}}
        matched = 0
        for item in items:
            existing_id = item.get('spotify_track_id')
            if existing_id and not overwrite:
                continue
            match = self.matcher.search_spotify_track(item, album, min_artist_score=min_artist_override)
            if match:
                matched += 1
                new_id = match['id']
                action = "Updated" if existing_id and existing_id != new_id else "Matched"
                log.info(f"  -> {log_prefix}{action} '{item.title}' -> Spotify ID: {new_id}")
                if not dry_run:
                    item['spotify_track_id'] = new_id
                    set_artist_id(item, match, label=item.title)
                    item.store()
                album_id = match.get('album', {}).get('id')
                if album_id:
                    if album_id not in album_hits:
                        album_hits[album_id] = {'count': 0, 'obj': match.get('album', {})}
                    album_hits[album_id]['count'] += 1
            else:
                log.warning(f"  -> No Spotify match found for track: '{item.title}'")

        existing_album_id = album.get('spotify_album_id')
        album_id_changed = False
        new_album_id = existing_album_id
        consensus_album_obj = None
        consensus_ratio = self.config['fallback_consensus_ratio'].as_number()
        if matched and album_hits:
            best_album_id, best_data = max(album_hits.items(), key=lambda x: x[1]['count'])
            best_count = best_data['count']
            if best_count / matched >= consensus_ratio:
                consensus_album_obj = best_data['obj']
                if existing_album_id != best_album_id:
                    album_id_changed = True
                    new_album_id = best_album_id
                if existing_album_id and existing_album_id != best_album_id:
                    log.info(
                        f"{log_prefix}Updating Spotify album ID based on track consensus: "
                        f"{existing_album_id} -> {best_album_id}"
                    )
                elif not existing_album_id:
                    log.info(
                        f"{log_prefix}Setting Spotify album ID based on track consensus: {best_album_id}"
                    )
                if not dry_run:
                    album['spotify_album_id'] = best_album_id
                    set_artist_id(album, consensus_album_obj, label=album.album)
                    album.store(inherit=False)
                else:
                    new_album_id = best_album_id

        return {
            "matched": matched,
            "album_id_changed": album_id_changed,
            "new_album_id": new_album_id,
            "consensus_album_obj": consensus_album_obj,
        }
