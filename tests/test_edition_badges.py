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


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (
            "released_at",
            {
                "100": "Deluxe Edition · Qobuz 100",
                "200": "Deluxe Edition · 2020 · Qobuz 200",
            },
        ),
        (
            "tracks_count",
            {
                "100": "Deluxe Edition · Qobuz 100",
                "200": "Deluxe Edition · 10 tracks · Qobuz 200",
            },
        ),
        (
            "maximum_bit_depth",
            {
                "100": "Deluxe Edition · Qobuz 100",
                "200": "Deluxe Edition · 16-bit/44.1kHz · Qobuz 200",
            },
        ),
        (
            "maximum_sampling_rate",
            {
                "100": "Deluxe Edition · Qobuz 100",
                "200": "Deluxe Edition · 16-bit/44.1kHz · Qobuz 200",
            },
        ),
    ],
)
def test_huge_fallback_numbers_are_omitted_without_crashing(field, expected):
    malformed = _album("100", "Album (Deluxe Edition)", 2020)
    malformed[field] = 10**10000

    assert build_edition_badges(
        [malformed, _album("200", "Album (Deluxe Edition)", 2020)]
    ) == expected


class _UnstableReleaseId:
    def __str__(self):
        raise AssertionError("an arbitrary release ID object must not be rendered")


@pytest.mark.parametrize(
    "album_id",
    [
        1.0,
        float("nan"),
        float("inf"),
        [],
        {},
        _UnstableReleaseId(),
        10**10000,
        "9" * 10000,
    ],
    ids=[
        "float",
        "nan",
        "infinity",
        "list",
        "dict",
        "arbitrary-object",
        "huge-integer",
        "huge-string",
    ],
)
def test_non_qobuz_release_id_scalars_are_ignored_without_rendering(album_id):
    assert (
        build_edition_badges(
            [
                _album(album_id, "Album", 2020),
                _album("200", "Album (Deluxe Edition)", 2020),
            ]
        )
        == {}
    )


def test_integer_and_trimmed_string_qobuz_ids_are_normalized_deterministically():
    assert build_edition_badges(
        [
            _album(100, "Album", 2020),
            _album(" 00200 ", "Album (Deluxe Edition)", 2020),
        ]
    ) == {
        "00200": "Deluxe Edition · Qobuz 00200",
        "100": "Standard Edition · Qobuz 100",
    }


@pytest.mark.parametrize(
    "variant",
    [
        "Album (Part II)",
        "Album (Original Motion Picture Soundtrack)",
        "Album (The Soundtrack)",
    ],
)
def test_genuine_parenthetical_title_variants_do_not_join_the_standard_family(variant):
    assert edition_family_key(_album("200", variant, 2020))[1] != "album"
    assert build_edition_badges(
        [_album("100", "Album", 2020), _album("200", variant, 2020)]
    ) == {}


def test_only_the_recognized_trailing_edition_decoration_is_removed():
    assert edition_family_key(
        _album("100", "Album (Part II) (Deluxe Edition)", 2020)
    ) == ("artist", "albumpartii", "2020")
    assert edition_family_key(
        _album("200", "Album (Part II)", 2020)
    ) == ("artist", "albumpartii", "2020")
    assert edition_family_key(
        _album("300", "X-Y - Deluxe Edition", 2020)
    ) == ("artist", "xy", "2020")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("The Anniversary", "Standard Edition"),
        ("The Anniversary (Part II)", "Standard Edition"),
        ("The Anniversary (Deluxe Edition)", "Deluxe Edition"),
        ("The Anniversary - 2011 Remaster", "2011 Remaster"),
        ("The Anniversary-2011 Remaster", "2011 Remaster"),
        ("The Anniversary: Deluxe Edition", "Deluxe Edition"),
        ("Album (Collector’s Edition)", "Collector’s Edition"),
    ],
)
def test_edition_labels_come_from_the_trailing_edition_decoration(title, expected):
    assert edition_label({"title": title}) == expected


@pytest.mark.parametrize(
    ("title", "normalized_title"),
    [
        ("Album (The Anniversary Soundtrack)", "albumtheanniversarysoundtrack"),
        ("Album (Deluxe Part II)", "albumdeluxepartii"),
        ("Album: The Special Relationship", "albumthespecialrelationship"),
    ],
)
def test_partial_marker_phrases_remain_genuine_title_components(
    title, normalized_title
):
    assert edition_label({"title": title}) == "Standard Edition"
    assert edition_family_key(_album("100", title, 2020)) == (
        "artist",
        normalized_title,
        "2020",
    )
    assert build_edition_badges(
        [_album("100", "Album", 2020), _album("200", title, 2020)]
    ) == {}


