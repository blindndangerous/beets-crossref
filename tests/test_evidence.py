import pytest
from beets.library import Item, Library

from beetsplug.crossref import evidence


@pytest.fixture
def lib(tmp_path):
    return Library(str(tmp_path / "lib.db"))


def test_stored_id_without_source_counts_as_fuzzy(lib):
    item = Item(title="t")
    item["deezer_track_id"] = "9"
    assert evidence.stored_method(item, "deezer_track_id") == "fuzzy"


def test_recorded_source_is_returned(lib):
    item = Item(title="t")
    item["deezer_track_id"] = "9"
    item["deezer_track_id_source"] = "isrc"
    assert evidence.stored_method(item, "deezer_track_id") == "isrc"


def test_no_id_means_no_method_even_with_stale_source(lib):
    item = Item(title="t")
    item["deezer_track_id_source"] = "isrc"
    assert evidence.stored_method(item, "deezer_track_id") is None


def test_item_reads_its_own_value_not_the_albums(lib):
    item = Item(title="t", track=1)
    lib.add(item)
    album = lib.add_album([item])
    album["deezer_album_id"] = "55"
    album["deezer_album_id_source"] = "musicbrainz"
    album.store(inherit=False)
    child = lib.get_item(item.id)
    assert child.get("deezer_album_id") == "55"  # beets falls back to the album ...
    assert evidence.stored_method(child, "deezer_album_id") is None  # ... crossref must not
    assert evidence.stored_method(album, "deezer_album_id") == "musicbrainz"


@pytest.mark.parametrize(
    ("current", "method", "expected"),
    [
        (None, "fuzzy", True),
        ("fuzzy", "isrc", True),
        ("isrc", "barcode", True),
        ("barcode", "musicbrainz", True),
        ("fuzzy", "fuzzy", False),
        ("isrc", "fuzzy", False),
        ("musicbrainz", "musicbrainz", False),
        ("musicbrainz", "barcode", False),
    ],
)
def test_can_improve_ordering(current, method, expected):
    assert evidence.can_improve(current, method) is expected


def test_order_is_strongest_first_and_matches_rank():
    ranks = [evidence.RANK[m] for m in evidence.ORDER]
    assert ranks == sorted(ranks, reverse=True)
