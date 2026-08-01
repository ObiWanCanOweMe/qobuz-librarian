import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.library.downsample import DownsampleCandidate
from qobuz_librarian.web import jobs as jm


def _candidate(album_dir: Path):
    return DownsampleCandidate(
        album_dir=album_dir,
        artist="Artist",
        title="Album (2024)",
        n_hires=1,
        n_flac=1,
        source_rates=[96000],
        target_rates=[48000],
        est_saving=2048,
    )


def test_artist_fingerprint_tracks_exact_release_manifest_state(tmp_path):
    from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
    from qobuz_librarian.library.release_identity import (
        MANIFEST_NAME,
        ReleaseIdentity,
        publish_release_identity,
    )

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")

    missing = artist_fingerprint(artist_dir)
    publish_release_identity(album_dir, ReleaseIdentity("qobuz", "100"))
    first = artist_fingerprint(artist_dir)
    (album_dir / MANIFEST_NAME).unlink()
    removed = artist_fingerprint(artist_dir)
    publish_release_identity(album_dir, ReleaseIdentity("qobuz", "200"))
    replaced = artist_fingerprint(artist_dir)
    (album_dir / MANIFEST_NAME).write_text("not-json", encoding="utf-8")
    malformed = artist_fingerprint(artist_dir)

    assert first != missing
    assert removed == missing
    assert replaced not in {missing, first}
    assert malformed not in {missing, first, replaced}


def test_artist_fingerprint_fails_closed_if_manifest_vanishes_during_read(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import artist_fingerprint as fingerprint_mod
    from qobuz_librarian.library.release_identity import MANIFEST_NAME

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fingerprint_mod, "read_release_identity", lambda _p: None)

    value = fingerprint_mod.artist_fingerprint(album_dir.parent)

    assert len(value) == 64


