"""Album and track matching against Spotify.

The AlbumMatcher class owns:
    - Album candidate search (by UPC, then text queries)
    - Candidate scoring and selection (with optional interactive disambiguation)
    - Mapping the local items of an album onto that album's Spotify tracks
"""
import logging

from spotipy.exceptions import SpotifyException

from .helpers import (
    album_barcode,
    artist_set_score,
    build_album_search_queries,
    calculate_match_score,
    find_matching_spotify_track,
    fuzzy_title_score,
    is_variant_title,
    set_artist_id,
)

log = logging.getLogger("beets.spotify_album_match")

SEARCH_LIMIT = 3
VARIANT_TITLE_THRESHOLD = 0.6


class AlbumMatcher:
    """Find Spotify album candidates and match local items to Spotify tracks.

    `prompter`, when provided, is a callable invoked for interactive disambiguation.
    Signature: prompter(candidates, local_album, local_items, build_candidate_fn) -> candidate or None.
    """

    def __init__(self, client, config, prompter=None):
        self.client = client
        self.config = config
        self.prompter = prompter

    # ------------------------------------------------------------------
    # Album search and selection
    # ------------------------------------------------------------------

    def find_best_album_match(self, local_album, interactive):
        """Return (selected_album_dict_or_None, supplemental_candidates).

        Tries the barcode when `use_upc_lookup` is on, then text-search
        candidates. Selection considers title/artist scores, popularity, and
        variant-vs-standard preference.
        """
        if self.config['use_upc_lookup'].get(bool):
            barcode = album_barcode(local_album)
            if barcode:
                upc_match = self._search_by_upc(local_album, barcode)
                if upc_match:
                    return upc_match, []

        candidates_by_id = self._search_album_candidates(local_album)
        if not candidates_by_id:
            return None, []

        local_items = list(local_album.items())
        preliminary = self._build_preliminary_candidates(local_album, candidates_by_id)
        candidates = self._build_detailed_candidates(local_album, local_items, preliminary)
        if not candidates:
            return None, []

        return self._select_album_candidate(local_album, local_items, candidates, interactive)

    def _search_by_upc(self, local_album, barcode):
        """Return the Spotify album for a barcode, or None.

        Experimental, off by default (`use_upc_lookup`). The hit is checked
        against the local title and artist before it is trusted: a barcode
        search returns exactly one release and a mistagged barcode would
        otherwise be accepted with no evidence at all. That check is not
        airtight: `fuzzy_title_score` uses partial_ratio, so a superset title
        such as "Greatest Hits" scores 1.0 against "Hits", and an album with an
        empty albumartist scores 0 on artist and can never clear the gate.
        """
        query = f'upc:{barcode}'
        log.info(f"  -> Searching Spotify by UPC: {query}")
        try:
            results = self.client.search(q=query, type='album', limit=1)
        except SpotifyException:
            log.warning("  -> Searching by UPC failed. Falling back to search.")
            return None

        items = results.get("albums", {}).get("items", []) if isinstance(results, dict) else []
        sp_album = items[0] if items else None
        if not sp_album or not isinstance(sp_album, dict):
            return None

        title_score = fuzzy_title_score(local_album.album, sp_album.get('name', ''))
        artist_score = artist_set_score(
            local_album.albumartist,
            [artist.get('name', '') for artist in sp_album.get('artists', [])],
        )
        validation_score = (title_score * 0.6) + (artist_score * 0.4)
        threshold = self.config['existing_album_validation_threshold'].as_number()
        if validation_score < threshold:
            log.warning(
                f"  -> UPC hit '{sp_album.get('name', '')}' does not match "
                f"'{local_album.album}' (score {validation_score:.2f} < "
                f"{threshold:.2f}). Falling back to search."
            )
            return None

        log.info(f"  -> Found direct match via UPC for '{local_album.album}'")
        return sp_album

    def build_candidate_from_album_id(self, album_id, local_album, local_items):
        """Build a candidate dict from a known Spotify album ID (for --sid / interactive)."""
        details = self.client.get_album(album_id)
        if not details or not isinstance(details, dict):
            return None
        tracks = self.client.get_album_tracks(album_id) or []

        score = calculate_match_score(local_album, local_items, tracks, details)
        track_count = len(tracks)
        base_title_score = fuzzy_title_score(local_album.album, details.get('name', ''))
        artist_score = artist_set_score(
            local_album.albumartist,
            [artist.get('name', '') for artist in details.get('artists', [])],
        )
        is_variant = is_variant_title(details.get('name', '')) and base_title_score >= VARIANT_TITLE_THRESHOLD
        popularity = details.get('popularity') or 0

        return {
            "score": score,
            "album": details,
            "track_count": track_count,
            "is_variant": is_variant,
            "popularity": popularity,
            "base_title_score": base_title_score,
            "artist_score": artist_score,
            "tracks": tracks,
        }

    def get_related_release_candidates(self, local_album, exclude_album_id=None):
        """Return related-release candidates, sorted by popularity then score."""
        candidates_by_id = self._search_album_candidates(local_album)
        if not candidates_by_id:
            return []

        local_items = list(local_album.items())
        preliminary = self._build_preliminary_candidates(local_album, candidates_by_id)
        candidates = self._build_detailed_candidates(local_album, local_items, preliminary)
        if not candidates:
            return []

        min_artist_score = self.config['min_related_release_artist_score'].as_number()
        related = []
        for candidate in candidates:
            album_id = candidate["album"].get("id")
            if exclude_album_id and album_id == exclude_album_id:
                continue
            if candidate["artist_score"] < min_artist_score:
                continue
            if candidate["base_title_score"] < VARIANT_TITLE_THRESHOLD:
                continue
            related.append(candidate)

        related.sort(key=lambda c: ((c["popularity"] or 0), c["score"]), reverse=True)
        return related

    def _search_album_candidates(self, local_album):
        candidates_by_id = {}
        queries = build_album_search_queries(local_album.album, local_album.albumartist)
        if queries:
            log.info(f"  -> Trying {len(queries)} album search variant(s).")
        else:
            log.warning("  -> No album search variants available.")

        for query in queries:
            log.debug(f"  -> Searching Spotify with query: {query}")
            try:
                results = self.client.search(q=query, type="album", limit=SEARCH_LIMIT)
            except SpotifyException:
                log.warning(f"Spotify search failed for query: {query}")
                continue

            if not isinstance(results, dict):
                continue
            for sp_album in results.get("albums", {}).get("items", []):
                if not sp_album or not isinstance(sp_album, dict) or not sp_album.get('id'):
                    continue
                candidates_by_id[sp_album['id']] = sp_album

        return candidates_by_id

    def _build_preliminary_candidates(self, local_album, candidates_by_id):
        preliminary = []
        min_preliminary_artist_score = self.config['min_preliminary_artist_score'].as_number()
        for sp_album in candidates_by_id.values():
            base_title_score = fuzzy_title_score(local_album.album, sp_album.get('name', ''))
            artist_score = artist_set_score(
                local_album.albumartist,
                [artist.get('name', '') for artist in sp_album.get('artists', [])],
            )
            if artist_score < min_preliminary_artist_score:
                log.debug(
                    f"  -> Skipping candidate '{sp_album.get('name', '')}' "
                    f"(artist_score {artist_score:.2f} < {min_preliminary_artist_score:.2f})"
                )
                continue
            quick_score = (base_title_score * 0.7) + (artist_score * 0.3)
            preliminary.append((quick_score, sp_album, base_title_score, artist_score))

        preliminary.sort(key=lambda x: x[0], reverse=True)
        max_candidates = self.config['max_album_candidates'].get(int)
        if max_candidates > 0 and len(preliminary) > max_candidates:
            log.info(
                f"  -> Limiting album candidates from {len(preliminary)} to {max_candidates} "
                "to reduce API calls."
            )
            preliminary = preliminary[:max_candidates]

        return preliminary

    def _build_detailed_candidates(self, local_album, local_items, preliminary):
        candidates = []
        candidate_ids = [sp_album['id'] for _, sp_album, _, _ in preliminary]
        details_by_id = {}
        if candidate_ids:
            details_by_id = self.client.get_albums_bulk(candidate_ids)

        for _, sp_album, base_title_score, artist_score in preliminary:
            album_details = details_by_id.get(sp_album['id'])
            album_data = album_details if isinstance(album_details, dict) else sp_album
            candidate_tracks = self.client.get_album_tracks(sp_album['id'])
            if not candidate_tracks:
                continue

            score = calculate_match_score(local_album, local_items, candidate_tracks, album_data)
            track_count = len(candidate_tracks)
            related_artist_threshold = self.config['related_artist_threshold'].as_number()
            # A "variant" is only meaningful when it is by the same artist — otherwise it's a different album
            is_variant = (
                is_variant_title(album_data.get('name', ''))
                and base_title_score >= VARIANT_TITLE_THRESHOLD
                and artist_score >= related_artist_threshold
            )
            popularity = None
            if album_details and isinstance(album_details, dict):
                popularity = album_details.get('popularity') or 0

            candidates.append({
                "score": score,
                "album": album_data,
                "track_count": track_count,
                "is_variant": is_variant,
                "popularity": popularity,
                "base_title_score": base_title_score,
                "artist_score": artist_score,
                "tracks": candidate_tracks,
            })

        return candidates

    def _select_album_candidate(self, local_album, local_items, candidates, interactive):
        candidates_by_score = sorted(candidates, key=lambda c: c["score"], reverse=True)
        all_candidates = candidates_by_score
        related_artist_threshold = self.config['related_artist_threshold'].as_number()
        related_candidates = [
            c for c in candidates_by_score if c["artist_score"] >= related_artist_threshold
        ]
        if not related_candidates:
            log.warning(
                f"  -> No candidate releases met artist match threshold ({related_artist_threshold:.2f})."
            )
            if interactive and self.prompter:
                log.info("No related releases found. You can enter a Spotify album ID/URL.")
                selected = self._prompt(all_candidates, local_album, local_items)
                if not selected:
                    return None, []
                supplemental = self._get_supplemental_candidates(selected, all_candidates)
                return selected["album"], supplemental
            return None, []

        candidates_by_score = related_candidates
        best_score = candidates_by_score[0]["score"]
        match_threshold = self.config['match_threshold'].as_number()
        margin = self.config['certainty_margin'].as_number()

        top_popularity = candidates_by_score[0]["popularity"]
        top_popularity_display = top_popularity if top_popularity is not None else "n/a"
        log.debug(
            f"Top candidate by score for '{local_album.album}': "
            f"'{candidates_by_score[0]['album']['name']}' "
            f"(score {best_score:.2f}, pop {top_popularity_display})"
        )

        is_strong_match = best_score >= match_threshold

        is_clear_winner = False
        if len(candidates_by_score) > 1:
            second_best_score = candidates_by_score[1]["score"]
            if (best_score - second_best_score) > margin:
                is_clear_winner = True
                log.debug(
                    f"  -> Match is a clear winner. "
                    f"Margin: {best_score - second_best_score:.2f} > {margin}"
                )

        if not (is_strong_match or is_clear_winner):
            if interactive and self.prompter:
                # The prompter prints its own heading; see cli.InteractivePrompter.
                log.debug(f"Uncertain match for '{local_album.album}'. Prompting.")
                pop_limit = self.config['max_album_popularity_checks'].get(int)
                display_limit = min(5, len(candidates_by_score))
                if pop_limit > 0 and display_limit > 0:
                    self._populate_album_popularity(
                        candidates_by_score, limit=min(pop_limit, display_limit),
                    )
                selected = self._prompt(candidates_by_score, local_album, local_items)
                if not selected:
                    return None, []
                supplemental = self._get_supplemental_candidates(selected, candidates_by_score)
                return selected["album"], supplemental
            return None, []

        eligible = [c for c in candidates_by_score if c["score"] >= best_score - margin]
        eligible_non_variants = [c for c in eligible if not c["is_variant"]]
        if eligible_non_variants:
            log.info(f"  -> Preferring standard release candidates for '{local_album.album}'.")
            eligible = eligible_non_variants
        else:
            log.info(
                f"  -> No standard release candidate available for '{local_album.album}'. "
                "Using related/variant release candidates."
            )

        max_pop_checks = self.config['max_album_popularity_checks'].get(int)
        if max_pop_checks != 0:
            self._populate_album_popularity(eligible, limit=max_pop_checks)

        eligible.sort(key=lambda c: (c["score"], c.get("popularity") or 0), reverse=True)
        selected = eligible[0]
        selected_popularity = selected["popularity"]
        selected_popularity_display = selected_popularity if selected_popularity is not None else "n/a"
        log.info(
            f"  -> Selected: '{selected['album']['name']}' "
            f"(score {selected['score']:.2f}, pop {selected_popularity_display})"
        )

        supplemental = self._get_supplemental_candidates(selected, candidates_by_score)
        return selected["album"], supplemental

    def _prompt(self, candidates, local_album, local_items):
        return self.prompter(candidates, local_album, local_items, self.build_candidate_from_album_id)

    def _get_supplemental_candidates(self, selected_candidate, candidates):
        if not selected_candidate:
            return []
        selected_id = selected_candidate["album"]["id"]
        related_artist_threshold = self.config['related_artist_threshold'].as_number()
        supplemental = []
        for candidate in candidates:
            if candidate["album"]["id"] == selected_id:
                continue
            if candidate["base_title_score"] < VARIANT_TITLE_THRESHOLD:
                continue
            if candidate["artist_score"] < related_artist_threshold:
                continue
            supplemental.append(candidate)

        supplemental.sort(key=lambda c: ((c["popularity"] or 0), c["score"]), reverse=True)
        return supplemental

    def _populate_album_popularity(self, candidates, limit=None):
        if not candidates:
            return
        to_fetch = [c for c in candidates if c.get("popularity") is None]
        if not to_fetch:
            return
        if limit is not None and limit > 0:
            to_fetch = to_fetch[:limit]

        album_ids = [c["album"]["id"] for c in to_fetch]
        details_by_id = self.client.get_albums_bulk(album_ids)
        for candidate in to_fetch:
            details = details_by_id.get(candidate["album"]["id"])
            if not details:
                details = self.client.get_album(candidate["album"]["id"])
            popularity = 0
            if details and isinstance(details, dict):
                popularity = details.get('popularity') or 0
            candidate["popularity"] = popularity
        for candidate in candidates:
            if candidate.get("popularity") is None:
                candidate["popularity"] = 0

    # ------------------------------------------------------------------
    # Track matching
    # ------------------------------------------------------------------

    def match_items_to_tracks(self, items, spotify_tracks, dry_run, *, context=None, overwrite=False):
        """Pair up local items with Spotify tracks; write IDs unless dry-run.

        Returns the list of items that could not be matched.
        """
        unmatched_spotify_tracks = list(spotify_tracks)
        log_prefix = "[DRY RUN] " if dry_run else ""
        context_prefix = f"{context} " if context else ""
        unmatched_items = []

        for item in items:
            existing_id = item.get('spotify_track_id')
            if existing_id and not overwrite:
                continue

            matched_track = self.find_matching_spotify_track(item, unmatched_spotify_tracks)
            if matched_track:
                new_id = matched_track['id']
                if existing_id and existing_id == new_id:
                    log.debug(
                        f"  -> {context_prefix}Verified (unchanged) '{item.title}' -> {new_id}"
                    )
                else:
                    action = "Updated" if existing_id else "Matched"
                    log.info(
                        f"  -> {log_prefix}{context_prefix}{action} '{item.title}' -> Spotify ID: {new_id}"
                    )
                    if not dry_run:
                        item['spotify_track_id'] = new_id
                        set_artist_id(item, matched_track, label=item.title)
                        item.store()
                unmatched_spotify_tracks.remove(matched_track)
            else:
                log.warning(f"  -> {context_prefix}No Spotify match found for track: '{item.title}'")
                unmatched_items.append(item)

        return unmatched_items

    def find_matching_spotify_track(self, item, spotify_tracks):
        return find_matching_spotify_track(
            item,
            spotify_tracks,
            duration_tolerance=self.config['duration_tolerance'].get(int),
            match_threshold=self.config['track_match_threshold'].as_number(),
            min_artist_score=self.config['min_track_artist_score'].as_number(),
            duration_mismatch_penalty_threshold=(
                self.config['duration_mismatch_penalty_threshold'].get(int)
            ),
            duration_mismatch_penalty=self.config['duration_mismatch_penalty'].as_number(),
        )
