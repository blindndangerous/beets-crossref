"""Beets-free helpers: fuzzy matching, normalization, Spotify query building.

Importable without any beets/spotipy dependencies. Most of these are pure
functions; `set_artist_id` and `discard_artist_id` are the exceptions, mutating
the album/item they are handed and logging as they go.
"""
import contextlib
import logging
import re

from thefuzz import fuzz

log = logging.getLogger("beets.spotify_album_match")

VARIANT_KEYWORDS = [
    "deluxe",
    "expanded",
    "remaster",
    "remastered",
    "anniversary",
    "special edition",
    "collector's edition",
    "bonus",
    "reissue",
    "re-release",
    "rerelease",
    "live",
    "remix",
    "remixes",
    "mono",
    "stereo",
    "instrumental",
    "acoustic",
    "demo",
    "soundtrack",
    "ost",
]
STRIP_KEYWORDS = VARIANT_KEYWORDS + [
    "edition",
    "version",
]
VARIANT_KEYWORDS_RE = "|".join(re.escape(k) for k in VARIANT_KEYWORDS)
STRIP_KEYWORDS_RE = "|".join(re.escape(k) for k in STRIP_KEYWORDS)
VARIANT_PATTERN = re.compile(r"\b(" + VARIANT_KEYWORDS_RE + r")\b", re.IGNORECASE)
STRIP_PATTERN = re.compile(r"\b(" + STRIP_KEYWORDS_RE + r")\b", re.IGNORECASE)
STRIP_BRACKET_PATTERN = re.compile(
    r"[\(\[][^)\]]*\b(" + STRIP_KEYWORDS_RE + r")\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)
STRIP_TRAIL_PATTERN = re.compile(
    r"[-:]\s*[^-:]*\b(" + STRIP_KEYWORDS_RE + r")\b[^-:]*$",
    re.IGNORECASE,
)
ARTIST_SEPARATOR_PATTERN = re.compile(
    r"(?:\s*,\s*|\s*&\s*|\s*;\s*|\s*\+\s*|\s+and\s+|\s+vs\.?\s+|"
    r"\s+with\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|\s+x\s+|\s+/\s+)",
    re.IGNORECASE,
)
SPOTIFY_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{22}$")


def clean_spotify_id(value):
    """Return a normalized Spotify ID, or None for blank/malformed values."""
    if value is None:
        return None
    value = str(value).strip()
    if SPOTIFY_ID_PATTERN.fullmatch(value):
        return value
    return None


def album_barcode(local_album):
    """Return a beets album's barcode as a plain string, or None.

    beets stores it in the fixed 'barcode' field (beets 2.2.0 library.py:1188);
    there is no 'upc' field, so a value under that name can only be one a user
    added by hand.
    """
    values = [getattr(local_album, 'barcode', None)]
    getter = getattr(local_album, 'get', None)
    if callable(getter):
        values.append(getter('upc'))
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def primary_artist_id(spotify_obj):
    """Return the cleaned Spotify ID of an album's or track's primary artist.

    Returns None when the object carries no usable ``artists`` entry.
    """
    if not isinstance(spotify_obj, dict):
        return None
    artists = spotify_obj.get('artists') or []
    if not artists:
        return None
    first_artist = artists[0]
    if not isinstance(first_artist, dict):
        return None
    return clean_spotify_id(first_artist.get('id'))


def own_artist_id(obj):
    """Artist ID stored on this object itself (beets Items fall back to their album).

    Reads _values_flex directly: every beets Model has it, and there is no
    safe fallback -- ``obj.get`` on an Item would return the album's value,
    which is exactly what this helper exists to avoid.
    """
    return obj._values_flex.get('spotify_artist_id')


def discard_artist_id(obj):
    """Drop this object's own 'spotify_artist_id'; it never outlives its album/track ID.

    Deletes tolerantly rather than guarding with ``in``: on a beets Item
    ``'spotify_artist_id' in item`` is also True when only the item's album
    carries the field, and the ``del`` would then raise.
    """
    with contextlib.suppress(KeyError):
        del obj['spotify_artist_id']


def set_artist_id(obj, spotify_obj, label=''):
    """Store 'spotify_artist_id' on a beets album/item from a Spotify object.

    When the primary artist ID is missing or malformed, any artist ID already
    on the object is deleted: the caller is storing a new album/track ID, and a
    leftover artist ID would then belong to the previous match. The caller owns
    the dry-run check and the following ``store()`` call.
    Returns True when a value was written.
    """
    artist_id = primary_artist_id(spotify_obj)
    if not artist_id:
        log.debug(
            f"No usable primary Spotify artist ID for '{label}'. "
            "Storing none and dropping any stale value."
        )
        discard_artist_id(obj)
        return False
    obj['spotify_artist_id'] = artist_id
    return True


def strip_version_tokens(text):
    if not text:
        return ""
    stripped = STRIP_BRACKET_PATTERN.sub(" ", text)
    stripped = STRIP_TRAIL_PATTERN.sub(" ", stripped)
    stripped = STRIP_PATTERN.sub(" ", stripped)
    return " ".join(stripped.split())


def normalize_text(text):
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = re.sub(r"\b(feat|ft|featuring)\b.*", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_title(text):
    stripped = normalize_text(strip_version_tokens(text))
    if stripped:
        return stripped
    # A title made only of variant keywords -- "Live", "Bonus", "Remixes" --
    # strips to nothing and would then score 0 even against itself. Fall back
    # to the unstripped text so those titles can still match.
    return normalize_text(text)


def is_variant_title(text):
    if not text:
        return False
    return bool(VARIANT_PATTERN.search(text))


def _fuzz_max_score(a, b):
    return max(
        fuzz.token_set_ratio(a, b),
        fuzz.partial_ratio(a, b),
        fuzz.ratio(a, b),
    ) / 100


def fuzzy_score(a, b):
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0
    return _fuzz_max_score(a_norm, b_norm)


def fuzzy_title_score(a, b):
    a_norm = normalize_title(a)
    b_norm = normalize_title(b)
    if not a_norm or not b_norm:
        return 0
    return _fuzz_max_score(a_norm, b_norm)


def normalize_artist_part(text):
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def split_artist_tokens(text):
    if not text:
        return []
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    parts = [part.strip() for part in ARTIST_SEPARATOR_PATTERN.split(text) if part.strip()]
    tokens = []
    for part in parts:
        normalized = normalize_artist_part(part)
        if normalized:
            tokens.append(normalized)
    return tokens


def get_artist_tokens(artist_names):
    tokens = []
    if isinstance(artist_names, str):
        tokens.extend(split_artist_tokens(artist_names))
    else:
        for name in artist_names:
            tokens.extend(split_artist_tokens(name))
    seen = set()
    unique = []
    for token in tokens:
        if token not in seen:
            unique.append(token)
            seen.add(token)
    return unique


def artist_fuzzy_score(a, b):
    a_norm = normalize_artist_part(a)
    b_norm = normalize_artist_part(b)
    if not a_norm or not b_norm:
        return 0
    return _fuzz_max_score(a_norm, b_norm)


def artist_set_score(local_artist, candidate_artists):
    local_tokens = get_artist_tokens(local_artist)
    candidate_tokens = get_artist_tokens(candidate_artists)
    if not local_tokens or not candidate_tokens:
        return 0.0

    def avg_best(source, target):
        scores = []
        for token in source:
            best = 0.0
            for cand in target:
                score = artist_fuzzy_score(token, cand)
                if score > best:
                    best = score
            scores.append(best)
        return sum(scores) / len(scores) if scores else 0.0

    return (avg_best(local_tokens, candidate_tokens) + avg_best(candidate_tokens, local_tokens)) / 2


def escape_query_value(value):
    """Make a value safe to drop inside a quoted Spotify search field.

    Spotify's search syntax has no escape mechanism, so a backslash-escaped
    quote is matched literally and the query returns nothing. Dropping the
    characters costs a little precision and always returns results.
    """
    if value is None:
        return ""
    clean = str(value).replace("\\", " ").replace('"', " ")
    return " ".join(clean.split())


def _add_unique_query(queries, seen, query):
    if not query:
        return
    key = " ".join(query.split()).lower()
    if key in seen:
        return
    seen.add(key)
    queries.append(query)


def build_album_search_queries(album_title, album_artist):
    album = album_title or ""
    album_stripped = strip_version_tokens(album)
    artist_full = album_artist or ""
    artist_tokens = get_artist_tokens(artist_full)
    primary_artist = artist_tokens[0] if artist_tokens else artist_full

    album_q = escape_query_value(album)
    album_stripped_q = escape_query_value(album_stripped)
    artist_full_q = escape_query_value(artist_full)
    primary_artist_q = escape_query_value(primary_artist)

    queries = []
    seen = set()

    if album_q and artist_full_q:
        _add_unique_query(queries, seen, f'album:"{album_q}" artist:"{artist_full_q}"')
    if album_stripped_q and album_stripped_q != album_q and artist_full_q:
        _add_unique_query(queries, seen, f'album:"{album_stripped_q}" artist:"{artist_full_q}"')
    if album_q and primary_artist_q and primary_artist_q != artist_full_q:
        _add_unique_query(queries, seen, f'album:"{album_q}" artist:"{primary_artist_q}"')
    if album_stripped_q and primary_artist_q and (
        album_stripped_q != album_q or primary_artist_q != artist_full_q
    ):
        _add_unique_query(queries, seen, f'album:"{album_stripped_q}" artist:"{primary_artist_q}"')
    if album_q:
        _add_unique_query(queries, seen, f'album:"{album_q}"')
    if album_stripped_q and album_stripped_q != album_q:
        _add_unique_query(queries, seen, f'album:"{album_stripped_q}"')

    return queries


def build_track_search_queries(item_title, item_artist, item_album_title):
    title = item_title or ""
    title_stripped = strip_version_tokens(title)
    artist_text = item_artist or ""
    artist_tokens = get_artist_tokens(artist_text)
    primary_artist = artist_tokens[0] if artist_tokens else artist_text
    album_title = item_album_title or ""
    album_stripped = strip_version_tokens(album_title)

    title_q = escape_query_value(title)
    title_stripped_q = escape_query_value(title_stripped)
    artist_full_q = escape_query_value(artist_text)
    primary_artist_q = escape_query_value(primary_artist)
    album_q = escape_query_value(album_title)
    album_stripped_q = escape_query_value(album_stripped)

    queries = []
    seen = set()

    if title_q and artist_full_q and album_q:
        _add_unique_query(queries, seen, f'track:"{title_q}" artist:"{artist_full_q}" album:"{album_q}"')
    if title_stripped_q and title_stripped_q != title_q and artist_full_q and album_q:
        _add_unique_query(
            queries, seen,
            f'track:"{title_stripped_q}" artist:"{artist_full_q}" album:"{album_q}"',
        )
    if title_q and primary_artist_q and album_q and primary_artist_q != artist_full_q:
        _add_unique_query(queries, seen, f'track:"{title_q}" artist:"{primary_artist_q}" album:"{album_q}"')
    if (
        title_stripped_q
        and title_stripped_q != title_q
        and primary_artist_q
        and album_q
        and primary_artist_q != artist_full_q
    ):
        _add_unique_query(
            queries, seen,
            f'track:"{title_stripped_q}" artist:"{primary_artist_q}" album:"{album_q}"',
        )
    if title_q and primary_artist_q and album_stripped_q and album_stripped_q != album_q:
        _add_unique_query(
            queries, seen,
            f'track:"{title_q}" artist:"{primary_artist_q}" album:"{album_stripped_q}"',
        )
    if (
        title_stripped_q
        and title_stripped_q != title_q
        and primary_artist_q
        and album_stripped_q
        and album_stripped_q != album_q
    ):
        _add_unique_query(
            queries, seen,
            f'track:"{title_stripped_q}" artist:"{primary_artist_q}" album:"{album_stripped_q}"',
        )
    if title_q and artist_full_q:
        _add_unique_query(queries, seen, f'track:"{title_q}" artist:"{artist_full_q}"')
    if title_q and primary_artist_q and primary_artist_q != artist_full_q:
        _add_unique_query(queries, seen, f'track:"{title_q}" artist:"{primary_artist_q}"')
    if title_stripped_q and title_stripped_q != title_q and artist_full_q:
        _add_unique_query(queries, seen, f'track:"{title_stripped_q}" artist:"{artist_full_q}"')
    if (
        title_stripped_q
        and title_stripped_q != title_q
        and primary_artist_q
        and primary_artist_q != artist_full_q
    ):
        _add_unique_query(queries, seen, f'track:"{title_stripped_q}" artist:"{primary_artist_q}"')
    if title_q:
        _add_unique_query(queries, seen, f'track:"{title_q}"')
    if title_stripped_q and title_stripped_q != title_q:
        _add_unique_query(queries, seen, f'track:"{title_stripped_q}"')

    return queries


def find_matching_spotify_track(
    item,
    spotify_tracks,
    duration_tolerance,
    match_threshold=0.7,
    min_artist_score=0.55,
    duration_mismatch_penalty_threshold=10,
    duration_mismatch_penalty=0.10,
):
    # ISRC short-circuit. Only fires for full Spotify track objects: the
    # simplified objects in an album's track list carry no external_ids, so
    # every caller that passes /albums/{id}/tracks output skips this.
    if item.isrc:
        for track in spotify_tracks:
            if track.get("external_ids", {}).get("isrc", "").lower() == item.isrc.lower():
                return track

    item_artist = item.artist or item.albumartist or ""
    best_track = None
    best_score = 0.0

    for track in spotify_tracks:
        title_score = fuzzy_title_score(item.title, track.get("name", ""))
        if title_score <= 0:
            continue

        candidate_artists = [artist.get("name", "") for artist in track.get("artists", [])]
        if item_artist:
            artist_score = artist_set_score(item_artist, candidate_artists)
            if artist_score < min_artist_score:
                continue
        else:
            # No artist info available — use a neutral score rather than 0 or 1
            artist_score = 0.5

        # A file with no disc tag has disc 0 in beets, which is common; treat
        # both sides' missing disc as disc 1 so the position bonus still
        # separates tracks whose titles are near-identical.
        local_disc = item.disc or 1
        spotify_disc = track.get("disc_number") or 1
        position_bonus = 0.0
        if (
            item.track
            and track.get("track_number") == item.track
            and spotify_disc == local_disc
        ):
            position_bonus = 0.15

        duration_adjustment = 0.0
        if item.length:
            spotify_duration = track.get("duration_ms", 0) / 1000
            if spotify_duration:
                diff = abs(spotify_duration - item.length)
                if diff <= duration_tolerance:
                    duration_adjustment = 0.05
                elif diff > duration_mismatch_penalty_threshold:
                    duration_adjustment = -duration_mismatch_penalty

        score = (title_score * 0.6) + (artist_score * 0.25) + position_bonus + duration_adjustment
        if score > best_score:
            best_score = score
            best_track = track

    if best_track and best_score >= match_threshold:
        return best_track
    return None


def album_type_score(album_type):
    if album_type == "album":
        return 1.0
    if album_type == "compilation":
        return 0.7
    if album_type == "single":
        return 0.5
    return 0.6


def calculate_fuzzy_title_score(local_items, spotify_tracks):
    local_titles = [normalize_title(item.title) for item in local_items]
    spotify_titles = [normalize_title(track.get('name', '')) for track in spotify_tracks]

    def avg_best_match(source, target):
        total = 0
        count = 0
        for title1 in source:
            if not title1:
                continue
            best = max(
                (fuzzy_score(title1, title2) for title2 in target if title2),
                default=0,
            )
            total += best
            count += 1
        return (total / count) if count > 0 else 0

    fwd = avg_best_match(local_titles, spotify_titles)
    rev = avg_best_match(spotify_titles, local_titles)
    return (fwd + rev) / 2 if (fwd or rev) else 0


def calculate_match_score(local_album, local_items, spotify_tracks, sp_album_candidate):
    title_score = calculate_fuzzy_title_score(local_items, spotify_tracks)
    local_count = len(local_items)
    spotify_count = len(spotify_tracks)
    if local_count > 0 and spotify_count > 0:
        track_count_similarity = 1.0 - (abs(local_count - spotify_count) / max(local_count, spotify_count))
        track_count_similarity = max(0.0, track_count_similarity)
    else:
        track_count_similarity = 0.0

    album_title_score = fuzzy_title_score(local_album.album, sp_album_candidate.get('name', ''))
    artist_score = artist_set_score(
        local_album.albumartist,
        [artist.get('name', '') for artist in sp_album_candidate.get('artists', [])],
    )
    type_score = album_type_score(sp_album_candidate.get('album_type', ''))

    year_score = 0.0
    try:
        sp_year = int(sp_album_candidate['release_date'].split('-')[0])
        if local_album.year:
            diff = abs(sp_year - local_album.year)
            if diff == 0:
                year_score = 1.0
            elif diff == 1:
                year_score = 0.5
    except (ValueError, IndexError, KeyError, AttributeError):
        pass

    # Weights sum to 1.0; all-perfect → 1.0 exactly.
    return (
        (title_score * 0.45) +
        (album_title_score * 0.20) +
        (artist_score * 0.15) +
        (track_count_similarity * 0.10) +
        (type_score * 0.05) +
        (year_score * 0.05)
    )