def test_artist_fingerprint_never_follows_a_manifest_symlink(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import artist_fingerprint as fingerprint_mod
    from qobuz_librarian.library.release_identity import MANIFEST_NAME

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (album_dir / MANIFEST_NAME).symlink_to(outside)
    monkeypatch.setattr(
        fingerprint_mod,
        "read_release_identity",
        lambda _p: (_ for _ in ()).throw(
            AssertionError("manifest symlink must not be opened")),
    )

    value = fingerprint_mod.artist_fingerprint(album_dir.parent)

    assert len(value) == 64


def test_artist_fingerprint_tracks_reserved_manifest_directory(tmp_path):
    from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
    from qobuz_librarian.library.release_identity import MANIFEST_NAME

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    missing = artist_fingerprint(album_dir.parent)

    reserved = album_dir / MANIFEST_NAME
    reserved.mkdir()
    invalid_directory = artist_fingerprint(album_dir.parent)
    reserved.rmdir()
    removed = artist_fingerprint(album_dir.parent)

    assert invalid_directory != missing
    assert removed == missing


def test_artist_fingerprint_tracks_reserved_symlink_to_directory(tmp_path):
    from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
    from qobuz_librarian.library.release_identity import MANIFEST_NAME

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    outside = tmp_path / "outside"
    outside.mkdir()
    missing = artist_fingerprint(album_dir.parent)

    reserved = album_dir / MANIFEST_NAME
    reserved.symlink_to(outside, target_is_directory=True)
    invalid_symlink = artist_fingerprint(album_dir.parent)
    reserved.unlink()
    removed = artist_fingerprint(album_dir.parent)

    assert invalid_symlink != missing
    assert removed == missing


def test_downsample_scan_uses_shared_refresh_state(tmp_path, monkeypatch):
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    calls = []

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        calls.append([a.name for a in artist_list])
        kwargs["on_artist"](artist_list[0], [_candidate(album_dir)], None, 1, 1)
        return downsample_state.RefreshResult(
            candidates=[_candidate(album_dir)],
            artists_scanned=["Artist"],
            errors={},
            complete=True,
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
    job = jm.Job(title="downsample")

    flows.scan_downsamples(job)

    assert calls == [["Artist"]]
    assert len(job.candidates) == 1
    assert job.candidates[0]["kind"] == "downsample"
    assert job.summary.startswith("1 album stored above CD rate")


def test_downsample_scan_rechecks_hidden_before_adding_active_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, hidden
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    candidate = _candidate(album_dir)
    badge_calls = []

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        hidden.hide(hidden.SCOPE_DOWNSAMPLE, [("Artist", "Album (2024)", "")])
        kwargs["on_artist"](artist_list[0], [candidate], None, 1, 1)
        return downsample_state.RefreshResult([candidate], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.scan_downsamples(job)

    assert job.candidates == []
    assert badge_calls == [("downsample", False)]


def test_baseline_scan_refreshes_shared_downsample_state(tmp_path, monkeypatch):
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    calls = []

    def fake_refresh(artists, **kwargs):
        calls.append([a.name for a in artists])
        return downsample_state.RefreshResult([], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", []),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert calls == [["Artist"]]


def test_upgrade_scan_uses_shared_refresh_state(tmp_path, monkeypatch):
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    calls = []
    spec = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        calls.append([a.name for a in artist_list])
        kwargs["on_artist"](artist_list[0], [spec], None, 1, 1)
        return upgrade_state.RefreshResult([spec], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_refresh)
    job = jm.Job(title="upgrade")

    flows.scan_upgrades(job, "tok")

    assert calls == [["Artist"]]
    assert len(job.candidates) == 1
    assert job.candidates[0]["kind"] == "upgrade"


def test_upgrade_scan_rechecks_hidden_before_adding_active_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    spec = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }
    badge_calls = []

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        hidden.hide(hidden.SCOPE_UPGRADE, [("Artist", "Album", "2024")])
        kwargs["on_artist"](artist_list[0], [spec], None, 1, 1)
        return upgrade_state.RefreshResult([spec], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    job = jm.Job(title="upgrade")

    flows.scan_upgrades(job, "tok")

    assert job.candidates == []
    assert badge_calls == [("upgrade", False)]


def test_execute_downsamples_refreshes_affected_artist_state(tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    refreshed = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_dir": str(album_dir)},
    }])

    assert refreshed == ["Artist"]


def test_targeted_upgrade_refresh_respects_upgrade_scan_disabled(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(
        flows.upgrade_state,
        "update_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("disabled upgrade scan should not refresh")),
    )
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)

    flows._refresh_after_local_album_change(
        {"artist": {"name": "Artist"}, "title": "Album"},
        {"dir": album_dir},
        token="tok",
        upgrade=True,
    )


def test_execute_upgrades_refreshes_upgrade_and_downsample_state(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    refreshed_upgrade = []
    refreshed_downsample = []
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    monkeypatch.setattr(flows, "get_album",
                        lambda album_id, token: {"id": album_id, "title": "Album"})
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "result": "downloaded",
                                         "dir": album_dir})
    monkeypatch.setattr("qobuz_librarian.library.catalog.find_existing_tracks",
                        lambda album: ([], None))
    monkeypatch.setattr(flows.upgrade_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed_upgrade.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed_downsample.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    job = jm.Job(title="upgrade")

    flows.execute_upgrades(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert refreshed_upgrade == ["Artist"]
    assert refreshed_downsample == ["Artist"]


def test_execute_upgrades_marks_partial_cap_before_refresh(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192,
    }
    refreshed_upgrade = []

    monkeypatch.setattr(flows, "get_album", lambda album_id, token: album)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "result": "downloaded",
                                         "dir": album_dir,
                                         "quality_verdict": {
                                             "under": True,
                                             "recovered": False,
                                             "n_below": 1,
                                         }})
    monkeypatch.setattr("qobuz_librarian.library.catalog.find_existing_tracks",
                        lambda _album: ([object()], None))
    monkeypatch.setattr(
        "qobuz_librarian.quality.decision.compare_album_quality",
        lambda *_a, **_k: {
            "classification": "mixed_below",
            "n_at": 0,
            "n_below": 1,
            "n_above": 0,
        },
    )

    def refresh_upgrade(_artist_dir, **_kwargs):
        assert decision.is_album_capped("alb-1", decision.load_capped())
        refreshed_upgrade.append(_artist_dir.name)
        return SimpleNamespace(complete=True, candidates=[])

    monkeypatch.setattr(flows.upgrade_state, "update_artist", refresh_upgrade)
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda _artist_dir, **_kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    job = jm.Job(title="upgrade")

    flows.execute_upgrades(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert refreshed_upgrade == ["Artist"]


def test_execute_upgrades_does_not_mark_cap_when_staging_verdict_passed(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192,
    }

    monkeypatch.setattr(flows, "get_album", lambda album_id, token: album)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "result": "downloaded",
                                         "dir": album_dir,
                                         "quality_verdict": {
                                             "under": False,
                                             "recovered": False,
                                             "n_below": 0,
                                         }})
    monkeypatch.setattr("qobuz_librarian.library.catalog.find_existing_tracks",
                        lambda _album: ([object()], None))
    monkeypatch.setattr(
        "qobuz_librarian.quality.decision.compare_album_quality",
        lambda *_a, **_k: {
            "classification": "mixed_below",
            "n_at": 0,
            "n_below": 1,
            "n_above": 0,
        },
    )
    monkeypatch.setattr(flows.upgrade_state, "update_artist",
                        lambda _artist_dir, **_kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda _artist_dir, **_kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    job = jm.Job(title="upgrade")

    flows.execute_upgrades(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert not decision.is_album_capped("alb-1", decision.load_capped())


def test_baseline_scan_refreshes_shared_upgrade_state(tmp_path, monkeypatch):
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    calls = []

    def fake_downsample_refresh(artists, **kwargs):
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
            hidden_signature="",
        )

    def fake_upgrade_refresh(artists, **kwargs):
        calls.append([a.name for a in artists])
        return upgrade_state.RefreshResult([], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_upgrade_refresh)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", []),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert calls == [["Artist"]]


def test_cancelled_baseline_scan_does_not_publish_quality_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    downsample_candidate = _candidate(album_dir)
    upgrade_candidate = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }
    badge_calls = []

    def fake_downsample_refresh(_artists, **kwargs):
        result = downsample_state.RefreshResult(
            [downsample_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            downsample_state.save(result)
        return result

    def fake_upgrade_refresh(_artists, **kwargs):
        result = upgrade_state.RefreshResult(
            [upgrade_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            upgrade_state.save(result)
        return result

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def cancel_during_library_scan(ad, token, partial_only, hidden):
        job.cancel_requested = True
        return ad.name, ad.name, [], "artist-id", []

    monkeypatch.setattr(flows, "_scan_library_artist", cancel_during_library_scan)
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert downsample_state.load()["complete"] is False
    assert upgrade_state.load()["complete"] is False
    assert badge_calls == []


def test_incomplete_baseline_scan_does_not_publish_quality_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    downsample_candidate = _candidate(album_dir)
    upgrade_candidate = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }
    badge_calls = []

    def fake_downsample_refresh(_artists, **kwargs):
        result = downsample_state.RefreshResult(
            [downsample_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            downsample_state.save(result)
        return result

    def fake_upgrade_refresh(_artists, **kwargs):
        result = upgrade_state.RefreshResult(
            [upgrade_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            upgrade_state.save(result)
        return result

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("artist failed")),
    )

    flows.scan_library(jm.Job(title="baseline"), "tok")

    assert downsample_state.load()["complete"] is False
    assert upgrade_state.load()["complete"] is False
    assert badge_calls == []


def test_resumed_baseline_scan_can_complete_saved_library_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    good_dir = tmp_path / "Good"
    next_dir = tmp_path / "Next"
    good_dir.mkdir()
    next_dir.mkdir()
    attention = {
        "kind": "identity_attention",
        "title": "Album",
        "artist": "Good",
        "detail": "Release identity needs manual review",
        "payload": {"non_actionable": True, "_artist_dir": "Good"},
        "selected": False,
    }
    cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({
        "missing": {
            "scanned": ["Good"],
            "candidates": [attention],
            "seen": {"good-id": ["good-album"]},
            "artists": {
                "Good": {
                    "fingerprint": "good-fp",
                    "candidates": [attention],
                    "artist_id": "good-id",
                    "catalog_ids": ["good-album"],
                },
            },
        },
    }), encoding="utf-8")
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("Good", "Album", "")])

    def fake_downsample_refresh(_artists, **_kwargs):
        return downsample_state.RefreshResult(
            [], ["Good", "Next"], {}, True,
            {"Good": "good-fp", "Next": "next-fp"},
        )

    def fake_upgrade_refresh(_artists, **_kwargs):
        return upgrade_state.RefreshResult(
            [], ["Good", "Next"], {}, True,
            {"Good": "good-fp", "Next": "next-fp"},
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir, next_dir])
    monkeypatch.setattr(flows, "artist_fingerprint",
                        lambda path: f"{path.name.lower()}-fp", raising=False)
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden:
            (ad.name, ad.name, [], "next-id", ["next-album"]),
    )

    job = jm.Job(title="baseline")
    flows.scan_library(job, "tok")

    state = library_scan_state.kind_state("missing")
    assert state["complete"] is True
    assert sorted(state["artists"]) == ["Good", "Next"]
    assert [c["kind"] for c in job.candidates] == ["identity_attention"]
    assert flows.scan_checkpoint.load("missing") is None


