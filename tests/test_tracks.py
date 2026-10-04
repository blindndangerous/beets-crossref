from types import SimpleNamespace

from beetsplug.crossref.tracks import TrackHit, item_isrcs, match_tracks


def item(item_id, track, length, isrc="", disc=1):
    data = {"isrc": isrc}
    return SimpleNamespace(id=item_id, track=track, length=length, disc=disc, get=lambda k: data.get(k))


def test_item_isrcs_splits_and_normalises():
    assert item_isrcs(item(1, 1, 100, " us123 ; GB456;;")) == {"US123", "GB456"}
    assert item_isrcs(item(1, 1, 100, "")) == set()


def test_isrc_match_agrees_on_slot():
    hits = [TrackHit("t1", 1, 1, 200.0, "US1"), TrackHit("t2", 1, 2, 100.0, "US2")]
    out = match_tracks([item(1, 1, 200.0, "US1"), item(2, 2, 100.0, "us2")], hits)
    assert {k: v.id for k, v in out.items()} == {1: "t1", 2: "t2"}


def test_isrc_reused_on_compilation_is_rejected():
    # Same recording, but a different slot and a very different (edit) length.
    hits = [TrackHit("c9", 1, 9, 250.0, "US1")]
    assert match_tracks([item(1, 1, 180.0, "US1")], hits) == {}


def test_isrc_match_accepts_other_slot_when_duration_agrees():
    hits = [TrackHit("c9", 1, 9, 181.5, "US1")]
    assert match_tracks([item(1, 1, 180.0, "US1")], hits)[1].id == "c9"


def test_isrc_match_with_unknown_duration_is_accepted():
    hits = [TrackHit("c9", 1, 9, None, "US1")]
    assert match_tracks([item(1, 1, 180.0, "US1")], hits)[1].id == "c9"


def test_multi_isrc_string_matches_either_code():
    hits = [TrackHit("t1", 1, 1, 200.0, "GB456")]
    assert match_tracks([item(1, 1, 200.0, "US123;GB456")], hits)[1].id == "t1"


def test_isrc_prefers_same_slot_over_first_candidate():
    hits = [TrackHit("rem", 1, 7, 200.0, "US1"), TrackHit("orig", 1, 1, 200.0, "US1")]
    assert match_tracks([item(1, 1, 200.0, "US1")], hits)[1].id == "orig"


def test_fallback_without_isrc_uses_slot_and_duration():
    hits = [TrackHit("t1", 1, 1, 200.0), TrackHit("t2", 1, 2, 100.0)]
    out = match_tracks([item(1, 1, 201.0), item(2, 2, 100.0)], hits)
    assert {k: v.id for k, v in out.items()} == {1: "t1", 2: "t2"}


def test_fallback_rejects_wrong_duration_or_disc():
    hits = [TrackHit("t1", 1, 1, 200.0), TrackHit("t2", 2, 2, 100.0)]
    assert match_tracks([item(1, 1, 260.0), item(2, 2, 100.0, disc=1)], hits) == {}


def test_missing_disc_counts_as_disc_one():
    hits = [TrackHit("t1", 1, 3, 100.0)]
    assert match_tracks([item(1, 3, 100.0, disc=0)], hits)[1].id == "t1"


def test_one_service_track_never_matches_two_items():
    hits = [TrackHit("t1", 1, 1, 200.0, "US1")]
    out = match_tracks([item(1, 1, 200.0, "US1"), item(2, 1, 200.0, "US1")], hits)
    assert list(out) == [1]


def test_fallback_does_not_reuse_isrc_matched_hit():
    hits = [TrackHit("t1", 1, 1, 200.0, "US1")]
    out = match_tracks([item(1, 5, 200.0, "US1"), item(2, 1, 200.0)], hits)
    assert list(out) == [1]
