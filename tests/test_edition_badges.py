from copy import deepcopy
from datetime import datetime, timezone

import pytest

from qobuz_librarian.library.edition_badges import (
    build_edition_badges,
    edition_family_key,
    edition_label,
)


def _released(year):
    return datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()


def _album(
    album_id,
    title,
    original_year,
    *,
    published=2020,
    tracks=10,
    bits=16,
    sample_rate=44.1,
    artist="Artist",
):
    return {
        "id": album_id,
        "title": title,
        "artist": {"name": artist},
        "release_date_original": f"{original_year}-01-01",
        "released_at": _released(published),
        "tracks_count": tracks,
        "maximum_bit_depth": bits,
        "maximum_sampling_rate": sample_rate,
    }


def test_standard_and_deluxe_family_get_visible_qobuz_ids():
    badges = build_edition_badges(
        [
            _album("100", "Album", 2020),
            _album("200", "Album (Deluxe Edition)", 2020, tracks=14),
        ]
    )

    assert badges == {
        "100": "Standard Edition · Qobuz 100",
        "200": "Deluxe Edition · Qobuz 200",
    }


def test_single_release_and_different_original_years_or_artists_are_not_badged():
    assert build_edition_badges([_album("100", "Album", 2020)]) == {}
    assert (
        build_edition_badges(
            [
                _album("100", "Artist", 1990),
                _album("200", "Artist", 2020),
            ]
        )
        == {}
    )
    assert (
        build_edition_badges(
            [
                _album("300", "Album", 2020, artist="First Artist"),
                _album("400", "Album (Deluxe)", 2020, artist="Other Artist"),
            ]
        )
        == {}
    )


def test_duplicate_labels_use_ordered_minimum_differentiators():
    badges = build_edition_badges(
        [
            _album("100", "Album (Deluxe Edition)", 2020, published=2021),
            _album("200", "Album (Deluxe Edition)", 2020, published=2022),
        ]
    )
    assert badges["100"] == "Deluxe Edition · 2021 · Qobuz 100"
    assert badges["200"] == "Deluxe Edition · 2022 · Qobuz 200"

    track_badges = build_edition_badges(
        [
            _album("300", "Other (Remastered)", 2020, published=2022, tracks=10),
            _album("400", "Other (Remastered)", 2020, published=2022, tracks=12),
        ]
    )
    assert track_badges["300"] == "Remastered · 10 tracks · Qobuz 300"
    assert track_badges["400"] == "Remastered · 12 tracks · Qobuz 400"

    quality_badges = build_edition_badges(
        [
            _album(
                "500",
                "Third (Expanded Edition)",
                2020,
                published=2022,
                tracks=12,
                bits=16,
                sample_rate=44.1,
            ),
            _album(
                "600",
                "Third (Expanded Edition)",
                2020,
                published=2022,
                tracks=12,
                bits=24,
                sample_rate=96,
            ),
        ]
    )
    assert quality_badges["500"] == (
        "Expanded Edition · 16-bit/44.1kHz · Qobuz 500"
    )
    assert quality_badges["600"] == (
        "Expanded Edition · 24-bit/96kHz · Qobuz 600"
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Album: DELUXE   EDITION", "DELUXE EDITION"),
        ("Album - Expanded Edition", "Expanded Edition"),
        ("Album (special edition)", "special edition"),
        ("Album [Collector's Edition]", "Collector's Edition"),
        ("Album (25th Anniversary Edition)", "25th Anniversary Edition"),
        ("Album (2011 Remaster)", "2011 Remaster"),
        ("Album (Remastered)", "Remastered"),
        ("Album", "Standard Edition"),
    ],
)
def test_edition_label_recognizes_marker_variants_without_rewriting_human_casing(
    title, expected
):
    assert edition_label({"title": title}) == expected