def test_resume_rescans_artist_when_checkpoint_fingerprint_changed(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
    from qobuz_librarian.library.release_identity import (
        ReleaseIdentity,
        publish_release_identity,
    )
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE", tmp_path / "checkpoint.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    saved_fingerprint = artist_fingerprint(artist_dir)
    stale_attention = {
        "kind": "identity_attention",
        "title": "Album",
        "artist": "Artist",
        "detail": "stale warning",
        "payload": {"non_actionable": True, "_artist_dir": "Artist"},
        "selected": False,
    }
    cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({
        "missing": {
            "scanned": ["Artist"],
            "candidates": [stale_attention],
            "seen": {"artist-id": ["old"]},
            "artists": {
                "Artist": {
                    "fingerprint": saved_fingerprint,
                    "candidates": [stale_attention],
                    "artist_id": "artist-id",
                    "catalog_ids": ["old"],
                },
            },
        },
    }), encoding="utf-8")
    publish_release_identity(album_dir, ReleaseIdentity("qobuz", "100"))
    scanned = []

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult(
            [], ["Artist"], {}, True, {"Artist": "unused"}),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: upgrade_state.RefreshResult(
            [], ["Artist"], {}, True, {"Artist": "unused"}),
    )
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def fake_scan(ad, _token, _partial_only, _hidden):
        scanned.append(ad.name)
        return ad.name, ad.name, [], "artist-id", ["fresh"], []

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan)
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert scanned == ["Artist"]
    assert job.candidates == []
    state = library_scan_state.kind_state("missing")
    assert state["artists"]["Artist"]["catalog_ids"] == ["fresh"]


