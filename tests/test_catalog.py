from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qobuz_librarian import config
from qobuz_librarian.library import catalog
from qobuz_librarian.library.catalog import (
    _is_split_album_merge,
    album_year,
    compute_missing,
    dedup_album_versions,
    filter_owned_albums,
    find_album_dir_filesystem,
    find_extras_in_existing,
)


def test_automatic_multi_artist_migration_is_fail_closed(
        tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    source = music_root / "Artist, Other" / "Album"
    destination = music_root / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_track = source / "01 - Source.flac"
    destination_track = destination / "02 - Existing.flac"
    source_track.write_bytes(b"source")
    destination_track.write_bytes(b"destination")

    monkeypatch.setattr(config, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(
        catalog, "find_album_dir_filesystem", lambda _album: source)
    capture = {"stale": True}

    result = catalog.prompt_and_migrate_multi_artist_folder(
        {"artist": {"name": "Artist"}},
        SimpleNamespace(yes=True),
        ownership_move_out=capture,
    )

    assert result == source
    assert source_track.read_bytes() == b"source"
    assert destination_track.read_bytes() == b"destination"
    assert capture == {}


def test_multi_artist_migration_uses_the_exact_imported_source(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import post_import_relocation

    music_root = tmp_path / "music"
    source = music_root / "Artist, Other" / "Album"
    destination = music_root / "Artist" / "Album"
    source.mkdir(parents=True)
    monkeypatch.setattr(config, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(
        catalog,
        "find_album_dir_filesystem",
        lambda _album: pytest.fail("fuzzy lookup replaced an exact import path"),
    )
    seen = []

    def relocate(actual_source, actual_destination, *, kind, authority):
        seen.append((actual_source, actual_destination, kind, authority))
        return post_import_relocation.RelocationResult(
            destination=actual_destination,
            published_files=1,
            ownership_receipt={"version": 2},
            changed=True,
        )

    monkeypatch.setattr(
        post_import_relocation,
        "relocate_post_import_album",
        relocate,
    )
    authority = object()
    capture = {}

    result = catalog.prompt_and_migrate_multi_artist_folder(
        {"artist": {"name": "Artist"}},
        SimpleNamespace(yes=True),
        authority=authority,
        ownership_move_out=capture,
        source_dir=source,
    )

    assert result == destination
    assert capture == {"version": 2}
    assert seen == [(
        source,
        destination,
        post_import_relocation.RelocationKind.WHOLE_ALBUM,
        authority,
    )]


def test_signature_album_lookup_requires_one_unique_complete_match(
        tmp_path, monkeypatch):
    from qobuz_librarian.integrations import rip

    music_root = tmp_path / "music"
    artist = music_root / "Artist"
    partial = artist / "Partial"
    complete = artist / "Complete"
    duplicate = artist / "Duplicate"
    partial.mkdir(parents=True)
    signature_one = ("Artist", "Album", "One", "ISRC-1")
    signature_two = ("Artist", "Album", "Two", "ISRC-2")
    signatures = {}

    def add_track(folder, name, signature):
        track = folder / name
        track.write_bytes(b"audio")
        signatures[track] = signature

    add_track(partial, "01.flac", signature_one)
    monkeypatch.setattr(config, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(rip, "_flac_signature", signatures.get)

    wanted = [signature_one, signature_two]
    assert catalog.find_album_dir_by_track_signatures(wanted) is None

    complete.mkdir()
    add_track(complete, "01.flac", signature_one)
    add_track(complete, "02.flac", signature_two)
    assert catalog.find_album_dir_by_track_signatures(wanted) == complete

    duplicate.mkdir()
    add_track(duplicate, "01.flac", signature_one)
    add_track(duplicate, "02.flac", signature_two)
    assert catalog.find_album_dir_by_track_signatures(wanted) is None


def _qt(title, isrc="", disc=1, **kw):
    return {"title": title, "isrc": isrc, "media_number": disc, **kw}


def _et(title, isrc="", disc=1, **kw):
    from qobuz_librarian.library.tags import normalize
    return {"title": title, "isrc": isrc, "discnumber": disc,
            "normalized": normalize(title), **kw}


def _qalbum(title, year, bd=16, sr=44.1, tc=10):
    return {"title": title, "release_date_original": str(year),
            "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
            "tracks_count": tc}


def test_compute_missing_disc_and_edition_handling():
    qobuz = [_qt("Intro", disc=1), _qt("Theme", disc=1),
             _qt("Intro", disc=2), _qt("Theme", disc=2)]
    owned = [_et("Intro", disc=1), _et("Theme", disc=1),
             _et("Intro", disc=2), _et("Theme", disc=2)]
    assert not compute_missing(qobuz, owned)[0]
    m, _ = compute_missing(qobuz, owned[:2])
    assert sorted(t["media_number"] for t in m) == [2, 2]
    m, p = compute_missing([_qt("Song (2014 Remaster)")], [_et("Song")])
    assert len(p) == 1 and not m
    m, p = compute_missing([_qt("Song")], [_et("Song (Acoustic)")])
    assert len(m) == 1 and not p


def test_flat_untagged_multidisc_album_is_not_reported_missing():
    qobuz = [_qt("One", disc=1), _qt("Two", disc=1),
             _qt("Three", disc=2), _qt("Four", disc=2)]
    existing = [_et("One"), _et("Two"), _et("Three"), _et("Four")]
    missing, present = compute_missing(qobuz, existing)
    assert not missing and len(present) == 4


def test_non_latin_titles_match_on_text_not_empty_normalization():
    _, p = compute_missing([_qt("東京")], [_et("東京")])
    assert len(p) == 1
    m, p = compute_missing([_qt("東京")], [_et("大阪")])
    assert len(m) == 1 and not p
    extras = find_extras_in_existing([_qt("東京")], [_et("大阪")])
    assert [t["title"] for t in extras] == ["大阪"]


def test_find_extras_flags_bonus_tracks_for_upgrade_safety():
    extras = find_extras_in_existing(
        [_qt("Time")], [_et("Time"), _et("Time (Bonus Track)")])
    assert len(extras) == 1 and "Bonus" in extras[0]["title"]

    extras = find_extras_in_existing(
        [_qt("T1", isrc=" "), _qt("T2", isrc=" ")],
        [_et("Bonus", isrc=" "), _et("T1", isrc=" "), _et("T2", isrc=" ")])
    assert [t["title"] for t in extras] == ["Bonus"]



def test_album_year_handles_late_utc_release_correctly():
    assert album_year({"release_date_original": "2021-06-15"}) == "2021"
    ts = int(datetime(2019, 12, 31, 23, 0, 0, tzinfo=timezone.utc).timestamp())
    assert album_year({"released_at": ts}) == "2019"



def test_dedup_album_versions_collapses_editions_but_keeps_distinct_years():
    pairs = [_qalbum("Abbey Road", 1969), _qalbum("Abbey Road (Remaster)", 1969)]
    assert len(dedup_album_versions(pairs)) == 1
    pairs = [_qalbum("American Football", 1999), _qalbum("American Football", 2016)]
    assert len(dedup_album_versions(pairs)) == 2
    cjk = [_qalbum("東京", 2020), _qalbum("大阪", 2021)]
    assert len(dedup_album_versions(cjk)) == 2
    pair = [_qalbum("Album", 2020, bd=16, sr=44.1),
            _qalbum("Album", 2020, bd=24, sr=96)]
    result = dedup_album_versions(pair, prefer_hires=True)
    assert len(result) == 1 and result[0][0]["maximum_bit_depth"] == 24


def test_filter_owned_albums_doesnt_swallow_sequels_or_distinct_years():
    pairs = [({"title": "Reload", "release_date_original": "1997"}, 1),
             ({"title": "Album (Deluxe Edition)", "release_date_original": "2010"}, 1)]
    result = filter_owned_albums(pairs, {"load": [1996], "album": [2010]})
    assert [a["title"] for a, _ in result] == ["Reload"]

    pairs = [({"title": "Revolver", "release_date_original": "2022"}, 1)]
    assert filter_owned_albums(pairs, {"revolver": [1966]}) == []

    pairs = [({"title": "Revolver", "release_date_original": "1966"}, 1)]
    assert filter_owned_albums(pairs, {"revolver": [None]}) == []

    pairs = [({"title": "Wasting Light", "release_date_original": "2011"}, 1)]
    assert [a["title"] for a, _ in
            filter_owned_albums(pairs, {"wastinglightlive": [2019]})] == ["Wasting Light"]



def test_split_album_merge_rules(tmp_path):
    art = tmp_path / "Bonobo"
    (art / "Black Sands").mkdir(parents=True)
    (art / "Black Sands (2010)").mkdir()
    assert _is_split_album_merge(art / "Black Sands", art / "Black Sands (2010)", "Bonobo") is False

    collaborators = tmp_path / "Bonobo, Andreya Triana"
    (collaborators / "Black Sands (2010)").mkdir(parents=True)
    assert _is_split_album_merge(
        collaborators / "Black Sands (2010)",
        art / "Black Sands (2010)",
        "Bonobo",
    ) is True

    (art / "Black Sands (Live)").mkdir()
    assert _is_split_album_merge(art / "Black Sands (Live)", art / "Black Sands (2010)", "Bonobo") is False

    (art / "Live (2010)").mkdir()
    (art / "Live (2011)").mkdir()
    assert _is_split_album_merge(art / "Live (2010)", art / "Live (2011)", "Bonobo") is False



def test_find_album_dir_does_not_match_a_live_release_to_the_studio_folder(tmp_path, monkeypatch):
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    missing = {
        "id": "M",
        "artist": {"name": "Absent Artist"},
        "title": "Absent Album",
    }
    assert find_album_dir_filesystem(missing) is None
    (tmp_path / "Bonobo" / "The North Borders (2013)").mkdir(parents=True)
    clear_scan_caches()
    live = {"id": "L", "artist": {"name": "Bonobo"},
            "title": "The North Borders Tour. — Live.",
            "release_date_original": "2014-01-01"}
    assert find_album_dir_filesystem(live) is None
    clear_scan_caches()
    studio = {"id": "S", "artist": {"name": "Bonobo"},
              "title": "The North Borders", "release_date_original": "2013-01-01"}
    assert find_album_dir_filesystem(studio).name == "The North Borders (2013)"
    clear_scan_caches()


def test_find_album_dir_falls_through_to_lower_scored_folder_when_top_fails_coverage(
        tmp_path, monkeypatch):
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    (tmp_path / "Band" / "Wide Awakening Sessions Bonus").mkdir(parents=True)
    (tmp_path / "Band" / "Wide Awaknng").mkdir(parents=True)
    clear_scan_caches()
    monkeypatch.setattr(catalog, "similarity",
                        lambda a, b: 0.95 if "Sessions" in a else 0.80)
    album = {"id": "X", "artist": {"name": "Band"}, "title": "Wide Awakening"}
    found = find_album_dir_filesystem(album)
    clear_scan_caches()
    assert found is not None and found.name == "Wide Awaknng"



def _exp_album(album_id, bd, sr, tracks):
    return {"id": album_id, "artist": {"name": "Test Artist"},
            "title": "Test Album", "maximum_bit_depth": bd,
            "maximum_sampling_rate": sr, "tracks_count": len(tracks),
            "tracks": {"items": tracks}}


def test_find_expanded_edition_prefers_quality_when_extras_tied(tmp_path):
    from qobuz_librarian.library.catalog import find_expanded_edition
    qt = lambda isrc, title: {"isrc": isrc, "title": title, "media_number": 1}
    et = lambda isrc, title: {"isrc": isrc, "title": title, "discnumber": 1}
    existing = [et("ISRC001", "Track 1"), et("ISRC002", "Track 2")]
    orig = _exp_album("orig", 16, 44.1, [qt("ISRC001", "Track 1"), qt("ISRC002", "Track 2")])
    hires = _exp_album("hires", 24, 96.0, orig["tracks"]["items"])
    redbook = _exp_album("redbook", 16, 44.1, orig["tracks"]["items"])

    with patch("qobuz_librarian.library.catalog.search_albums",
               return_value=[hires, redbook]), \
         patch("qobuz_librarian.library.catalog.get_album",
               side_effect=lambda aid, tok: hires if aid == "hires" else redbook):
        results = find_expanded_edition(orig, tmp_path, existing, "tok", SimpleNamespace())
    assert [r[0]["id"] for r in results] == ["hires", "redbook"]


def test_folder_holds_all_tracks_matches_identity_not_count(tmp_path):
    from qobuz_librarian.library.catalog import folder_holds_all_tracks

    expected = [{"id": 1, "title": "Alpha"}, {"id": 2, "title": "Beta"}]
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "01 - Alpha.flac").write_bytes(b"x")
    (folder / "09 - Bonus.flac").write_bytes(b"x")
    assert folder_holds_all_tracks(folder, expected) is False

    (folder / "02 - Beta.flac").write_bytes(b"x")
    assert folder_holds_all_tracks(folder, expected) is True

    assert folder_holds_all_tracks(folder, []) is False
    assert folder_holds_all_tracks(tmp_path / "gone", expected) is False


def test_title_fallback_cannot_hand_an_isrc_twin_to_another_track():
    qobuz = [
        {"title": "Song", "media_number": 1, "isrc": "USAAA0000001"},
        {"title": "Song", "media_number": 1, "isrc": "USBBB0000002"},
    ]
    on_disk = [
        {"title": "Song", "discnumber": 1, "isrc": "USAAA0000001"},
        {"title": "Song", "discnumber": 1, "isrc": "USAAA0000001"},
    ]
    missing, present = compute_missing(qobuz, on_disk)
    assert [t["isrc"] for t in missing] == ["USBBB0000002"]
    assert len(present) == 1

    other_edition = [{"title": "Song", "discnumber": 1, "isrc": "GBZZZ9999999"}]
    missing, _ = compute_missing([qobuz[0]], other_edition)
    assert not missing


def test_folder_completeness_requires_a_full_tree_walk(monkeypatch, tmp_path):
    from qobuz_librarian.library import catalog as cat

    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "01 - Song.flac").write_bytes(b"x")
    qobuz = [{"title": "Song", "media_number": 1, "isrc": ""}]
    tracks = [{"title": "Song", "discnumber": 1, "isrc": ""}]

    def degraded(f, walk_errors=None):
        if walk_errors is not None:
            walk_errors.append(f"{f}/Disc 2: EIO")
        return list(tracks)

    monkeypatch.setattr(cat, "read_album_dir", degraded)
    assert cat.folder_holds_all_tracks(folder, qobuz) is False

    monkeypatch.setattr(cat, "read_album_dir",
                        lambda f, walk_errors=None: list(tracks))
    assert cat.folder_holds_all_tracks(folder, qobuz) is True


def test_disposal_completeness_requires_strict_identity_and_slots(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import catalog as cat

    folder = tmp_path / "Album"
    folder.mkdir()
    expected = [
        {"title": "Same", "media_number": 1, "track_number": 1,
         "duration": 100, "isrc": "USAAA1234567"},
        {"title": "Same", "media_number": 1, "track_number": 2,
         "duration": 100, "isrc": "USBBB1234568"},
    ]
    duplicate_blanks = [
        {"title": "Same", "discnumber": 1, "tracknumber": 1,
         "length": 100.0, "isrc": ""},
        {"title": "Same", "discnumber": 1, "tracknumber": 1,
         "length": 100.0, "isrc": ""},
    ]
    monkeypatch.setattr(
        cat, "read_album_dir",
        lambda _folder, walk_errors=None: list(duplicate_blanks[:1]),
    )
    assert cat.folder_holds_all_tracks(folder, expected[:1]) is True
    assert cat.folder_holds_all_tracks(
        folder, expected[:1], destructive=True) is False

    placeholder_expected = [{**expected[0], "isrc": "N/A"}]
    placeholder_existing = [{**duplicate_blanks[0], "isrc": "N/A"}]
    monkeypatch.setattr(
        cat, "read_album_dir",
        lambda _folder, walk_errors=None: list(placeholder_existing),
    )
    assert cat.folder_holds_all_tracks(
        folder, placeholder_expected, destructive=True) is False

    for field, placeholder in (
        ("isrc", "000000000000"),
        ("mb_trackid", "00000000-0000-0000-0000-000000000000"),
    ):
        malformed_expected = [{
            **expected[0], "isrc": "", field: placeholder}]
        malformed_existing = [{
            **duplicate_blanks[0], "title": "Different", field: placeholder}]
        monkeypatch.setattr(
            cat, "read_album_dir",
            lambda _folder, walk_errors=None,
            tracks=malformed_existing: list(tracks),
        )
        assert cat.folder_holds_all_tracks(
            folder, malformed_expected, destructive=True) is False

    unidentified_expected = [{**track, "isrc": ""} for track in expected]
    edition_variant = [{
        **duplicate_blanks[0], "title": "Same (Remaster)"}]
    monkeypatch.setattr(
        cat, "read_album_dir",
        lambda _folder, walk_errors=None: list(edition_variant),
    )
    assert cat.folder_holds_all_tracks(
        folder, unidentified_expected[:1]) is True
    assert cat.folder_holds_all_tracks(
        folder, unidentified_expected[:1], destructive=True) is False

    monkeypatch.setattr(
        cat, "read_album_dir",
        lambda _folder, walk_errors=None: list(duplicate_blanks),
    )
    assert cat.folder_holds_all_tracks(folder, unidentified_expected) is True
    assert cat.folder_holds_all_tracks(
        folder, unidentified_expected, destructive=True) is False

    verified = [
        {**track, "discnumber": 1, "tracknumber": index,
         "length": 100.0}
        for index, track in enumerate(expected, 1)
    ]
    monkeypatch.setattr(
        cat, "read_album_dir",
        lambda _folder, walk_errors=None: list(verified),
    )
    assert cat.folder_holds_all_tracks(
        folder, expected, destructive=True) is True

    verified_unidentified = [{**track, "isrc": ""} for track in verified]
    monkeypatch.setattr(
        cat, "read_album_dir",
        lambda _folder, walk_errors=None: list(verified_unidentified),
    )
    assert cat.folder_holds_all_tracks(
        folder, unidentified_expected, destructive=True) is True

    malformed_values = (
        ("media_number", True),
        ("track_number", 1.9),
        ("duration", True),
    )
    for field, value in malformed_values:
        malformed_expected = [{
            **unidentified_expected[0], field: value}]
        monkeypatch.setattr(
            cat, "read_album_dir",
            lambda _folder, walk_errors=None: list(verified_unidentified[:1]),
        )
        assert cat.folder_holds_all_tracks(
            folder, malformed_expected, destructive=True) is False