def test_family_keys_reuse_stripping_and_normalization_helpers():
    assert edition_family_key(_album("100", "Café: Deluxe Edition", 2020)) == (
        "artist",
        "cafe",
        "2020",
    )
    assert edition_family_key(
        _album("200", "CAFE!!! (25th Anniversary Edition)", 2020, artist="ARTIST")
    ) == ("artist", "cafe", "2020")


@pytest.mark.parametrize(
    "album",
    [
        _album(None, "Album", 2020),
        _album(True, "Album", 2020),
        _album("", "Album", 2020),
        _album("bad/id", "Album", 2020),
        _album("bad\x00id", "Album", 2020),
    ],
)
def test_invalid_release_ids_are_ignored(album):
    assert build_edition_badges([album, _album("200", "Album (Deluxe)", 2020)]) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"release_date_original": None},
        {"release_date_original": "not-a-date"},
        {"release_date_original": "2020-99-99"},
        {"artist": None},
        {"artist": "Artist"},
        {"artist": {"name": None}},
        {"title": None},
        {"title": 123},
    ],
)
def test_missing_or_invalid_family_metadata_is_ignored(change):
    album = _album("100", "Album", 2020)
    album.update(change)

    assert edition_family_key(album) is None
    assert build_edition_badges([album, _album("200", "Album (Deluxe)", 2020)]) == {}


def test_normalized_duplicate_api_rows_do_not_invent_a_family():
    album = _album(" 100 ", "Album", 2020)

    assert build_edition_badges([album, deepcopy(album)]) == {}


def test_conflicting_duplicate_api_rows_are_discarded_independent_of_order():
    standard = _album("100", "Album", 2020)
    conflicting = _album(" 100 ", "Album (Deluxe Edition)", 2020)
    remaster = _album("200", "Album (2011 Remaster)", 2020)

    assert build_edition_badges([standard, conflicting, remaster]) == {}
    assert build_edition_badges([conflicting, remaster, standard]) == {}


def test_case_only_marker_differences_share_fallback_differentiation():
    badges = build_edition_badges(
        [
            _album("100", "Album (Deluxe Edition)", 2020, published=2021),
            _album("200", "Album (DELUXE EDITION)", 2020, published=2022),
        ]
    )

    assert badges == {
        "100": "Deluxe Edition · 2021 · Qobuz 100",
        "200": "DELUXE EDITION · 2022 · Qobuz 200",
    }


def test_partial_fallbacks_add_only_metadata_that_reduces_ambiguity():
    missing = _album(
        "300",
        "Album (Deluxe Edition)",
        2020,
        published=2021,
        tracks=12,
        bits=24,
        sample_rate=96,
    )
    missing["released_at"] = None
    missing["tracks_count"] = None
    missing["maximum_bit_depth"] = None
    missing["maximum_sampling_rate"] = None

    badges = build_edition_badges(
        [
            _album("200", "Album (Deluxe Edition)", 2020, published=2021, tracks=10),
            missing,
            _album("100", "Album (Deluxe Edition)", 2020, published=2021, tracks=12),
            _album("400", "Album (Deluxe Edition)", 2020, published=2022, tracks=12),
        ]
    )

    assert badges == {
        "100": "Deluxe Edition · 2021 · 12 tracks · Qobuz 100",
        "200": "Deluxe Edition · 2021 · 10 tracks · Qobuz 200",
        "300": "Deluxe Edition · Qobuz 300",
        "400": "Deluxe Edition · 2022 · Qobuz 400",
    }


def test_results_are_sorted_and_inputs_are_not_mutated():
    albums = [
        _album(20, "Album (Deluxe Edition)", 2020),
        _album(" 10 ", "Album", 2020),
    ]
    original = deepcopy(albums)

    badges = build_edition_badges(albums)

    assert list(badges) == ["10", "20"]
    assert badges["10"].endswith("Qobuz 10")
    assert badges["20"].endswith("Qobuz 20")
    assert albums == original
