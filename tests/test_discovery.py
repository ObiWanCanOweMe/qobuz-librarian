"""The shared discovery engine — one answer to "what's missing for this artist"
that both the CLI artist mode and the web scans drive.

These exercise the engine against a real temp library on disk (so folder
resolution, edition matching and track comparison all run for real) with only
the Qobuz API stubbed. They pin the reconciled behaviour the two interfaces
must now agree on; the per-interface tests elsewhere prove each face presents
this same result.
"""
from datetime import date

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.library import catalog as cat
from qobuz_librarian.library import discovery
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
)
from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseIdentity,
    publish_release_identity,
    read_release_identity,
)
from qobuz_librarian.library.scanner import clear_scan_caches

# ── Fixture library + fake Qobuz ────────────────────────────────────────────────

def _qt(title, isrc="", disc=1):
    return {"title": title, "isrc": isrc, "media_number": disc, "track_number": 0}


def _et(title, isrc="", disc=1):
    return {"title": title, "isrc": isrc, "discnumber": disc}


def _album(album_id, title, artist, year, tracks, bd=16, sr=44.1):
    """A full Qobuz album dict (get_album shape), with a track list."""
    return {
        "id": album_id, "title": title,
        "artist": {"name": artist, "id": f"art-{artist}"},
        "release_date_original": f"{year}-01-01",
        "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
        "tracks_count": len(tracks),
        "tracks": {"items": tracks},
    }


def _catalog_entry(album):
    """The lighter shape get_artist_albums returns — no track list."""
    return {k: v for k, v in album.items() if k != "tracks"}


class FakeQobuz:
    """Stands in for the Qobuz search/catalog API. Holds artist search hits,
    one catalog per artist id, and full album dicts keyed by id."""

    def __init__(self, *, artists, catalog, total=None):
        self._artists = artists                       # search_artists results
        self._full = {a["id"]: a for a in catalog}    # get_album by id
        self._catalog = [_catalog_entry(a) for a in catalog]
        self._total = total if total is not None else len(catalog)

    def search_artists(self, query, token, limit=5):
        return self._artists

    def get_artist_albums(self, artist_id, token, limit=500, fresh=False):
        return list(self._catalog), self._total

    def get_album(self, album_id, token):
        return self._full[album_id]

    def search_albums(self, query, token, limit=12):
        # Per-folder search fallback: the fixtures keep everything in the
        # catalog, so the fallback returns nothing.
        return []

    def install(self, monkeypatch):
        monkeypatch.setattr(discovery, "search_artists", self.search_artists)
        monkeypatch.setattr(discovery, "get_artist_albums", self.get_artist_albums)
        monkeypatch.setattr(discovery, "get_album", self.get_album)
        monkeypatch.setattr(cat, "get_album", self.get_album)
        monkeypatch.setattr(cat, "search_albums", self.search_albums)


def _library(monkeypatch, tmp_path, layout):
    """layout: {artist_folder: {album_folder: [existing_track_dict, ...]}}.

    Creates the dirs (one empty .flac per track so the audio-file count is
    honest) and stubs read_album_dir to return the assigned track dicts.
    """
    track_map = {}
    for artist, albums in layout.items():
        for album, tracks in albums.items():
            d = tmp_path / artist / album
            d.mkdir(parents=True)
            for i in range(len(tracks)):
                (d / f"{i + 1:02d}.flac").write_bytes(b"")
            track_map[str(d)] = tracks
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cat, "read_album_dir", lambda d: track_map.get(str(d), []))
    clear_scan_caches()


@pytest.fixture
def beatles_search():
    return [{"name": "The Beatles", "id": "beatles", "albums_count": 50}]


def _run(monkeypatch, tmp_path, *, layout, catalog, artists,
         query="The Beatles", artist_folder="The Beatles", total=None,
         hidden=None, want_missing=True, prefer_hires=True):
    _library(monkeypatch, tmp_path, layout)
    FakeQobuz(artists=artists, catalog=catalog, total=total).install(monkeypatch)
    artist_dir = (tmp_path / artist_folder) if artist_folder else None
    if artist_dir is not None and not artist_dir.exists():
        artist_dir = None
    result = find_missing_for_artist(
        query, token="tok",
        opts=DiscoveryOpts(prefer_hires=prefer_hires),
        artist_dir=artist_dir, hidden=hidden, want_missing=want_missing)
    clear_scan_caches()
    return result


def _titles(gaps):
    return sorted(g.qobuz_album.get("title") for g in gaps)


# ── Core classification ─────────────────────────────────────────────────────────