def test_resumed_baseline_rescans_checkpoint_entries_without_artist_snapshot(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    good_dir.mkdir()
    cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({
        "missing": {
            "scanned": ["Good"],
            "candidates": [{
                "kind": "album",
                "title": "Stale",
                "artist": "Good",
                "detail": "old checkpoint candidate",
                "payload": {"album_id": "old", "_artist_dir": "Good"},
                "selected": False,
            }],
            "seen": {"good-id": ["old"]},
        },
    }), encoding="utf-8")
    scanned = []

    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir])
    monkeypatch.setattr(flows, "artist_fingerprint",
                        lambda path: f"{path.name.lower()}-fp", raising=False)
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult(
            [], ["Good"], {}, True, {"Good": "good-fp"}),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: upgrade_state.RefreshResult(
            [], ["Good"], {}, True, {"Good": "good-fp"}),
    )
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def fake_scan_artist(ad, token, partial_only, hidden):
        scanned.append(ad.name)
        return ad.name, ad.name, [], "good-id", ["fresh"]

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan_artist)
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    state = library_scan_state.kind_state("missing")
    assert scanned == ["Good"]
    assert job.candidates == []
    assert state["complete"] is True
    assert sorted(state["artists"]) == ["Good"]
    assert flows.scan_checkpoint.load("missing") is None