def test_exact_edition_suffix_after_genuine_component_is_the_only_part_removed():
    title = "Album (The Anniversary Soundtrack) (Deluxe Edition)"

    assert edition_label({"title": title}) == "Deluxe Edition"
    assert edition_family_key(_album("100", title, 2020)) == (
        "artist",
        "albumtheanniversarysoundtrack",
        "2020",
    )


def test_repeated_exact_edition_suffixes_are_removed_and_last_label_is_visible():
    title = "Album (Deluxe Edition) (2011 Remaster)"

    assert edition_label({"title": title}) == "2011 Remaster"
    assert edition_family_key(_album("100", title, 2020)) == (
        "artist",
        "album",
        "2020",
    )


def test_publication_year_bounds_include_history_and_small_future_allowance():
    current_year = datetime.now(timezone.utc).year

    historical = _album(
        "100", "Album (Deluxe Edition)", 2020, published=1800
    )
    too_old = _album(
        "200", "Album (Deluxe Edition)", 2020, published=1799
    )
    assert build_edition_badges([historical, too_old]) == {
        "100": "Deluxe Edition · 1800 · Qobuz 100",
        "200": "Deluxe Edition · Qobuz 200",
    }

    announced = _album(
        "300", "Other (Deluxe Edition)", 2020, published=current_year + 2
    )
    too_future = _album(
        "400", "Other (Deluxe Edition)", 2020, published=current_year + 3
    )
    assert build_edition_badges([announced, too_future]) == {
        "300": f"Deluxe Edition · {current_year + 2} · Qobuz 300",
        "400": "Deluxe Edition · Qobuz 400",
    }

    year_9999 = _album("500", "Third (Deluxe Edition)", 2020)
    year_9999["released_at"] = _released(9999)
    assert build_edition_badges(
        [year_9999, _album("600", "Third (Deluxe Edition)", 2020, published=2020)]
    ) == {
        "500": "Deluxe Edition · Qobuz 500",
        "600": "Deluxe Edition · 2020 · Qobuz 600",
    }


@pytest.mark.parametrize("invalid_count", [10_001, 100_000])
def test_track_count_bounds_preserve_large_box_sets_and_omit_implausible_values(
    invalid_count,
):
    assert build_edition_badges(
        [
            _album("100", "Album (Deluxe Edition)", 2020, tracks=10_000),
            _album("200", "Album (Deluxe Edition)", 2020, tracks=invalid_count),
        ]
    ) == {
        "100": "Deluxe Edition · 10000 tracks · Qobuz 100",
        "200": "Deluxe Edition · Qobuz 200",
    }


@pytest.mark.parametrize(
    ("valid_bits", "valid_rate", "invalid_bits", "invalid_rate", "valid_label"),
    [
        (16, 16, 15, 16, "16-bit/16kHz"),
        (32, 768, 33, 768, "32-bit/768kHz"),
        (32, 768_000, 32, 769_000, "32-bit/768kHz"),
        (24, 96, 1024, 96, "24-bit/96kHz"),
        (24, 96, 24, 10_000, "24-bit/96kHz"),
    ],
)
def test_quality_bounds_preserve_supported_hires_and_omit_implausible_metadata(
    valid_bits, valid_rate, invalid_bits, invalid_rate, valid_label
):
    assert build_edition_badges(
        [
            _album(
                "100",
                "Album (Deluxe Edition)",
                2020,
                bits=valid_bits,
                sample_rate=valid_rate,
            ),
            _album(
                "200",
                "Album (Deluxe Edition)",
                2020,
                bits=invalid_bits,
                sample_rate=invalid_rate,
            ),
        ]
    ) == {
        "100": f"Deluxe Edition · {valid_label} · Qobuz 100",
        "200": "Deluxe Edition · Qobuz 200",
    }


def test_nonbreaking_hyphen_delimits_editions_without_splitting_the_base_title():
    title = "X‑Y ‑ Deluxe Edition"

    assert edition_label({"title": "Album‑2011 Remaster"}) == "2011 Remaster"
    assert edition_label({"title": "X‑Y"}) == "Standard Edition"
    assert edition_family_key(_album("100", title, 2020)) == (
        "artist",
        "xy",
        "2020",
    )
