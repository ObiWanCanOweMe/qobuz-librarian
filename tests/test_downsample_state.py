from pathlib import Path

from qobuz_librarian.library.downsample import DownsampleCandidate


def _candidate(album_dir: Path, artist="Artist", title="Album"):
    return DownsampleCandidate(
        album_dir=album_dir,
        artist=artist,
        title=title,
        n_hires=2,
        n_flac=3,
        source_rates=[96000],
        target_rates=[48000],
        est_saving=1234,
    )


def test_refresh_for_artists_writes_shared_downsample_state(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    candidate = _candidate(album_dir)

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        hidden=None,
        scan_artist=lambda _ad: [candidate],
    )

    assert result.complete is True
    assert result.candidates == [candidate]
    state = downsample_state.load()
    assert state["complete"] is True
    assert state["artists_scanned"] == ["Artist"]
    assert state["candidates"][0]["artist"] == "Artist"
    assert state["candidates"][0]["album_dir"] == str(album_dir)


def test_refresh_for_artists_uses_downsample_hidden_scope(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, hidden

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    candidate = _candidate(album_dir)
    hidden_store = hidden.load()
    hidden_store[hidden.SCOPE_DOWNSAMPLE][hidden.album_fingerprint("Artist", "Album")] = {
        "artist": "Artist",
        "title": "Album",
    }

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        hidden=hidden_store,
        scan_artist=lambda _ad: [candidate],
    )

    assert result.candidates == []
    assert downsample_state.load()["candidates"] == []


def test_update_artist_replaces_only_that_artists_downsample_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    old_artist = tmp_path / "Artist"
    other_artist = tmp_path / "Other Artist"
    old_album = old_artist / "Old Album"
    other_album = other_artist / "Other Album"
    new_album = old_artist / "New Album"
    for path in (old_album, other_album, new_album):
        path.mkdir(parents=True)

    downsample_state.save(downsample_state.RefreshResult(
        candidates=[
            _candidate(old_album, artist="Artist", title="Old Album"),
            _candidate(other_album, artist="Other Artist", title="Other Album"),
        ],
        artists_scanned=["Artist", "Other Artist"],
        errors={},
        complete=True,
    ))

    result = downsample_state.update_artist(
        old_artist,
        hidden=None,
        scan_artist=lambda _ad: [
            _candidate(new_album, artist="Artist", title="New Album")
        ],
    )

    assert [c.title for c in result.candidates] == ["New Album"]
    state = downsample_state.load()
    assert [c["title"] for c in state["candidates"]] == [
        "Other Album",
        "New Album",
    ]
    assert state["artists_scanned"] == ["Artist", "Other Artist"]


def test_parallel_update_artist_keeps_both_downsample_refreshes(
        tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    first_artist = tmp_path / "First Artist"
    second_artist = tmp_path / "Second Artist"
    first_album = first_artist / "Album"
    second_album = second_artist / "Album"
    first_album.mkdir(parents=True)
    second_album.mkdir(parents=True)
    downsample_state.save(downsample_state.RefreshResult([], [], {}, True))
    monkeypatch.setattr(
        downsample_state, "artist_fingerprint", lambda artist_dir: artist_dir.name)

    real_load = downsample_state.load
    load_barrier = threading.Barrier(2)

    def raced_load():
        state = real_load()
        try:
            load_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return state

    monkeypatch.setattr(downsample_state, "load", raced_load)

    def refresh(artist_dir):
        return downsample_state.update_artist(
            artist_dir,
            hidden=None,
            scan_artist=lambda ad: [
                _candidate(ad / "Album", artist=ad.name, title=f"{ad.name} Album")
            ],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh, first_artist), pool.submit(refresh, second_artist)]
        for future in futures:
            future.result()

    state = real_load()
    assert sorted(c["artist"] for c in state["candidates"]) == [
        "First Artist",
        "Second Artist",
    ]


def test_refresh_for_artists_reuses_unchanged_downsample_artist(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr(downsample_state, "artist_fingerprint",
                        lambda _path: "same", raising=False)

    calls = []
    first = downsample_state.refresh_for_artists(
        [artist_dir],
        hidden=None,
        scan_artist=lambda _ad: calls.append("scan") or [_candidate(album_dir)],
    )
    assert [c.title for c in first.candidates] == ["Album"]

    def fail_if_called(_artist_dir):
        raise AssertionError("unchanged artist should be reused")

    second = downsample_state.refresh_for_artists(
        [artist_dir],
        hidden=None,
        scan_artist=fail_if_called,
        skip_unchanged=True,
    )

    assert calls == ["scan"]
    assert [c.title for c in second.candidates] == ["Album"]


def test_full_save_preserves_newer_targeted_downsample_refresh_with_same_fingerprint(
        tmp_path, monkeypatch):
    import time

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr(downsample_state, "artist_fingerprint",
                        lambda _path: "same", raising=False)
    stale_candidate = _candidate(album_dir, title="Stale Album")
    downsample_state.save(downsample_state.RefreshResult(
        [stale_candidate],
        ["Artist"],
        {},
        True,
        {"Artist": "same"},
    ))

    refresh_started_at = time.time()
    time.sleep(0.001)
    full_result = downsample_state.RefreshResult(
        [stale_candidate],
        ["Artist"],
        {},
        True,
        {"Artist": "same"},
    )
    downsample_state.update_artist(
        artist_dir,
        hidden=None,
        scan_artist=lambda _ad: [],
    )
    downsample_state.save(
        full_result,
        preserve_concurrent=True,
        refresh_started_at=refresh_started_at,
    )

    assert downsample_state.load()["candidates"] == []


def test_cancelled_refresh_keeps_last_complete_downsample_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    downsample_state.save(downsample_state.RefreshResult(
        [_candidate(album_dir)],
        ["Artist"],
        {},
        True,
    ))

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        scan_artist=lambda _ad: (_ for _ in ()).throw(
            AssertionError("cancelled refresh should not scan")),
        cancel_check=lambda: True,
    )

    state = downsample_state.load()
    assert result.complete is False
    assert state["complete"] is True
    assert [c["title"] for c in state["candidates"]] == ["Album"]


def test_artist_error_keeps_last_complete_downsample_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    artist_dir.mkdir()
    album_dir.mkdir()
    downsample_state.save(downsample_state.RefreshResult(
        [_candidate(album_dir)],
        ["Artist"],
        {},
        True,
    ))

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        scan_artist=lambda _ad: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    state = downsample_state.load()
    assert result.complete is False
    assert result.errors == {"Artist": "boom"}
    assert state["complete"] is True
    assert [c["title"] for c in state["candidates"]] == ["Album"]