def test_fully_missing_album_is_a_gap_with_no_dir(monkeypatch, tmp_path, beatles_search):
    owned = _album("a1", "Revolver", "The Beatles", 1966,
                   [_qt(f"r{i}", f"ISRCR{i}") for i in range(11)])
    missing = _album("a2", "Abbey Road", "The Beatles", 1969,
                     [_qt(f"a{i}", f"ISRCA{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Revolver (1966)":
                                       [_et(f"r{i}", f"ISRCR{i}") for i in range(11)]}},
               catalog=[owned, missing], artists=beatles_search)

    fully = [g for g in res.gaps if g.on_disk_dir is None]
    assert _titles(fully) == ["Abbey Road"]
    # The owned, complete album is not a gap and not unmatched.
    assert "Revolver" not in _titles(res.gaps)
    assert [c["dir"].name for c in res.complete] == ["Revolver (1966)"]
    assert not res.unmatched_dirs


def test_partial_owned_album_reports_the_real_track_gap(monkeypatch, tmp_path, beatles_search):
    full = _album("a1", "Abbey Road", "The Beatles", 1969,
                  [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Abbey Road (1969)":
                                       [_et(f"t{i}", f"ISRC{i}") for i in range(6)]}},
               catalog=[full], artists=beatles_search)

    partials = [g for g in res.gaps if g.on_disk_dir is not None]
    assert len(partials) == 1
    gap = partials[0]
    assert gap.qobuz_album["title"] == "Abbey Road"
    assert gap.on_disk_dir.name == "Abbey Road (1969)"
    assert len(gap.present) == 6
    assert len(gap.missing) == 4