def test_scan_library_reuses_unchanged_artist_snapshot(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    saved_candidate = {
        "kind": "album",
        "title": "Saved Album",
        "artist": "Artist",
        "detail": "2024 · CD quality · 10 tracks",
        "payload": {"album_id": "saved", "_artist_dir": "Artist"},
        "selected": False,
    }
    attention = {
        "kind": "identity_attention",
        "title": "Album",
        "artist": "Artist",
        "detail": "Release identity needs manual review",
        "payload": {"non_actionable": True, "_artist_dir": "Artist"},
        "selected": False,
    }
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("Artist", "Album", "")])
    hidden = hidden_mod.load()
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "same",
                "candidates": [saved_candidate, attention],
                "artist_id": "artist-id",
                "catalog_ids": ["saved"],
            },
        },
        complete=True,
        hidden_signature=library_scan_state.hidden_signature(
            hidden, hidden_mod.SCOPE_MISSING),
        quality_sig=library_scan_state.quality_signature(),
    )
    downsample_skip = []
    upgrade_skip = []

    def fake_downsample_refresh(artists, **kwargs):
        downsample_skip.append(kwargs.get("skip_unchanged"))
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
            hidden_signature="",
        )

    def fake_upgrade_refresh(artists, **kwargs):
        upgrade_skip.append(kwargs.get("skip_unchanged"))
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
            hidden_signature="",
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "artist_fingerprint", lambda _path: "same",
                        raising=False)
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("unchanged artist should be reused")),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert downsample_skip == [True]
    assert upgrade_skip == [True]
    assert [c["title"] for c in job.candidates] == ["Saved Album", "Album"]


def test_scan_library_rescans_saved_artist_after_manifest_publish(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
    from qobuz_librarian.library.release_identity import (
        ReleaseIdentity,
        publish_release_identity,
    )
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    hidden = hidden_mod.load()
    stale = {
        "kind": "identity_attention",
        "title": "Album",
        "artist": "Artist",
        "detail": "stale warning",
        "payload": {"non_actionable": True, "_artist_dir": "Artist"},
        "selected": False,
    }
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": artist_fingerprint(artist_dir),
                "candidates": [stale],
                "artist_id": "artist-id",
                "catalog_ids": ["stale"],
            },
        },
        complete=True,
        hidden_signature=library_scan_state.hidden_signature(
            hidden, hidden_mod.SCOPE_MISSING),
        quality_sig=library_scan_state.quality_signature(),
    )
    publish_release_identity(album_dir, ReleaseIdentity("qobuz", "100"))
    scanned = []

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(
        flows.downsample_state, "refresh_for_artists",
        lambda *_a, **_k: SimpleNamespace(
            complete=True, candidates=[], artists_scanned=[], errors={},
            fingerprints={}, hidden_signature=""),
    )
    monkeypatch.setattr(
        flows.upgrade_state, "refresh_for_artists",
        lambda *_a, **_k: SimpleNamespace(
            complete=True, candidates=[], artists_scanned=[], errors={},
            fingerprints={}, hidden_signature=""),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def fake_scan(ad, _token, _partial_only, _hidden):
        scanned.append(ad.name)
        return ad.name, ad.name, [], "artist-id", ["fresh"], []

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan)
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert scanned == ["Artist"]
    assert job.candidates == []
    state = library_scan_state.kind_state("missing")
    assert state["artists"]["Artist"]["catalog_ids"] == ["fresh"]


def test_scan_library_force_full_ignores_saved_artist_snapshot(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    hidden = hidden_mod.load()
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "same",
                "candidates": [],
                "artist_id": "artist-id",
                "catalog_ids": [],
            },
        },
        complete=True,
        hidden_signature=library_scan_state.hidden_signature(
            hidden, hidden_mod.SCOPE_MISSING),
        quality_sig=library_scan_state.quality_signature(),
    )
    downsample_skip = []
    upgrade_skip = []
    scanned = []

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "artist_fingerprint", lambda _path: "same",
                        raising=False)
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda _artists, **kwargs: downsample_skip.append(
            kwargs.get("skip_unchanged")) or SimpleNamespace(
                complete=True,
                candidates=[],
                artists_scanned=[],
                errors={},
                fingerprints={},
                hidden_signature="",
            ),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda _artists, **kwargs: upgrade_skip.append(
            kwargs.get("skip_unchanged")) or SimpleNamespace(
                complete=True,
                candidates=[],
                artists_scanned=[],
                errors={},
                fingerprints={},
                hidden_signature="",
            ),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden: (
            scanned.append(ad.name) or (ad.name, ad.name, [], "artist-id", [])),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok", force_full=True)

    assert downsample_skip == [False]
    assert upgrade_skip == [False]
    assert scanned == ["Artist"]


