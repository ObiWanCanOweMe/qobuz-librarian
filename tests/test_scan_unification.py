import json
from pathlib import Path
from types import SimpleNamespace

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


def test_baseline_publish_preserves_newer_targeted_quality_refresh_without_stale_artists(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_a = tmp_path / "Artist A"
    artist_b = tmp_path / "Artist B"
    album_a = artist_a / "Album A"
    album_b = artist_b / "Album B"
    album_a.mkdir(parents=True)
    album_b.mkdir(parents=True)

    def downsample_candidate(artist, album_dir, title):
        return DownsampleCandidate(
            album_dir=album_dir,
            artist=artist,
            title=title,
            n_hires=1,
            n_flac=1,
            source_rates=[96000],
            target_rates=[48000],
            est_saving=1024,
        )

    stale_a_downsample = downsample_candidate(
        "Artist A", album_a, "Stale A Downsample")
    fresh_a_downsample = downsample_candidate(
        "Artist A", album_a, "Fresh A Downsample")
    old_b_downsample = downsample_candidate(
        "Artist B", album_b, "Old B Downsample")
    fresh_b_downsample = downsample_candidate(
        "Artist B", album_b, "Fresh B Downsample")
    stale_a_upgrade = {
        "title": "Stale A Upgrade",
        "artist": "Artist A",
        "detail": "old",
        "payload": {"album_id": "old-a"},
    }
    old_b_upgrade = {
        "title": "Old B Upgrade",
        "artist": "Artist B",
        "detail": "old",
        "payload": {"album_id": "old-b"},
    }
    fresh_b_upgrade = {
        "title": "Fresh B Upgrade",
        "artist": "Artist B",
        "detail": "new",
        "payload": {"album_id": "new-b"},
    }

    downsample_state.save(downsample_state.RefreshResult(
        [stale_a_downsample, old_b_downsample],
        ["Artist A", "Artist B"],
        {},
        True,
        {"Artist A": "old-a", "Artist B": "old-b"},
    ))
    upgrade_state.save(upgrade_state.RefreshResult(
        [stale_a_upgrade, old_b_upgrade],
        ["Artist A", "Artist B"],
        {},
        True,
        {"Artist A": "old-a", "Artist B": "old-b"},
    ))

    monkeypatch.setattr(flows, "list_library_artists",
                        lambda: [artist_a, artist_b])
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *a, **k: downsample_state.RefreshResult(
            [stale_a_downsample, fresh_b_downsample],
            ["Artist A", "Artist B"],
            {},
            True,
            {"Artist A": "old-a", "Artist B": "new-b"},
        ),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *a, **k: upgrade_state.RefreshResult(
            [stale_a_upgrade, fresh_b_upgrade],
            ["Artist A", "Artist B"],
            {},
            True,
            {"Artist A": "old-a", "Artist B": "new-b"},
        ),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)

    def refresh_during_library_scan(ad, token, partial_only, hidden):
        if ad.name == "Artist A":
            downsample_state.update_artist(
                artist_a,
                scan_artist=lambda _ad: [fresh_a_downsample],
            )
            upgrade_state.update_artist(
                artist_a,
                token="tok",
                args=SimpleNamespace(),
                capped={},
                scan_artist=lambda _ad: [{
                    "qobuz_album": {
                        "id": "new-a",
                        "title": "Fresh A Upgrade",
                        "image": {},
                    },
                    "existing_quality_label": "old",
                    "target_quality_label": "new",
                }],
            )
        return ad.name, ad.name, [], "artist-id", []

    monkeypatch.setattr(flows, "_scan_library_artist", refresh_during_library_scan)

    flows.scan_library(jm.Job(title="baseline"), "tok")

    assert sorted(c["title"] for c in downsample_state.load()["candidates"]) == [
        "Fresh A Downsample",
        "Fresh B Downsample",
    ]
    assert sorted(c["title"] for c in upgrade_state.load()["candidates"]) == [
        "Fresh A Upgrade",
        "Fresh B Upgrade",
    ]


def test_review_badge_candidate_check_ignores_hidden_saved_quality_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    hidden.hide(hidden.SCOPE_UPGRADE, [("Artist", "Album", "2024")])
    hidden.hide(hidden.SCOPE_DOWNSAMPLE, [("Artist", "Album", "2024")])
    monkeypatch.setattr(flows.upgrade_state, "load", lambda: {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "upgrade",
            "payload": {"album_id": "up1"},
        }],
    })
    monkeypatch.setattr(flows.downsample_state, "load", lambda: {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "downsample",
            "album_dir": "/music/Artist/Album",
            "est_saving": 1234,
        }],
    })

    assert flows._surface_has_candidates("upgrade") is False
    assert flows._surface_has_candidates("downsample") is False


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


def test_execute_downsamples_refreshes_state_after_noop(tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    refreshed = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: {"resampled": 0, "saved_bytes": 0, "errors": 0})
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


def test_execute_downsamples_refreshes_state_after_missing_album_folder(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    album_dir = artist_dir / "Removed Album"
    refreshed = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("missing album folder should not be downsampled")))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Removed Album",
        "payload": {"album_dir": str(album_dir)},
    }])

    assert refreshed == ["Artist"]