def test_missing_and_gap_fill_receive_badges_from_catalog_family(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MISSING_ALBUMS_MIN_TRACKS", 1)
    standard = _album("100", "Album", "Artist", 2020,
                      [_qt("one", "A"), _qt("two", "B")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("remixed", "Z"), _qt("bonus", "C")])
    missing_standard = _album("300", "Other", "Artist", 2021,
                              [_qt("x", "X")])
    missing_deluxe = _album("400", "Other (Deluxe Edition)", "Artist", 2021,
                            [_qt("x", "X"), _qt("y", "Y")])
    result = _run(
        monkeypatch,
        tmp_path,
        query="Artist",
        artist_folder="Artist",
        layout={"Artist": {"Album (2020)": [_et("one", "A")]}},
        catalog=[standard, deluxe, missing_standard, missing_deluxe],
        artists=[{"name": "Artist", "id": "artist", "albums_count": 4}],
    )

    gap_fill = next(g for g in result.gaps if g.on_disk_dir is not None)
    missing = next(g for g in result.gaps if g.qobuz_album["id"] == "300")
    assert gap_fill.edition_badge == "Standard Edition · Qobuz 100"
    assert missing.edition_badge == "Standard Edition · Qobuz 300"


def test_unambiguous_catalog_album_has_no_badge(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MISSING_ALBUMS_MIN_TRACKS", 1)
    only = _album("100", "Only Album", "Artist", 2020, [_qt("one", "A")])
    result = _run(
        monkeypatch, tmp_path, query="Artist", artist_folder="Artist",
        layout={"Artist": {}}, catalog=[only],
        artists=[{"name": "Artist", "id": "artist", "albums_count": 1}],
    )
    assert result.gaps[0].edition_badge == ""


def test_incomplete_catalog_never_attaches_edition_badges(monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020,
                      [_qt("one", "A"), _qt("two", "B")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("two", "B"), _qt("bonus", "C")])
    result = _run(
        monkeypatch, tmp_path, query="Artist", artist_folder="Artist",
        layout={"Artist": {}}, catalog=[standard, deluxe], total=3,
        artists=[{"name": "Artist", "id": "artist", "albums_count": 3}],
    )

    assert result.catalog_incomplete is True
    assert all(gap.edition_badge == "" for gap in result.gaps)


def test_capped_catalog_never_attaches_edition_badges(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "ARTIST_CATALOG_LIMIT", 2)
    monkeypatch.setattr(cfg, "MISSING_ALBUMS_MIN_TRACKS", 1)
    standard = _album("100", "Album", "Artist", 2020, [_qt("one", "A")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("bonus", "B")])
    result = _run(
        monkeypatch, tmp_path, query="Artist", artist_folder="Artist",
        layout={"Artist": {}}, catalog=[standard, deluxe], total=3,
        artists=[{"name": "Artist", "id": "artist", "albums_count": 3}],
    )

    assert result.catalog_incomplete is False
    assert all(gap.edition_badge == "" for gap in result.gaps)


def test_manifest_selects_exact_release_even_when_title_rank_prefers_another(
        monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020, [_qt("one", "A")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("bonus", "B")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A"), _et("bonus", "B")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    publish_release_identity(folder, ReleaseIdentity("qobuz", "200"))
    FakeQobuz(artists=[], catalog=[standard, deluxe]).install(monkeypatch)

    match = discovery.match_album_dir(
        folder, "Artist", "tok",
        catalog=[_catalog_entry(standard), _catalog_entry(deluxe)],
        prefer_hires=False,
    )

    assert match.qobuz_album["id"] == "200"
    assert match.status == "complete"


def test_unique_partial_legacy_match_publishes_manifest(monkeypatch, tmp_path):
    wanted = _album("100", "Album", "Artist", 2020,
                    [_qt("one", "A"), _qt("two", "B")])
    wrong = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                   [_qt("other", "Z")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    FakeQobuz(artists=[], catalog=[wanted, wrong]).install(monkeypatch)

    match = discovery.match_album_dir(
        folder, "Artist", "tok",
        catalog=[_catalog_entry(wanted), _catalog_entry(wrong)],
        prefer_hires=False,
    )

    assert match.status == "partial"
    assert read_release_identity(folder) == ReleaseIdentity("qobuz", "100")


def test_shared_standard_tracks_leave_legacy_folder_ambiguous(monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020,
                      [_qt("one", "A"), _qt("two", "B")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("two", "B"), _qt("bonus", "C")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    FakeQobuz(artists=[], catalog=[standard, deluxe]).install(monkeypatch)

    match = discovery.match_album_dir(
        folder, "Artist", "tok",
        catalog=[_catalog_entry(standard), _catalog_entry(deluxe)],
        prefer_hires=False,
    )

    assert match.status == "identity_ambiguous"
    assert [str(album["id"]) for album in match.candidate_releases] == ["100", "200"]
    assert not (folder / MANIFEST_NAME).exists()


def test_invalid_manifest_keeps_candidate_releases_for_review(monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020,
                      [_qt("one", "A"), _qt("two", "B")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("two", "B"), _qt("bonus", "C")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    manifest = folder / MANIFEST_NAME
    malformed = '{"schema_version":1,"provider":"qobuz","release_id":"100","extra":true}'
    manifest.write_text(malformed, encoding="utf-8")
    FakeQobuz(
        artists=[{"name": "Artist", "id": "artist", "albums_count": 2}],
        catalog=[standard, deluxe],
    ).install(monkeypatch)
    real_existing_tracks = discovery.find_existing_tracks

    def reviewed_tracks_only(album, *, album_dir=None):
        if album_dir is None:
            return [], None
        return real_existing_tracks(album, album_dir=album_dir)

    # The catalog pass must rely on the IDs classified from the invalid folder,
    # rather than resolving its path or hiding it by its owned title.
    monkeypatch.setattr(discovery, "find_existing_tracks", reviewed_tracks_only)
    monkeypatch.setattr(discovery, "_owned_by_name", lambda *_args: False)

    result = find_missing_for_artist(
        "Artist", token="tok", opts=DiscoveryOpts(prefer_hires=False),
        artist_dir=tmp_path / "Artist")

    assert result.gaps == []
    assert result.skipped == [{
        "dir": folder,
        "reason": "identity_invalid",
        "candidate_releases": [
            {"id": "100", "title": "Album"},
            {"id": "200", "title": "Album (Deluxe Edition)"},
        ],
    }]
    assert manifest.read_text(encoding="utf-8") == malformed


def test_deluxe_edition_gap_measured_against_the_owned_edition(monkeypatch, tmp_path, beatles_search):
    # The folder is an anniversary edition; the gap must be computed against the
    # edition that folder actually is (14 tracks), not the standard release.
    deluxe = _album("dlx", "Severed Survival", "Autopsy", 1989,
                    [_qt(f"s{i}", f"ISRCS{i}") for i in range(14)])
    res = _run(monkeypatch, tmp_path,
               query="Autopsy", artist_folder="Autopsy",
               layout={"Autopsy": {"Severed Survival (20th Anniversary Edition) (2016)":
                                   [_et(f"s{i}", f"ISRCS{i}") for i in range(8)]}},
               catalog=[deluxe],
               artists=[{"name": "Autopsy", "id": "autopsy", "albums_count": 9}])

    partials = [g for g in res.gaps if g.on_disk_dir is not None]
    assert len(partials) == 1
    assert len(partials[0].missing) == 6
    assert len(partials[0].present) == 8


def test_zero_overlap_match_is_skipped_as_false_match(monkeypatch, tmp_path, beatles_search):
    # A folder that fuzz-resolves to a catalog album it shares no tracks with is
    # a false match, not a whole-album gap to download over it.
    album = _album("a1", "Help", "The Beatles", 1965,
                   [_qt(f"h{i}", f"ISRCH{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Help (1965)":
                                       [_et(f"x{i}", f"OTHER{i}") for i in range(10)]}},
               catalog=[album], artists=beatles_search)

    assert not res.gaps
    assert any(s["reason"] == "false_match" for s in res.skipped)


def test_transient_api_error_aborts_the_scan_instead_of_burying_a_folder(
        monkeypatch, tmp_path, beatles_search):
    # A transient Qobuz failure while matching an owned folder must propagate,
    # not collapse into a "no match / nothing missing" verdict — otherwise an
    # outage silently buries albums the next scan would never re-check.
    from qobuz_librarian.api.auth import QobuzUnavailable

    full = _album("a1", "Abbey Road", "The Beatles", 1969,
                  [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])

    class Flaky(FakeQobuz):
        def get_album(self, album_id, token):
            raise QobuzUnavailable("the Qobuz API timed out — try again later")

    _library(monkeypatch, tmp_path,
             {"The Beatles": {"Abbey Road (1969)":
                              [_et(f"t{i}", f"ISRC{i}") for i in range(6)]}})
    Flaky(artists=beatles_search, catalog=[full]).install(monkeypatch)

    with pytest.raises(QobuzUnavailable):
        find_missing_for_artist(
            "The Beatles", token="tok", opts=DiscoveryOpts(prefer_hires=True),
            artist_dir=tmp_path / "The Beatles")


# ── New-release quickscan ─────────────────────────────────────────────────────

def test_resolve_artist_does_not_cache_an_id_less_match(monkeypatch):
    # A partial/malformed Qobuz 200 — a name match carrying no id — must not
    # be cached.
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    monkeypatch.setattr(discovery, "_resolve_cache_dirty", False)

    calls = []

    def fake_search(query, token, limit=None):
        calls.append(query)
        return [{"name": "Phantom Singer", "albums_count": 7}]  # no "id"
    monkeypatch.setattr(discovery, "search_artists", fake_search)

    assert discovery.resolve_artist("Phantom Singer", "tok") == (None, "Phantom Singer")
    # A later scan must re-search, not hand back a cached non-match.
    assert discovery.resolve_artist("Phantom Singer", "tok") == (None, "Phantom Singer")
    assert len(calls) == 2
    assert "Phantom Singer" not in discovery._resolve_cache


def test_artist_search_failure_is_not_returned_as_a_no_match(monkeypatch):
    monkeypatch.setattr(discovery, "_resolve_cache", {})

    def malformed(*_args, **_kwargs):
        from qobuz_librarian.api.auth import QobuzError
        raise QobuzError("malformed artist/search response")

    monkeypatch.setattr(discovery, "search_artists", malformed)

    with pytest.raises(discovery.ArtistSearchUnavailable):
        discovery.resolve_artist("Phantom Singer", "tok")


def test_new_releases_surface_only_what_appeared_since_the_baseline(
        monkeypatch, tmp_path):
    # resolve_artist hands back an int id (as Qobuz does) but the baseline is
    # persisted as JSON, so it comes back string-keyed — the engine must match
    # the two or it re-baselines forever and never surfaces anything.
    owned = _album(101, "Ocean Eyes", "Billie Eilish", 2016,
                   [_qt(f"o{i}", f"ISRCO{i}") for i in range(4)])
    old = _album(202, "Happier Than Ever", "Billie Eilish", 2021,
                 [_qt(f"h{i}", f"ISRCH{i}") for i in range(16)])
    # A genuinely-new release: dated in the current year so it always stays
    # inside the default recency window, whenever the suite runs.
    fresh = _album(303, "Hit Me Hard And Soft", "Billie Eilish", date.today().year,
                   [_qt(f"s{i}", f"ISRCS{i}") for i in range(10)])
    _library(monkeypatch, tmp_path,
             {"Billie Eilish": {"Ocean Eyes (2016)":
                                [_et(f"o{i}", f"ISRCO{i}") for i in range(4)]}})
    FakeQobuz(artists=[{"name": "Billie Eilish", "id": 2867335}],
              catalog=[owned, old, fresh]).install(monkeypatch)
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    ad = tmp_path / "Billie Eilish"
    opts = DiscoveryOpts(prefer_hires=True)

    first = find_new_releases_for_artist("Billie Eilish", token="tok", opts=opts,
                                         seen_by_id=None, artist_dir=ad)
    assert first.new_gaps == []                       # first check only baselines
    assert set(first.current_ids) == {"101", "202", "303"}

    # A later check that already knew the old catalog (string-keyed, as stored)
    # surfaces only the unowned album that's new since — not the owned one, not
    # the one it had already seen.
    later = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): ["101", "202"]}, artist_dir=ad)
    assert _titles(later.new_gaps) == ["Hit Me Hard And Soft"]

    caught_up = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): first.current_ids}, artist_dir=ad)
    assert caught_up.new_gaps == []

    # Recency window: an OLD album that's new to the baseline (Qobuz back-
    # filled it) is a back-catalogue gap, not a new release.
    recency = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): ["101"]}, artist_dir=ad)
    assert _titles(recency.new_gaps) == ["Hit Me Hard And Soft"]
