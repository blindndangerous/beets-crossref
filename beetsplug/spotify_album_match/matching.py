"""Album and track matching against Spotify.

The AlbumMatcher class owns:
    - Album candidate search (by UPC, then text queries)
    - Candidate scoring and selection (with optional interactive disambiguation)
    - Track matching primitives shared between authoritative-album mapping and fallback search
"""
import logging

from spotipy.exceptions import SpotifyException

from .helpers import (
    artist_set_score,
    build_album_search_queries,
    build_track_search_queries,
    calculate_match_score,
    find_matching_spotify_track,
    fuzzy_title_score,
    is_variant_title,
    set_artist_id,
)

log = logging.getLogger("beets.spotify_album_match")

SEARCH_LIMIT = 3
TRACK_SEARCH_LIMIT = 5
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

        Tries UPC first, then text-search candidates. Selection considers
        title/artist scores, popularity, and variant-vs-standard preference.
        """
        if hasattr(local_album, 'upc') and local_album.upc:
            query = f'upc:{local_album.upc}'
            log.info(f"  -> Searching Spotify by UPC: {query}")
            try:
                results = self.client.search(q=query, type='album', limit=1)
                if results and results.get("albums", {}).get("items"):
                    log.info(f"  -> Found direct match via UPC for '{local_album.album}'")
                    return results["albums"]["items"][0], []
            except SpotifyException:
                log.warning("  -> Searching by UPC failed. Falling back to search.")

        candidates_by_id = self._search_album_candidates(local_album)
        if not candidates_by_id:
            return None, []

        local_items = list(local_album.items())
        preliminary = self._build_preliminary_candidates(local_album, candidates_by_id)
        candidates = self._build_detailed_candidates(local_album, local_items, preliminary)
        if not candidates:
            return None, []

        return self._select_album_candidate(local_album, local_items, candidates, interactive)

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
                log.info(f"Uncertain match for '{local_album.album}'. Please choose an option:")
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
            f"  -> Selected by popularity: '{selected['album']['name']}' "
            f"(pop {selected_popularity_display}, score {selected['score']:.2f})"
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

    def search_spotify_track(self, item, album, *, min_artist_score=None):
        """Search Spotify for a single track by ISRC or query variants.

        When `min_artist_score` is given it overrides the configured
        min_track_artist_score for this lookup; otherwise the configured value applies.
        """
        if not item.title and not item.isrc:
            return None

        if item.isrc:
            try:
                results = self.client.search(
                    q=f"isrc:{item.isrc}",
                    type="track",
                    limit=TRACK_SEARCH_LIMIT,
                )
            except SpotifyException:
                log.warning(f"Could not search Spotify by ISRC for '{item.title}'.")
            else:
                match = self.select_best_track(item, results, min_artist_score=min_artist_score)
                if match:
                    return match

        max_queries = self.config['max_track_search_queries'].get(int)
        artist_text = item.artist or item.albumartist or ""
        album_title = album.album if album else ""
        queries = build_track_search_queries(item.title, artist_text, album_title)
        if max_queries > 0:
            queries = queries[:max_queries]
        for query in queries:
            log.debug(f"  -> Searching Spotify track with query: {query}")
            try:
                results = self.client.search(q=query, type="track", limit=TRACK_SEARCH_LIMIT)
            except SpotifyException:
                log.warning(f"Spotify track search failed for query: {query}")
                continue
            match = self.select_best_track(item, results, min_artist_score=min_artist_score)
            if match:
                return match

        return None

    def select_best_track(self, item, results, *, min_artist_score=None):
        """Pick the best Spotify track from a search-results dict.

        ISRC matches short-circuit. Otherwise scores by title + artist + duration + album type.
        `min_artist_score` overrides the configured floor for this lookup.
        """
        tracks = results.get("tracks", {}).get("items", []) if results else []
        if not tracks:
            return None

        if item.isrc:
            for track in tracks:
                track_isrc = track.get('external_ids', {}).get('isrc', '')
                if track_isrc and track_isrc.lower() == item.isrc.lower():
                    return track

        floor = (
            min_artist_score
            if min_artist_score is not None
            else self.config['min_track_artist_score'].as_number()
        )
        best_track = None
        best_score = 0.0
        artist_text = item.artist or item.albumartist or ""
        for track in tracks:
            if artist_text:
                artist_score = artist_set_score(
                    artist_text,
                    [artist.get('name', '') for artist in track.get('artists', [])],
                )
                if artist_score < floor:
                    continue
            else:
                artist_score = 0.5

            score = self.score_track_candidate(item, track, artist_score=artist_score)
            if score > best_score:
                best_score = score
                best_track = track

        threshold = self.config['track_match_threshold'].as_number()
        if best_track and best_score >= threshold:
            return best_track
        return None

    def score_track_candidate(self, item, track, *, artist_score=None):
        title_score = fuzzy_title_score(item.title, track.get('name', ''))
        if artist_score is None:
            artist_text = item.artist or item.albumartist or ""
            artist_score = artist_set_score(
                artist_text,
                [artist.get('name', '') for artist in track.get('artists', [])],
            )

        # Duration score: 1.0 within tolerance, 0.0 outside, 0.5 neutral when unknown.
        duration_score = 0.5
        if item.length:
            track_duration = track.get('duration_ms', 0) / 1000
            if track_duration:
                duration_tolerance = self.config['duration_tolerance'].get(int)
                duration_score = 1.0 if abs(track_duration - item.length) <= duration_tolerance else 0.0

        type_score = 1.0 if track.get('album', {}).get('album_type') == "album" else 0.5

        # Weights sum to 1.0; all-perfect → 1.0 exactly.
        return (
            (title_score * 0.55) +
            (artist_score * 0.30) +
            (duration_score * 0.10) +
            (type_score * 0.05)
        )