def test_scan_library_skips_upgrade_refresh_when_upgrade_disabled(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda _artists, **_kwargs: downsample_state.RefreshResult([], ["Artist"], {}, True),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("upgrade refresh should be skipped")),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", []),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert job.summary == "No missing albums found for artists in your library."


def test_library_artist_scan_ignores_single_store_when_suppression_off(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    seen = []

    def fake_find_missing(*args, **kwargs):
        seen.append(kwargs.get("single_store"))
        return SimpleNamespace(
            artist_id="id1",
            artist_name="Artist",
            gaps=[],
            catalog=[],
            catalog_incomplete=False,
        )

    monkeypatch.setattr(cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False)
    monkeypatch.setattr(flows, "find_missing_for_artist", fake_find_missing)

    flows._scan_library_artist(artist_dir, "tok", False, {"single": {"old": {}}})

    assert seen == [None]


def test_new_release_scan_keeps_incomplete_rebaseline_truthful(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artists = [tmp_path / name for name in ("Good", "Unresolved", "Short", "Error")]
    for artist in artists:
        artist.mkdir()

    def fake_find(name, **_kwargs):
        if name == "Error":
            raise RuntimeError("temporary failure")
        if name == "Unresolved":
            return SimpleNamespace(artist_id=None, fetch_failed=False,
                                   current_ids=[], new_gaps=[], artist_name=None)
        return SimpleNamespace(
            artist_id=name,
            fetch_failed=name == "Short",
            current_ids=["album"],
            new_gaps=[],
            artist_name=name,
        )

    marked = {}
    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
    monkeypatch.setattr(flows, "find_new_releases_for_artist", fake_find)
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"Good": []},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT) - 1,
    })
    monkeypatch.setattr(
        flows.new_releases_mod,
        "mark_run",
        lambda _seen, **kwargs: marked.update(kwargs) or True,
    )
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert marked["complete"] is False
    assert marked["baseline_limit"] is None
    assert "3 artists couldn't be checked" in job.summary
    assert "Recorded a fresh baseline" not in job.summary


def test_new_release_scan_reports_state_save_failure(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist = tmp_path / "Artist"
    artist.mkdir()
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(
        flows,
        "find_new_releases_for_artist",
        lambda *_args, **_kwargs: SimpleNamespace(
            artist_id="artist-id",
            fetch_failed=False,
            current_ids=["album"],
            new_gaps=[],
            artist_name="Artist",
        ),
    )
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"artist-id": []},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })
    monkeypatch.setattr(flows.new_releases_mod, "mark_run", lambda *_a, **_k: False)
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert "couldn't be saved" in job.summary

def test_incomplete_baseline_scan_summary_reports_unchecked_artists(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    bad_dir = tmp_path / "Bad"
    good_dir.mkdir()
    bad_dir.mkdir()

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir, bad_dir])
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult([], [], {}, True),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: upgrade_state.RefreshResult([], [], {}, True),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def fake_scan_artist(ad, token, partial_only, hidden):
        if ad.name == "Bad":
            raise RuntimeError("artist failed")
        return ad.name, ad.name, [], f"{ad.name}-id", [f"{ad.name}-album"]

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan_artist)

    job = jm.Job(title="baseline")
    flows.scan_library(job, "tok")

    # One artist errored, so the checkpoint stays and the crawl was partial —
    # the summary must say so instead of a clean-sounding definitive total.
    assert "1 artist" in job.summary
    assert "resume" in job.summary.lower()


