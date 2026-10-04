"""How much to trust an ID, and when a new one may replace a stored one.

Every ID crossref writes is recorded with the method that found it, in a
sibling field (`deezer_album_id` -> `deezer_album_id_source`).  An ID stored
by anything else (an import, another plugin) has no
record and counts as fuzzy, the weakest evidence.
"""

from __future__ import annotations

RANK = {"fuzzy": 1, "isrc": 2, "barcode": 3, "musicbrainz": 4}
ORDER = ("musicbrainz", "barcode", "isrc", "fuzzy")  # strongest first


def source_field(field: str) -> str:
    return f"{field}_source"


def stored_method(obj, field: str) -> str | None:
    """The method behind obj's own value of `field`, or None when it has none.

    Reads the object's own flexible values: beets lets an item see its
    album's value through get(), which would make an item look resolved.
    """
    flex = obj._values_flex
    if not flex.get(field):
        return None
    return flex.get(source_field(field)) or "fuzzy"


def can_improve(current: str | None, method: str) -> bool:
    """Does `method` beat what is stored?  Also the test for overwriting:
    an equal or weaker method never replaces a stored ID (and finding the
    same ID by a stronger method just upgrades its record)."""
    return current is None or RANK[method] > RANK[current]
