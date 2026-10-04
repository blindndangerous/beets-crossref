"""Spotify ID validation and the fuzzy text scoring used by the Spotify search."""

from __future__ import annotations

import re

from thefuzz import fuzz

STRIP_KEYWORDS = (
    "deluxe", "expanded", "remaster", "remastered", "anniversary", "special edition",
    "collector's edition", "bonus", "reissue", "re-release", "rerelease", "live", "remix",
    "remixes", "mono", "stereo", "instrumental", "acoustic", "demo", "soundtrack", "ost",
    "edition", "version",
)
_KW = "|".join(re.escape(k) for k in STRIP_KEYWORDS)
STRIP_PATTERN = re.compile(rf"\b({_KW})\b", re.IGNORECASE)
STRIP_BRACKET_PATTERN = re.compile(rf"[\(\[][^)\]]*\b({_KW})\b[^)\]]*[\)\]]", re.IGNORECASE)
STRIP_TRAIL_PATTERN = re.compile(rf"[-:]\s*[^-:]*\b({_KW})\b[^-:]*$", re.IGNORECASE)
ARTIST_SEPARATOR_PATTERN = re.compile(
    r"(?:\s*,\s*|\s*&\s*|\s*;\s*|\s*\+\s*|\s+and\s+|\s+vs\.?\s+|"
    r"\s+with\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|\s+x\s+|\s+/\s+)",
    re.IGNORECASE,
)
SPOTIFY_ID_PATTERN = re.compile(r"[A-Za-z0-9]{22}")


def clean_spotify_id(value) -> str | None:
    """The ID when it is exactly 22 base62 characters, else None."""
    if value is None:
        return None
    value = str(value).strip()
    return value if SPOTIFY_ID_PATTERN.fullmatch(value) else None


def strip_version_tokens(text: str | None) -> str:
    if not text:
        return ""
    stripped = STRIP_BRACKET_PATTERN.sub(" ", text)
    stripped = STRIP_TRAIL_PATTERN.sub(" ", stripped)
    return " ".join(STRIP_PATTERN.sub(" ", stripped).split())


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text.lower())
    text = re.sub(r"\b(feat|ft|featuring)\b.*", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def normalize_title(text: str | None) -> str:
    # A title made only of version words ("Live", "Remixes") strips to nothing;
    # fall back to the unstripped text so it can still match itself.
    return normalize_text(strip_version_tokens(text)) or normalize_text(text)


def normalize_artist_part(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _fuzz_max_score(a: str, b: str) -> float:
    return max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b), fuzz.ratio(a, b)) / 100


def fuzzy_title_score(a: str | None, b: str | None) -> float:
    a_norm, b_norm = normalize_title(a), normalize_title(b)
    if not a_norm or not b_norm:
        return 0.0
    return _fuzz_max_score(a_norm, b_norm)


def get_artist_tokens(artist_names) -> list[str]:
    names = [artist_names] if isinstance(artist_names, str) else artist_names
    tokens = []
    for name in names:
        name = re.sub(r"[\(\[].*?[\)\]]", " ", name or "")
        for part in ARTIST_SEPARATOR_PATTERN.split(name):
            normalized = normalize_artist_part(part)
            if normalized:
                tokens.append(normalized)
    return list(dict.fromkeys(tokens))


def artist_set_score(local_artist, candidate_artists) -> float:
    """Symmetric best-match average over the artists on each side."""
    local = get_artist_tokens(local_artist or "")
    candidate = get_artist_tokens(candidate_artists)
    if not local or not candidate:
        return 0.0

    def avg_best(source, target):
        return sum(max(_fuzz_max_score(s, t) for t in target) for s in source) / len(source)

    return (avg_best(local, candidate) + avg_best(candidate, local)) / 2


def escape_query_value(value) -> str:
    """Spotify search has no quote escaping; drop quotes and backslashes instead."""
    if value is None:
        return ""
    return " ".join(str(value).replace("\\", " ").replace('"', " ").split())


def build_album_search_queries(album_title, album_artist) -> list[str]:
    """Query variants, most specific first, duplicates dropped."""
    album = album_title or ""
    artist_full = album_artist or ""
    tokens = get_artist_tokens(artist_full)
    primary = tokens[0] if tokens else artist_full
    albums = [escape_query_value(album), escape_query_value(strip_version_tokens(album))]
    artists = [escape_query_value(artist_full), escape_query_value(primary)]
    queries: dict[str, None] = {}
    for artist_q in artists:
        for album_q in albums:
            if album_q and artist_q:
                queries.setdefault(f'album:"{album_q}" artist:"{artist_q}"', None)
    for album_q in albums:
        if album_q:
            queries.setdefault(f'album:"{album_q}"', None)
    return list(queries)