def test_scan_signature_covers_candidate_shaping_settings(monkeypatch):
    """The cheap refresh reuses saved candidates while the signature matches —
    so every setting that changes WHICH candidates a scan yields has to be in
    it, or Settings changes leave stale gap/missing lists."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import library_scan_state as lss

    base = lss.quality_signature()
    for name in ("SUPPRESS_SINGLE_TRACK_GAPS", "EXCLUDE_LIVE_ALBUMS"):
        with monkeypatch.context() as mctx:
            mctx.setattr(cfg, name, not getattr(cfg, name))
            assert lss.quality_signature() != base, name
    for name in ("ARTIST_CATALOG_LIMIT", "MISSING_ALBUMS_MIN_TRACKS"):
        with monkeypatch.context() as mctx:
            mctx.setattr(cfg, name, int(getattr(cfg, name)) + 1)
            assert lss.quality_signature() != base, name


def test_refresh_library_census_publishes_only_complete_inventory(monkeypatch):
    from qobuz_librarian.library import census
    from qobuz_librarian.web import flows

    payload = {
        "version": 1,
        "tiers": {"cd": [1, 10], "hires96": [0, 0],
                  "hires192": [0, 0], "unknown": [0, 0]},
        "total_tracks": 1,
        "total_bytes": 10,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }
    saved = []
    monkeypatch.setattr(
        flows.library_census,
        "build",
        lambda **kwargs: census.InventoryResult(payload, True, 1, []),
    )
    monkeypatch.setattr(
        flows.library_census, "save", lambda data: saved.append(data) or True)

    result = flows._refresh_library_census(jm.Job(title="scan"))

    assert result.complete is True
    assert saved == [payload]


def test_refresh_library_census_preserves_snapshot_on_cancel(monkeypatch):
    from qobuz_librarian.library import census
    from qobuz_librarian.web import flows

    job = jm.Job(title="scan")
    captured = {}

    def cancel_during_build(**kwargs):
        captured["cancel_check"] = kwargs["cancel_check"]
        assert captured["cancel_check"]() is False
        job.cancel_requested = True
        assert captured["cancel_check"]() is True
        return census.InventoryResult(None, False, 0, [], True)

    monkeypatch.setattr(
        flows.library_census,
        "build",
        cancel_during_build,
    )
    monkeypatch.setattr(
        flows.library_census,
        "save",
        lambda _data: (_ for _ in ()).throw(
            AssertionError("cancelled census must not be saved")),
    )

    result = flows._refresh_library_census(job)

    assert result.cancelled is True
    assert captured["cancel_check"]() is job.cancel_requested


def test_scan_library_refreshes_census_before_qobuz_artist_scan(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist = tmp_path / "Artist"
    artist.mkdir()
    order = []
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(
        flows,
        "_refresh_library_census",
        lambda _job: order.append("census"),
    )
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult(
            [], ["Artist"], {}, True),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, *_a, **_k: (
            order.append("qobuz") or
            (ad.name, ad.name, [], "artist-id", [])
        ),
    )

    flows.scan_library(jm.Job(title="scan"), "token")

    assert order[:2] == ["census", "qobuz"]


def test_scan_library_replaces_stale_census_when_last_artist_disappears(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import census
    from qobuz_librarian.web import flows

    root = tmp_path / "music"
    root.mkdir()
    snapshot = tmp_path / "census.json"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    monkeypatch.setattr(cfg, "LIBRARY_CENSUS_FILE", snapshot)
    assert census.save({
        "version": census.STATE_VERSION,
        "tiers": {
            "cd": [3, 30],
            "hires96": [0, 0],
            "hires192": [0, 0],
            "unknown": [0, 0],
        },
        "total_tracks": 3,
        "total_bytes": 30,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    })
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("an empty library must not enter Qobuz matching"),
        ),
    )

    flows.scan_library(jm.Job(title="scan"), "token")

    assert census.load()["total_tracks"] == 0


@pytest.mark.parametrize(
    "relative_track",
    [
        Path("root-level.flac"),
        Path("Various Artists") / "Compilation" / "track.flac",
    ],
)
def test_scan_library_inventories_audio_without_qobuz_eligible_artist(
        tmp_path, monkeypatch, relative_track):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import census
    from qobuz_librarian.web import flows

    root = tmp_path / "music"
    track = root / relative_track
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    monkeypatch.setattr(
        cfg, "LIBRARY_CENSUS_FILE", tmp_path / "census.json")
    monkeypatch.setattr(
        census.scanner,
        "read_audio_meta",
        lambda _path: {
            "bits": 16,
            "sample_rate": 44100,
            "size": len(b"audio"),
            "path": "",
        },
    )
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("non-artist audio must not enter Qobuz matching"),
        ),
    )

    flows.scan_library(jm.Job(title="scan"), "token")

    snapshot = census.load()
    assert snapshot["total_tracks"] == 1
    assert snapshot["tiers"]["cd"] == [1, len(b"audio")]