def test_execute_downsamples_refreshes_state_after_missing_artist_folder(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Removed Album"
    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    downsample_state.save(downsample_state.RefreshResult(
        [_candidate(album_dir)],
        ["Artist"],
        {},
        True,
        {"Artist": "old"},
    ))
    badge_calls = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("missing artist folder should not be downsampled")))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda *a, **k: SimpleNamespace(
                            complete=False, errors={"Artist": "gone"}))
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *a: badge_calls.append(a))
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Removed Album",
        "payload": {"album_dir": str(album_dir)},
    }])

    assert downsample_state.load()["candidates"] == []
    assert badge_calls == [("downsample", False)]


def test_execute_downsamples_skips_candidate_without_album_dir(monkeypatch):
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("missing album_dir must not downsample the cwd")))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("missing album_dir has no artist to refresh")))
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Malformed",
        "payload": {},
    }])

    assert "skipped" in job.summary


def test_execute_downsamples_marks_album_locally_capped(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_dir": str(album_dir)},
    }])

    assert decision.is_local_album_capped(album_dir, decision.load_capped())


def test_execute_downsamples_refreshes_upgrade_state_when_token_available(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    refreshed_upgrade = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.upgrade_state, "update_artist",
                        lambda artist_dir, **kwargs:
                        refreshed_upgrade.append(
                            (artist_dir.name, kwargs.get("token")))
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_dir": str(album_dir)},
    }], token="tok")

    assert refreshed_upgrade == [("Artist", "tok")]


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


def test_incomplete_baseline_scan_does_not_stamp_completed_baselines(
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
    recorded = []
    flagged = []
    seeded = []
    cleared = []
    saved = []

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
    monkeypatch.setattr(flows.scan_checkpoint, "save",
                        lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(flows.scan_checkpoint, "clear",
                        lambda kind: cleared.append(kind))
    monkeypatch.setattr(flows, "_record_last_scan",
                        lambda: recorded.append(True))
    monkeypatch.setattr(flows, "_flag_new_since_last_scan",
                        lambda *args, **kwargs: flagged.append(args))
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete",
                        lambda: False)
    monkeypatch.setattr(flows.new_releases_mod, "seed_baseline",
                        lambda seen: seeded.append(seen))

    def fake_scan_artist(ad, token, partial_only, hidden):
        if ad.name == "Bad":
            raise RuntimeError("artist failed")
        return ad.name, ad.name, [], f"{ad.name}-id", [f"{ad.name}-album"]

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan_artist)

    flows.scan_library(jm.Job(title="baseline"), "tok")

    assert recorded == []
    assert flagged == []
    assert seeded == []
    assert cleared == []
    assert saved
    assert saved[-1][0][0] == "missing"
    assert saved[-1][0][1] == {"Good"}


def test_resumed_baseline_scan_can_complete_saved_library_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    next_dir = tmp_path / "Next"
    good_dir.mkdir()
    next_dir.mkdir()
    cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({
        "missing": {
            "scanned": ["Good"],
            "candidates": [],
            "seen": {"good-id": ["good-album"]},
            "artists": {
                "Good": {
                    "fingerprint": "good-fp",
                    "candidates": [],
                    "artist_id": "good-id",
                    "catalog_ids": ["good-album"],
                },
            },
        },
    }), encoding="utf-8")

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

    flows.scan_library(jm.Job(title="baseline"), "tok")

    state = library_scan_state.kind_state("missing")
    assert state["complete"] is True
    assert sorted(state["artists"]) == ["Good", "Next"]
    assert flows.scan_checkpoint.load("missing") is None


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
    hidden = hidden_mod.load()
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "same",
                "candidates": [saved_candidate],
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
    assert [c["title"] for c in job.candidates] == ["Saved Album"]


def test_scan_library_reuses_unchanged_unmatched_artist_snapshot(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Bonobo, Andreya Triana"
    artist_dir.mkdir()
    hidden = hidden_mod.load()
    downsample_skip = []
    upgrade_skip = []

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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], None, []),
    )

    flows.scan_library(jm.Job(title="baseline"), "tok")

    state = library_scan_state.kind_state("missing")
    assert state["complete"] is True
    assert state["hidden_signature"] == library_scan_state.hidden_signature(
        hidden, hidden_mod.SCOPE_MISSING)
    assert state["artists"]["Bonobo, Andreya Triana"]["artist_id"] == ""
    assert state["artists"]["Bonobo, Andreya Triana"]["catalog_ids"] == []

    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("unchanged unmatched artist should be reused")),
    )

    flows.scan_library(jm.Job(title="baseline"), "tok")

    assert downsample_skip == [False, True]
    assert upgrade_skip == [False, True]


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


def test_new_release_scan_ignores_single_store_when_suppression_off(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    seen = []

    def fake_find_new(*args, **kwargs):
        seen.append(kwargs.get("single_store"))
        return SimpleNamespace(
            artist_id="id1",
            fetch_failed=False,
            current_ids=[],
            new_gaps=[],
            artist_name="Artist",
        )

    monkeypatch.setattr(cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "find_new_releases_for_artist", fake_find_new)
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"id1": []},
        "baseline_limit": cfg.ARTIST_CATALOG_LIMIT,
    })
    monkeypatch.setattr(flows.new_releases_mod, "mark_run", lambda *a, **k: None)
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert seen == [None]

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
