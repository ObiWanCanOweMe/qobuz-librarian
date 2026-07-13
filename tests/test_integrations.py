"""Tests for integrations/rip.py, integrations/beets.py, integrations/lyrics.py
— the streamrip/beets seams where most real bugs live."""

import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.lyrics import (
    load_lyric_retry,
    save_lyric_retry,
)
from qobuz_librarian.integrations.rip import (
    _FLAC_TRUNCATION_FLOOR,
    cleanup_lossy,
    cleanup_staging_residue,
    is_flac,
)

# ── shared SQLite transaction boundary ────────────────────────────────


def test_database_transactions_keep_the_public_sqlite_journal_name(
    tmp_path, monkeypatch
):
    import sqlite3

    from qobuz_librarian import config
    from qobuz_librarian.integrations import beets

    database = tmp_path / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (path BLOB)")
        connection.execute("INSERT INTO items VALUES (?)", (b"old",))
    monkeypatch.setattr(config, "BEETS_DB_PATH", database)

    anchor = beets._open_beets_database_anchor()
    connection_path = beets._beets_database_connection_path
    close = beets._close_beets_database_anchor

    connection = None
    try:
        path = connection_path(anchor)
        connection = sqlite3.connect(path)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE items SET path = ?", (b"new",))

        assert path == (f"/proc/self/fd/{anchor['parent_chain'][-1]}/{database.name}")
        assert database.with_name(f"{database.name}-journal").is_file()
        assert not list(tmp_path.glob(".qobuz-db-transaction-*-journal"))
        assert not list(tmp_path.glob(".qobuz-db-transaction-*"))
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        close(anchor)


def test_managed_album_boundary_uses_beets_album_authority(tmp_path, monkeypatch):
    import sqlite3

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    disc_one = album / "Disc 1"
    disc_two = album / "Disc 2"
    disc_one.mkdir(parents=True)
    disc_two.mkdir()
    first = disc_one / "01.flac"
    second = disc_two / "01.flac"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    database = tmp_path / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB)"
        )
        connection.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO albums VALUES (7)")
        connection.executemany(
            "INSERT INTO items (album_id, path) VALUES (?, ?)",
            ((7, os.fsencode(first)), (7, os.fsencode(second))),
        )
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)

    root_identity = beets._ownership_identity(os.stat(music))
    destinations = {
        first.relative_to(music).as_posix(),
        second.relative_to(music).as_posix(),
    }
    boundary = beets._managed_album_boundary(destinations, music, root_identity)

    assert boundary == (
        "Artist/Album",
        tuple(
            beets._ownership_identity(os.stat(album))[field]
            for field in beets._OWNERSHIP_IDENTITY_FIELDS
        ),
    )

    other_disc = music / "Artist" / "Other Album" / "Disc 2"
    other_disc.mkdir(parents=True)
    moved = other_disc / second.name
    moved.write_bytes(second.read_bytes())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE items SET path = ? WHERE id = 2",
            (os.fsencode(moved),),
        )

    assert beets._managed_album_boundary(destinations, music, root_identity) is None

    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB)"
        )
        connection.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO albums VALUES (7)")
        connection.executemany(
            "INSERT INTO items (album_id, path) VALUES (?, ?)",
            ((7, os.fsencode(first)), (7, os.fsencode(second))),
        )
    real_connect = sqlite3.connect
    swaps = []

    def connect_during_leaf_swap(value, *args, **kwargs):
        if swaps:
            return real_connect(value, *args, **kwargs)
        swaps.append(True)
        held = tmp_path / "held.db"
        os.replace(database, held)
        os.replace(replacement, database)
        try:
            connection = real_connect(value, *args, **kwargs)
        finally:
            os.replace(database, replacement)
            os.replace(held, database)
        return connection

    monkeypatch.setattr(beets.sqlite3, "connect", connect_during_leaf_swap)
    assert beets._managed_album_boundary(destinations, music, root_identity) is None
    assert swaps == [True]


# ── rip: FLAC validation + lossy cleanup ──────────────────────────────────


def test_is_flac_rejects_truncated_keeps_complete(tmp_path, _need_ffmpeg, _need_flac):
    def _sine(path, seconds):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=44100:duration={seconds}",
                "-c:a",
                "flac",
                str(path),
            ],
            check=True,
        )

    # A short but complete track is real audio — keep it even though it sits
    # well under the size heuristic the no-flac fallback uses.
    short = tmp_path / "interlude.flac"
    _sine(short, 1.2)
    assert short.stat().st_size < _FLAC_TRUNCATION_FLOOR
    assert is_flac(short) is True

    # An interrupted download leaves a file whose header still advertises the
    # full duration, so only decoding the (missing) frames exposes the gap.
    full = tmp_path / "full.flac"
    _sine(full, 3)
    data = full.read_bytes()
    partial = tmp_path / "partial.flac"
    partial.write_bytes(data[: len(data) * 2 // 5])
    assert is_flac(partial) is False

    assert is_flac(tmp_path / "never-written.flac") is False


def test_flac_audio_ok_treats_a_verify_timeout_as_broken(monkeypatch):
    # A `flac -t` that hangs past the timeout (a pathological/corrupt large FLAC)
    # must read as broken (False), not as "tool absent" (None): None routes a
    # large file through the size heuristic, which trusts it. FLAC verifies far
    # faster than real time, so a hang is the file, not the tool.
    import qobuz_librarian.integrations.rip as rip

    monkeypatch.setattr(rip.shutil, "which", lambda name: "/usr/bin/flac")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="flac", timeout=300)

    monkeypatch.setattr(rip.subprocess, "run", hang)

    assert rip.flac_audio_ok("/any/large.flac") is False


def test_cleanup_lossy_sorts_flac_lossy_and_broken(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)
    good = tmp_path / "good.flac"
    good.write_bytes(b"\x00" * 200_000)
    bad = tmp_path / "truncated.flac"
    bad.write_bytes(b"\x00" * 200_000)
    mp3 = tmp_path / "track.mp3"
    mp3.write_bytes(b"\x00" * 1000)
    # is_flac stubbed: only `good` verifies; the other FLAC is treated as broken.
    with patch("qobuz_librarian.integrations.rip.is_flac", side_effect=lambda p: p == good):
        kept, lossy, broken = cleanup_lossy([good, bad, mp3])
    assert kept == [good]
    assert lossy == [mp3] and broken == [bad]
    assert not bad.exists() and not mp3.exists()


# ── rip: staging residue cleanup ─────────────────────────────────────────


def test_cleanup_staging_residue_keeps_art_beside_leftover_audio(tmp_path, monkeypatch):
    # An interrupted run can leave a fully-downloaded album in staging; its
    # cover.jpg is the filesystem fetchart source on import (ARTWORK=sidecar).
    # The sweep must not delete residue that sits beside real audio.
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
    (album / "cover.jpg").write_bytes(b"img")
    (album / "meta.json").write_text("{}")
    # Multi-disc: art at album root, audio one level down.
    boxset = tmp_path / "Artist" / "BoxSet"
    (boxset / "Disc 1").mkdir(parents=True)
    (boxset / "Disc 1" / "01.flac").write_bytes(b"audio" * 1000)
    (boxset / "cover.jpg").write_bytes(b"img")
    # A legacy orphan has no run receipt, so its current occupant is preserved.
    orphan = tmp_path / "Old"
    orphan.mkdir()
    (orphan / "cover.jpg").write_bytes(b"img")

    cleanup_staging_residue()
    assert (album / "cover.jpg").exists() and (album / "meta.json").exists()
    assert (boxset / "cover.jpg").exists()
    assert (orphan / "cover.jpg").exists()


# ── exact empty-directory cleanup ────────────────────────────────────────


def test_exact_empty_directory_cleanup_does_not_hide_a_late_replacement(
    tmp_path, monkeypatch
):
    import qobuz_librarian.integrations.beets as beets

    parent = tmp_path / "source"
    parent.mkdir()
    public = parent / "empty"
    public.mkdir()
    parent_fd = beets._open_ownership_dir(parent)
    held_directory = beets._open_ownership_dir(public)
    real_rename = beets._rename_ownership_noreplace
    replaced = False

    def replace_before_quarantine(source_fd, source_name, destination_fd, destination_name):
        nonlocal replaced
        if not replaced and str(destination_name).startswith(".qobuz-merge-cleanup-"):
            replaced = True
            public.rmdir()
            public.mkdir()
            (public / "late.txt").write_bytes(b"late replacement")
        return real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(beets, "_rename_ownership_noreplace", replace_before_quarantine)

    try:
        assert (
            beets._remove_exact_empty_merge_dir(
                parent_fd,
                public.name,
                held_directory,
            )
            is False
        )
        assert replaced is True
        assert (public / "late.txt").read_bytes() == b"late replacement"
        assert not list(parent.glob(".qobuz-merge-*"))
    finally:
        os.close(held_directory)
        os.close(parent_fd)


def test_exact_empty_directory_cleanup_rejects_a_post_unlink_fsync_error(
    tmp_path, monkeypatch
):
    import errno

    import qobuz_librarian.integrations.beets as beets

    parent = tmp_path / "source"
    parent.mkdir()
    public = parent / "empty"
    public.mkdir()
    parent_fd = beets._open_ownership_dir(parent)
    held_directory = beets._open_ownership_dir(public)
    real_fsync = beets.os.fsync
    failed = False

    def fail_after_unlink(descriptor):
        nonlocal failed
        if descriptor == parent_fd and not list(parent.glob(".qobuz-merge-*")):
            failed = True
            raise OSError(errno.EIO, "simulated parent fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(beets.os, "fsync", fail_after_unlink)

    try:
        assert (
            beets._remove_exact_empty_merge_dir(
                parent_fd,
                public.name,
                held_directory,
            )
            is False
        )
        assert failed is True
        assert not public.exists()
        assert not list(parent.glob(".qobuz-merge-*"))
    finally:
        os.close(held_directory)
        os.close(parent_fd)


# ── lyrics: retry manifest + atomic writes ────────────────────────────────


class _FakeLyricFLAC:
    def __init__(self, path):
        from mutagen.flac import VCFLACDict

        self.filename = str(path)
        self.tags = VCFLACDict()
        self.save_targets = []

    def save(self, target):
        self.save_targets.append(target)
        Path(target).write_bytes(b"new-audio+tags")


def test_lyric_retry_round_trips_and_clears(tmp_path, monkeypatch):
    rfile = tmp_path / "retry.json"
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
    save_lyric_retry(["/music/a.flac", "/music/b.flac"])
    assert load_lyric_retry() == ["/music/a.flac", "/music/b.flac"]
    # Saving an empty list removes the file rather than leaving an empty manifest.
    save_lyric_retry([])
    assert not rfile.exists()


def test_write_lyrics_saves_atomically_and_clears_legacy_tag(tmp_path):
    from qobuz_librarian.integrations import lyric_fetch

    real = tmp_path / "track.flac"
    real.write_bytes(b"original-audio")

    f = _FakeLyricFLAC(real)
    f.tags["UNSYNCEDLYRICS"] = ["stale plain text"]
    lyric_fetch.write_lyrics(f, "[00:01.00]hello")

    assert f.tags["lyrics"] == ["[00:01.00]hello"]
    assert "unsyncedlyrics" not in f.tags
    # The live file must never be written in place — mutagen saves into a temp
    # copy that is then atomically swapped in, so a crash can't truncate it.
    assert f.save_targets and all(t != f.filename for t in f.save_targets)
    assert real.read_bytes() == b"new-audio+tags"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_write_lyrics_keeps_original_when_replacement_cannot_be_flushed(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch
    from qobuz_librarian.library import backup

    real = tmp_path / "track.flac"
    real.write_bytes(b"original-audio")
    monkeypatch.setattr(backup, "_fsync", lambda _path: False)

    with pytest.raises(OSError, match="original left untouched"):
        lyric_fetch.write_lyrics(_FakeLyricFLAC(real), "lyrics")

    assert real.read_bytes() == b"original-audio"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_write_lyrics_reports_folder_flush_failure(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch
    from qobuz_librarian.library import backup

    real = tmp_path / "track.flac"
    real.write_bytes(b"original-audio")
    outcomes = iter((True, False))
    flushed = []

    def fake_fsync(path):
        flushed.append(Path(path))
        return next(outcomes)

    monkeypatch.setattr(backup, "_fsync", fake_fsync)

    with pytest.raises(OSError, match="may not survive a power loss"):
        lyric_fetch.write_lyrics(_FakeLyricFLAC(real), "lyrics")

    assert real.read_bytes() == b"new-audio+tags"
    assert len(flushed) == 2
    assert flushed[1].parent == Path("/proc/self/fd")


@pytest.mark.parametrize("target_kind", ["flac", "sidecar"])
def test_lyric_replacement_refuses_a_writer_opened_at_exchange(tmp_path, monkeypatch, target_kind):
    import errno
    import os

    from qobuz_librarian.integrations import lyric_fetch

    track = tmp_path / "track.flac"
    track.write_bytes(b"original-audio")
    if target_kind == "flac":
        target = track
        original = track.read_bytes()

        def replace():
            lyric_fetch.write_lyrics(_FakeLyricFLAC(track), "lyrics")
    else:
        target = track.with_suffix(".lrc")
        target.write_text("old lyrics", encoding="utf-8")
        original = target.read_bytes()

        def replace():
            lyric_fetch.write_sidecar(track, "fetched lyrics")

    real_exchange = lyric_fetch._rename_exchange
    writer_refused = False

    def attempt_edit_then_exchange(parent_fd, first, second):
        nonlocal writer_refused
        if not writer_refused and second == target.name:
            try:
                writer_fd = os.open(
                    second,
                    os.O_WRONLY | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                assert exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                writer_refused = True
            else:
                try:
                    before = os.fstat(writer_fd)
                    os.pwrite(writer_fd, b"X" * len(original), 0)
                    os.ftruncate(writer_fd, len(original))
                    os.utime(
                        second,
                        dir_fd=parent_fd,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                        follow_symlinks=False,
                    )
                finally:
                    os.close(writer_fd)
        return real_exchange(parent_fd, first, second)

    monkeypatch.setattr(lyric_fetch, "_rename_exchange", attempt_edit_then_exchange)

    with pytest.raises(OSError, match="changed during atomic replacement"):
        replace()

    assert writer_refused is True
    assert target.read_bytes() == original
    assert not any(path.name.endswith(".tmp") for path in tmp_path.iterdir())


def test_new_sidecar_does_not_adopt_an_edit_made_after_publication(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch

    track = tmp_path / "track.flac"
    track.write_bytes(b"audio")
    sidecar = track.with_suffix(".lrc")
    real_rename = lyric_fetch._rename_noreplace

    def rename_then_edit(first_fd, first, second_fd, second):
        real_rename(first_fd, first, second_fd, second)
        if second == sidecar.name:
            sidecar.write_text("user edit", encoding="utf-8")

    monkeypatch.setattr(lyric_fetch, "_rename_noreplace", rename_then_edit)
    receipt = {}

    with pytest.raises(OSError, match="sidecar changed"):
        lyric_fetch.write_sidecar(track, "fetched lyrics", creation_out=receipt)

    assert sidecar.read_text(encoding="utf-8") == "user edit"
    assert receipt == {}


def test_lyric_fetch_refuses_paths_outside_or_linked_out_of_its_owned_root(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch

    music_root = tmp_path / "music"
    outside = tmp_path / "outside"
    (outside / "Artist" / "Album").mkdir(parents=True)
    music_root.mkdir()
    (music_root / "Artist").symlink_to(outside / "Artist", target_is_directory=True)
    outside_track = outside / "outside.flac"
    linked_track = music_root / "Artist" / "Album" / "linked.flac"
    outside_track.write_bytes(b"outside audio")
    (outside / "Artist" / "Album" / "linked.flac").write_bytes(b"linked outside audio")
    state_path = tmp_path / "lyrics-state.json"
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)

    counts = lyric_fetch.fetch_for_paths(
        [outside_track, linked_track],
        owned_root=music_root,
        state_path=state_path,
        rescan=True,
        workers=1,
        lyrics_format="both",
    )
    indexed = lyric_fetch.index_existing(
        [outside_track, linked_track],
        owned_root=music_root,
        state_path=tmp_path / "lyrics-index-state.json",
        workers=1,
    )

    assert counts == {"unsafe-path": 2}
    assert indexed == {"unsafe-path": 2}
    assert outside_track.read_bytes() == b"outside audio"
    assert (outside / "Artist" / "Album" / "linked.flac").read_bytes() == (b"linked outside audio")
    assert not outside_track.with_suffix(".lrc").exists()
    assert not linked_track.with_suffix(".lrc").exists()


def test_parallel_lyric_cancel_drains_only_the_bounded_active_workers(tmp_path, monkeypatch):
    import threading

    from qobuz_librarian.integrations import lyric_fetch

    paths = [tmp_path / f"{index}.flac" for index in range(8)]
    cancel = threading.Event()
    active = threading.Barrier(2)
    started = []
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(lyric_fetch, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(lyric_fetch, "load_state", lambda *_a, **_k: {})
    monkeypatch.setattr(lyric_fetch, "should_process", lambda *a, **k: True)

    def process_file(path, *_args, **_kwargs):
        started.append(path)
        active.wait(timeout=2)
        cancel.set()
        return "wrote-plain"

    monkeypatch.setattr(lyric_fetch, "process_file", process_file)

    counts = lyric_fetch.fetch_for_paths(
        paths,
        owned_root=tmp_path,
        state_path=tmp_path / "state.json",
        workers=2,
        should_stop=cancel.is_set,
    )

    assert len(started) == 2
    assert counts == {"wrote-plain": 2, "stopped": 1, "stop-total": 8}


def test_lyric_fetch_refuses_a_stale_track_replacement_after_provider_lookup(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch

    track = tmp_path / "track.flac"
    parked = tmp_path / "original.flac"
    track.write_bytes(b"original audio")
    track.with_suffix(".lrc").write_text("", encoding="utf-8")

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"title": ["Track"], "artist": ["Artist"]}
            self.info = type("Info", (), {"length": 180})()

        def save(self, _path):
            raise AssertionError("stale replacement reached the tag writer")

    def replace_during_lookup(*_args, **_kwargs):
        track.rename(parked)
        track.write_bytes(b"unrelated replacement")
        return "[00:01.00]words", "provider", "synced", 1

    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)
    monkeypatch.setattr(lyric_fetch, "search_lyrics", replace_during_lookup)

    state = {}
    outcome = lyric_fetch.process_file(
        track,
        state,
        ["provider"],
        False,
        lyric_fetch.logging.getLogger("lyrics-test"),
        tmp_path,
        lyrics_format="both",
    )

    assert outcome == "write-error"
    assert parked.read_bytes() == b"original audio"
    assert track.read_bytes() == b"unrelated replacement"
    assert track.with_suffix(".lrc").read_text(encoding="utf-8") == ""
    assert state[str(track)].status == "transient"


@pytest.mark.parametrize(
    ("lyrics_format", "embedded", "sidecar", "missing_format"),
    [
        ("embed", None, "[00:01.00]sidecar words", "embed"),
        ("sidecar", "[00:01.00]embedded words", None, "sidecar"),
        ("both", None, "[00:01.00]sidecar words", "embed"),
    ],
)
def test_lyric_completion_fills_only_the_missing_requested_representation(
    tmp_path, monkeypatch, lyrics_format, embedded, sidecar, missing_format
):
    from qobuz_librarian.integrations import lyric_fetch

    track = tmp_path / "track.flac"
    track.write_bytes(b"audio")
    if sidecar is not None:
        track.with_suffix(".lrc").write_text(sidecar, encoding="utf-8")

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"title": ["Track"], "artist": ["Artist"]}
            if embedded is not None:
                self.tags["lyrics"] = [embedded]
            self.info = type("Info", (), {"length": 180})()

    writes = []

    def write(path, _f, content, fmt, *, binding):
        writes.append((path, content, fmt))
        return binding.identity

    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)
    monkeypatch.setattr(
        lyric_fetch,
        "search_lyrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an existing representation must not call a provider")
        ),
    )
    monkeypatch.setattr(lyric_fetch, "write_output", write)

    outcome = lyric_fetch.process_file(
        track,
        {},
        ["provider"],
        False,
        lyric_fetch.logging.getLogger("lyrics-test"),
        tmp_path,
        lyrics_format=lyrics_format,
    )

    expected_content = embedded if embedded is not None else sidecar
    assert outcome == "wrote-synced"
    assert writes == [(track, expected_content, missing_format)]


def test_partial_both_write_is_immediately_retryable_without_refetching(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch

    track = tmp_path / "track.flac"
    track.write_bytes(b"original audio")
    embedded_present = False
    provider_calls = 0
    write_formats = []

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"title": ["Track"], "artist": ["Artist"]}
            if embedded_present:
                self.tags["lyrics"] = ["[00:01.00]words"]
            self.info = type("Info", (), {"length": 180})()

    def search(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return "[00:01.00]words", "provider", "synced", 1

    def write(path, _f, content, fmt, *, binding):
        nonlocal embedded_present
        write_formats.append(fmt)
        if fmt == "both":
            embedded_present = True
            replacement = path.with_name("replacement.flac")
            replacement.write_bytes(b"audio with embedded lyrics")
            replacement.replace(path)
            raise OSError("sidecar publication failed")
        assert fmt == "sidecar"
        path.with_suffix(".lrc").write_text(content, encoding="utf-8")
        return binding.identity

    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)
    monkeypatch.setattr(lyric_fetch, "search_lyrics", search)
    monkeypatch.setattr(lyric_fetch, "write_output", write)
    state = {}

    first = lyric_fetch.process_file(
        track,
        state,
        ["provider"],
        False,
        lyric_fetch.logging.getLogger("lyrics-test"),
        tmp_path,
        lyrics_format="both",
    )
    assert first == "write-error"
    assert state[str(track)].status == "transient"
    assert (
        lyric_fetch.should_process(
            track,
            state[str(track)],
            False,
            mtime=state[str(track)].mtime,
            size=state[str(track)].size,
        )
        is True
    )

    second = lyric_fetch.process_file(
        track,
        state,
        ["provider"],
        False,
        lyric_fetch.logging.getLogger("lyrics-test"),
        tmp_path,
        lyrics_format="both",
    )

    assert second == "wrote-synced"
    assert provider_calls == 1
    assert write_formats == ["both", "sidecar"]
    assert track.with_suffix(".lrc").read_text(encoding="utf-8") == ("[00:01.00]words")


def test_post_import_sidecar_reports_a_better_existing_file_as_kept(tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch, lyrics

    album = tmp_path / "Album"
    album.mkdir()
    track = album / "01.flac"
    track.write_bytes(b"audio")
    sidecar = track.with_suffix(".lrc")
    sidecar.write_text("[00:01.00]better lyrics", encoding="utf-8")

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"lyrics": ["plain embedded lyrics"]}

    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "sidecar")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)

    def fail_strip(*_args):
        raise OSError("strip failed")

    monkeypatch.setattr(lyric_fetch, "save_flac_tags", fail_strip)
    caplog.set_level("INFO", logger="qobuz_librarian")

    lyrics.write_post_import_sidecars([album])

    assert sidecar.read_text(encoding="utf-8") == "[00:01.00]better lyrics"
    assert "kept 1 better existing .lrc sidecar" in caplog.text
    assert "wrote 1 .lrc sidecar" not in caplog.text
    assert "embedded tag removal confirmed for 0 of 1" in caplog.text


def test_post_import_sidecar_refuses_a_symlinked_library_component(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch, lyrics

    music_root = tmp_path / "music"
    outside = tmp_path / "outside"
    album = outside / "Album"
    music_root.mkdir()
    album.mkdir(parents=True)
    (music_root / "Artist").symlink_to(outside, target_is_directory=True)
    track = album / "01.flac"
    track.write_bytes(b"audio")

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"lyrics": ["plain embedded lyrics"]}

    monkeypatch.setattr(cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "both")
    monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)

    lyrics.write_post_import_sidecars(
        [
            music_root / "Artist" / "Album",
        ]
    )

    assert not track.with_suffix(".lrc").exists()


def test_post_import_sidecar_keeps_embedded_lyrics_if_public_sidecar_changes(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch, lyrics

    album = tmp_path / "Album"
    album.mkdir()
    track = album / "01.flac"
    track.write_bytes(b"audio")
    sidecar = track.with_suffix(".lrc")
    sidecar.write_text("[00:01.00]better lyrics", encoding="utf-8")
    opened = []

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"lyrics": ["plain embedded lyrics"]}
            opened.append(self)

    class FakeExclusion:
        def intact(self):
            return True

        def close(self):
            pass

    def replace_sidecar_before_commit(_descriptor):
        sidecar.rename(album / "parked.lrc")
        sidecar.write_text("", encoding="utf-8")
        return FakeExclusion()

    save_calls = []
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "sidecar")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)
    monkeypatch.setattr(lyrics, "acquire_inode_write_exclusion", replace_sidecar_before_commit)
    monkeypatch.setattr(
        lyric_fetch,
        "save_flac_tags",
        lambda *_args, **_kwargs: save_calls.append(True),
    )

    lyrics.write_post_import_sidecars([album])

    assert save_calls == []
    assert opened[0].tags["lyrics"] == ["plain embedded lyrics"]
    assert sidecar.read_bytes() == b""


def test_update_state_reloads_inside_the_lock(tmp_path):
    # update_state must read the file fresh inside the lock and write the
    # mutated result — so a value another process appended before the lock was
    # acquired survives the mutation instead of being clobbered by a stale
    # in-memory snapshot.
    from qobuz_librarian.integrations import lyric_fetch

    sf = tmp_path / "state.json"
    lyric_fetch.save_state({"a": lyric_fetch.TrackState(status="synced")}, sf)
    # Simulate a concurrent writer landing a new key after the caller's last read
    # but before the prune runs.
    cur = lyric_fetch.load_state(sf)
    cur["b"] = lyric_fetch.TrackState(status="transient")
    lyric_fetch.save_state(cur, sf)

    lyric_fetch.update_state(lambda s: s.pop("a", None), sf)
    out = lyric_fetch.load_state(sf)
    assert "a" not in out  # the mutation applied
    assert "b" in out  # the concurrent writer's key was not clobbered


def test_library_lyrics_dry_run_keeps_state_file_byte_for_byte(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch
    from qobuz_librarian.library import lyrics

    state_path = tmp_path / "lyrics-state.json"
    missing = tmp_path / "moved.flac"
    lyric_fetch.save_state(
        {str(missing): lyric_fetch.TrackState(status="transient")},
        state_path,
    )
    before = state_path.read_bytes()

    track = tmp_path / "track.flac"
    track.write_bytes(b"audio")
    stat = track.stat()
    monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", state_path)
    monkeypatch.setattr(
        lyrics,
        "iter_library_flacs",
        lambda **_kwargs: iter([(track, stat.st_mtime, stat.st_size)]),
    )
    indexed = []
    monkeypatch.setattr(
        lyric_fetch,
        "index_existing",
        lambda *_args, **_kwargs: indexed.append(True),
    )
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(
        lyric_fetch,
        "process_file",
        lambda *_args, **_kwargs: "dry:wrote-synced",
    )

    lyrics.run_library_lyrics(dry_run=True)

    assert indexed == []
    assert state_path.read_bytes() == before


# ── beets: _beets_direct behaviour ─────────────────────────────────────────


def test_beets_runtime_check_rejects_an_unrelated_executable(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    unrelated = shutil.which("true")
    assert unrelated is not None
    monkeypatch.setattr(cfg, "BEETS_PYTHON", unrelated)

    assert beets.beets_runtime_path() is None


def test_beets_direct_preflights_a_new_database_before_import(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    config_dir = tmp_path / "beets"
    config_dir.mkdir()
    database = config_dir / "library.db"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)

    inspected = []
    imported = []

    def reject_filesystem(anchor, *_args, **_kwargs):
        inspected.append(anchor["descriptor"])
        raise OSError("unsupported database filesystem")

    monkeypatch.setattr(beets, "inspect_sqlite_source", reject_filesystem)
    monkeypatch.setattr(
        beets,
        "_beets_direct_guarded",
        lambda *_args, **_kwargs: imported.append(True) or (True, "ok"),
    )

    real_connect = beets.sqlite3.connect

    def fail_bootstrap(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("file:/proc/self/fd/"):
            raise beets.sqlite3.DatabaseError("incomplete bootstrap")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(beets.sqlite3, "connect", fail_bootstrap)
    assert beets._beets_direct(None, lambda: None, [str(tmp_path)]) == (False, "error")
    assert not database.exists()
    assert not list(config_dir.glob(".qobuz-beets-bootstrap-*"))
    assert imported == []

    monkeypatch.setattr(beets.sqlite3, "connect", real_connect)
    assert beets._beets_direct(None, lambda: None, [str(tmp_path)]) == (False, "error")
    assert inspected and inspected[0] is not None
    assert imported == []
    assert database.read_bytes().startswith(b"SQLite format 3\0")


def test_beets_direct_detects_silent_skip_by_unmoved_audio(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    captured_env = {}
    captured_args = []

    class _Proc:
        def __init__(self, lines=(), on_wait=None):
            self.stdout = iter(lines)
            self.returncode = 0
            self._on_wait = on_wait

        def wait(self, timeout=None):
            if self._on_wait:
                self._on_wait()
            return 0

        def kill(self):
            pass

    def _popen_returning(proc):
        def _popen(*args, **kwargs):
            captured_args.append(args[0])
            captured_env.update(kwargs.get("env") or {})
            return proc

        return _popen

    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
    album = tmp_path / "Artist - Album"
    album.mkdir()
    track = album / "01.flac"
    track.write_bytes(b"flac-bytes")

    # beets moves the staged track into the library (here, deletes it) and
    # prints a per-item "Skipping." for a duplicate. The album still imported,
    # so the skip line must not flip the result to failure.
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc(["Skipping.\n"], track.unlink)))
    runtime = beets._checked_beets_runtime(sys.executable)
    assert runtime is not None
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is True and kind == "ok"
    assert captured_args[-1][:4] == [
        sys.executable,
        "-I",
        str(beets._managed_beets_entrypoint()),
        "--run-beets",
    ]
    assert captured_env.get("BEETSDIR") == str(cfg.BEETS_CONFIG_DIR)

    # beets exits 0 but moves nothing out of staging — the real silent skip.
    track.write_bytes(b"flac-bytes")
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc()))
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is False and kind == "error"

    # A runtime replaced after preflight must not fall back to an unchecked
    # PATH launcher.
    invalid_runtime = beets._BeetsRuntime(
        runtime.python,
        (*runtime.link_identity[:-1], runtime.link_identity[-1] + 1),
        runtime.target_identity,
    )
    spawned_before = len(captured_args)
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=invalid_runtime,
    )
    assert ok is False and kind == "error"
    assert len(captured_args) == spawned_before


def test_beets_pruning_stays_bound_to_the_captured_staging_roots(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    staging = tmp_path / "staging"
    stable_run = staging / ".qobuz-run-111111111111111111111111"
    swapped_run = staging / ".qobuz-run-222222222222222222222222"
    stable_album = stable_run / "Artist" / "Album"
    swapped_album = swapped_run / "Artist" / "Album"
    for album in (stable_album, swapped_album):
        disc = album / "Disc 2"
        disc.mkdir(parents=True)
        (disc / "01.flac").write_bytes(b"track")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    capture = beets._capture_beets_prune_roots(
        [str(stable_album), str(swapped_album)]
    )
    assert capture is not None
    (stable_album / "Disc 2" / "01.flac").unlink()
    (swapped_album / "Disc 2" / "01.flac").unlink()
    parked = staging / "parked-original"
    swapped_run.rename(parked)
    outside = tmp_path / "outside"
    (outside / "Artist" / "Album" / "Disc 2").mkdir(parents=True)
    swapped_run.symlink_to(outside, target_is_directory=True)

    try:
        beets._prune_captured_beets_directories(capture)
    finally:
        beets._close_beets_prune_capture(capture)

    assert stable_run.is_dir()
    assert list(stable_run.iterdir()) == []
    assert swapped_run.is_symlink()
    assert (outside / "Artist" / "Album" / "Disc 2").is_dir()
    assert (parked / "Artist" / "Album" / "Disc 2").is_dir()


def test_forget_beets_entries_uses_the_verified_runtime(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    runtime = beets._checked_beets_runtime(sys.executable)
    assert runtime is not None
    monkeypatch.setattr(beets, "_resolve_beets_runtime", lambda: runtime)
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", Path("beets"))
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("beets/musiclibrary.db"))

    class _Exclusion:
        def acquire(self, _parent):
            pass

        def release(self):
            return None

    monkeypatch.setattr(beets, "_SQLiteDatabaseExclusion", _Exclusion)
    monkeypatch.setattr(
        beets,
        "_open_beets_database_anchor",
        lambda: {"parent_chain": [object()]},
    )
    monkeypatch.setattr(beets, "_preflight_beets_database_anchor", lambda _anchor: None)
    monkeypatch.setattr(beets, "_close_beets_database_anchor", lambda _anchor: None)

    calls = []

    def run_capture(args, *, env, before_spawn, **_options):
        before_spawn()
        calls.append((args, env))
        output = "17\n" if "ls" in args else ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(beets, "_run_owned_beets_capture", run_capture)

    assert beets.forget_beets_entries(["/music/Artist/Album/01.flac"]) == 1
    assert len(calls) == 2
    expected_prefix = [
        sys.executable,
        "-I",
        str(beets._managed_beets_entrypoint()),
        "--run-beets",
        "-l",
        str(Path("beets/musiclibrary.db").absolute()),
    ]
    assert calls[0][0][:6] == expected_prefix
    assert calls[1][0][:6] == expected_prefix
    assert calls[0][1]["BEETSDIR"] == str(Path("beets").absolute())


def _wait_for_process_pid(path):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            return int(path.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
    return None


def _process_state(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError:
        return None
    closing = raw.rfind(b")")
    return raw[closing + 1 :].split()[0].decode()


def _stop_process(pid):
    if pid is None:
        return
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if _process_state(pid) in {None, "Z", "X"}:
            return
        time.sleep(0.01)


def test_beets_direct_does_not_wait_for_detached_stdout_writer(monkeypatch, tmp_path):
    import errno

    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library.sqlite_atomic import (
        _SQLiteDatabaseExclusion,
    )

    pid_path = tmp_path / "detached.pid"
    entrypoint = tmp_path / "beets_probe.py"
    entrypoint.write_text(
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    with open(os.environ['QOBUZ_TEST_PID'], 'w') as handle:\n"
        "        handle.write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "deadline = time.monotonic() + 1\n"
        "while not os.path.exists(os.environ['QOBUZ_TEST_PID']):\n"
        "    if time.monotonic() >= deadline:\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "os._exit(0)\n"
    )
    monkeypatch.setenv("QOBUZ_TEST_PID", str(pid_path))
    monkeypatch.setattr(beets, "_managed_beets_entrypoint", lambda: entrypoint)
    runtime = beets._checked_beets_runtime(sys.executable)
    assert runtime is not None

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    exclusion = _SQLiteDatabaseExclusion()
    exclusion.acquire(directory_fd)
    outcome = {}
    done = threading.Event()

    def run_import():
        started = time.monotonic()
        try:
            beets._beets_direct_guarded(
                None,
                [str(tmp_path)],
                database_exclusion=exclusion,
                beets_runtime=runtime,
            )
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started
            done.set()

    worker = threading.Thread(target=run_import, daemon=True)
    worker.start()
    pid = _wait_for_process_pid(pid_path)
    completed_without_cleanup = done.wait(1)
    try:
        assert completed_without_cleanup
        assert isinstance(outcome.get("error"), OSError)
        assert outcome["error"].errno == errno.EBUSY
        assert outcome["elapsed"] < 1
    finally:
        _stop_process(pid)
        worker.join(1)
        exclusion.release()
        os.close(directory_fd)


def test_beets_capture_retires_same_group_descendant_promptly(tmp_path):
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library.sqlite_atomic import (
        _SQLiteDatabaseExclusion,
    )

    pid_path = tmp_path / "same-group.pid"
    child_code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    with open(sys.argv[1], 'w') as handle:\n"
        "        handle.write(str(os.getpid()))\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "deadline = time.monotonic() + 1\n"
        "while not os.path.exists(sys.argv[1]):\n"
        "    if time.monotonic() >= deadline:\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "os._exit(0)\n"
    )
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    exclusion = _SQLiteDatabaseExclusion()
    exclusion.acquire(directory_fd)
    outcome = {}
    done = threading.Event()

    def run_capture():
        started = time.monotonic()
        try:
            outcome["result"] = beets._run_owned_beets_capture(
                [sys.executable, "-B", "-c", child_code, str(pid_path)],
                env=os.environ.copy(),
                timeout=1,
                database_exclusion=exclusion,
            )
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started
            done.set()

    worker = threading.Thread(target=run_capture, daemon=True)
    worker.start()
    pid = _wait_for_process_pid(pid_path)
    completed_without_cleanup = done.wait(1)
    state = _process_state(pid) if pid is not None else None
    try:
        assert completed_without_cleanup
        assert outcome.get("error") is None
        assert outcome["result"].returncode == 0
        assert outcome["elapsed"] < 1
        assert state in {None, "Z", "X"}
    finally:
        _stop_process(pid)
        worker.join(1)
        assert exclusion.release() is None

    replacement = _SQLiteDatabaseExclusion()
    try:
        replacement.acquire(directory_fd)
    finally:
        assert replacement.release() is None
        os.close(directory_fd)


def test_managed_carrier_retirement_is_exact_and_idempotent(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    config_dir = tmp_path / "beets"
    music = tmp_path / "music"
    config_dir.mkdir()
    config_dir.chmod(stat.S_IMODE(config_dir.stat().st_mode) | stat.S_ISGID)
    music.mkdir()
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", config_dir / "library.db")
    carrier = config_dir / f".qobuz-managed-beets-{'c' * 32}.jsonl"
    descriptor = os.open(carrier, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        source = {
            "slot": "qobuz:1",
            "path": str(tmp_path / "staging" / "01.flac"),
            "identity": [1, 2, 3, 4, 5, 6],
        }
        root_identity = beets._ownership_identity(os.stat(music, follow_symlinks=False))
        generation_zero = {
            "version": 2,
            "generation": 0,
            "previous_hash": None,
            "nonce": "d" * 64,
            "owner": owner,
            "root": str(music),
            "root_identity": root_identity,
            "sealed": False,
            "intent": [source],
            "mappings": [],
            "cleanup_directories": [],
        }
        previous_hash = beets._write_managed_snapshot(descriptor, generation_zero)
        final_hash = beets._write_managed_snapshot(
            descriptor,
            {
                **generation_zero,
                "generation": 1,
                "previous_hash": previous_hash,
                "sealed": True,
                "mappings": [
                    {
                        "slot": source["slot"],
                        "source": {
                            "path": source["path"],
                            "identity": source["identity"],
                        },
                        "destination": {
                            "path": "Artist/Album/01.flac",
                            "identity": {
                                "device": 7,
                                "inode": 8,
                                "size": 9,
                                "modified_ns": 10,
                                "changed_ns": 11,
                            },
                        },
                    }
                ],
            },
        )
    finally:
        os.close(descriptor)
    carrier_stat = carrier.stat()
    parent_stat = config_dir.stat()
    reference = {
        "version": 1,
        "path": str(carrier),
        "device": int(carrier_stat.st_dev),
        "inode": int(carrier_stat.st_ino),
        "parent_device": int(parent_stat.st_dev),
        "parent_inode": int(parent_stat.st_ino),
        "nonce": "d" * 64,
        "owner": owner,
    }
    quarantine = f".qobuz-retire-managed-beets-{'e' * 64}"

    hardlink = config_dir / "unexpected-link.jsonl"
    os.link(carrier, hardlink)
    refused = beets.retire_managed_carrier(reference, owner, final_hash, quarantine)
    assert refused.outcome is beets.ManagedCarrierRetirementOutcome.REFUSED
    assert carrier.exists() and hardlink.exists()
    hardlink.unlink()

    retired = beets.retire_managed_carrier(reference, owner, final_hash, quarantine)
    assert retired.outcome is beets.ManagedCarrierRetirementOutcome.RETIRED
    assert not carrier.exists()
    assert not (config_dir / quarantine).exists()

    repeated = beets.retire_managed_carrier(reference, owner, final_hash, quarantine)
    assert repeated.outcome is (beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT)


def test_beets_direct_rejects_partial_exact_track_consumption(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.integrations.staging import capture_file

    class _Proc:
        def __init__(self, moved):
            self.stdout = iter(())
            self.returncode = 0
            self._moved = moved

        def wait(self, timeout=None):
            self._moved.unlink()
            return 0

        def kill(self):
            pass

    staging = tmp_path / "staging"
    album = staging / "Artist - Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _Proc(first))

    intended = (capture_file(first), capture_file(second))
    assert all(intended)
    runtime = beets._checked_beets_runtime(sys.executable)
    assert runtime is not None
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        intended_audio=intended,
        beets_runtime=runtime,
    )

    assert ok is False and kind == "error"
    assert not first.exists()
    assert second.read_bytes() == b"second"


# ── beets: staging tag prep (quarantine, never delete) ────────────────────


def test_prepare_staging_tags_sets_aside_untagged_keeps_tagged(tmp_path, monkeypatch, _need_ffmpeg):
    # A cancelled/crashed rip leaves untagged FLACs beets would file under
    # '/_/'. They're moved out of the import set — but set aside, never deleted.
    from mutagen.flac import FLAC

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.integrations.staging import capture_file

    staging = tmp_path / "staging"
    data = tmp_path / "data"
    staging.mkdir()
    data.mkdir()
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", data)

    tagged = staging / "Real Artist" / "Real Album" / "01 - Good.flac"
    untagged = staging / "Partial" / "00 -.flac"
    _make_silent_flac(tagged)
    _make_silent_flac(untagged)
    f = FLAC(str(tagged))
    f["albumartist"], f["album"], f["title"] = ["Real Artist"], ["Real Album"], ["Good"]
    f.save()
    broken = staging / "Broken" / "x.flac"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a flac at all")

    moved = beets._prepare_staging_tags()
    assert tagged.exists()
    assert not untagged.exists() and untagged in moved
    assert not broken.exists() and broken in moved
    assert len(list((staging / cfg.BEETS_RETRY_DIR).rglob("*.flac"))) == 2

    clean = capture_file(tagged)
    assert clean is not None
    binding = [
        {
            "slot": "qobuz:1",
            "path": str(clean.path),
            "identity": list(clean.identity),
        }
    ]
    intent = beets._prepare_staging_tags(roots=[tagged.parent], managed_bindings=binding)
    assert intent[0]["identity"] == list(clean.identity)

    f = FLAC(str(tagged))
    f["album"] = ["  Real Album  "]
    f.save()
    dirty = capture_file(tagged)
    assert dirty is not None
    binding[0]["identity"] = list(dirty.identity)
    with pytest.raises(OSError, match="requires a tag-clean rewrite"):
        beets._prepare_staging_tags(roots=[tagged.parent], managed_bindings=binding)
    assert capture_file(tagged, expected=dirty.identity) is not None
    assert FLAC(str(tagged))["album"] == ["  Real Album  "]

    rewritten = beets.prepare_managed_staging_tags(
        [tagged.parent],
        binding,
        authority_check=lambda: None,
    )
    assert rewritten[0]["identity"] != list(dirty.identity)
    assert FLAC(str(tagged))["album"] == ["Real Album"]

    f = FLAC(str(tagged))
    f["album"] = ["  Real Album  "]
    f.save()
    dirty = capture_file(tagged)
    assert dirty is not None
    binding[0]["identity"] = list(dirty.identity)
    authority_live = [True]
    commit_checks = []

    def authority_check():
        if not authority_live[0]:
            raise RuntimeError("lease lost")

    def stop_at_commit(_tags, _path, *, commit_guard, **_kwargs):
        authority_live[0] = False
        try:
            allowed = commit_guard()
        except RuntimeError:
            allowed = False
        commit_checks.append(allowed)
        raise OSError("commit refused")

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "save_flac_tags", stop_at_commit)
    with pytest.raises(OSError, match="tag-clean rewrite failed"):
        beets.prepare_managed_staging_tags(
            [tagged.parent],
            binding,
            authority_check=authority_check,
        )
    assert commit_checks == [False]
    assert capture_file(tagged, expected=dirty.identity) is not None


def test_prepare_staging_tags_keeps_original_when_flush_fails(tmp_path, monkeypatch):
    from mutagen import flac

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library import backup

    staging = tmp_path / "staging"
    track = staging / "Artist" / "Album" / "01.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"original")

    class DirtyTags(dict):
        def save(self, target):
            Path(target).write_bytes(b"rewritten")

    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(beets, "HAVE_MUTAGEN", True)
    monkeypatch.setattr(
        flac,
        "FLAC",
        lambda _path: DirtyTags(album=[" Album "], albumartist=["Artist"], title=["Track"]),
    )
    monkeypatch.setattr(backup, "_fsync", lambda _path: False)

    beets._prepare_staging_tags()

    assert track.read_bytes() == b"original"
    assert list(track.parent.glob("*.tmp")) == []


# ── beets: import override pins non-destructive duplicate handling ─────────


def test_import_override_pins_duplicate_action_merge(monkeypatch):
    # OUR importer must pin duplicate_action: merge regardless of the user's
    # config. `remove` would delete the existing library album on a collision
    # (irreversible); `skip` would silently import nothing for a per-track
    # gap-fill (which relies on beets MERGING the missing tracks into the
    # existing folder). merge is non-destructive and what gap-fill / the
    # consolidation re-import need.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    conf = yaml.safe_load(beets._build_import_override_yaml())
    assert conf["import"]["duplicate_action"] == "merge"
    assert conf["plugins"][-1] == "qobuz_art_guard"
    assert conf["pluginpath"][0] == str(Path(beets.__file__).parent / "beets_plugins")
    # Streamrip already wrote authoritative Qobuz tags, so autotag must be pinned
    # off — otherwise a user's autotag:yes pushes downloads through MusicBrainz
    # matching and strands unmatched albums in staging under quiet mode.
    assert conf["import"]["autotag"] is False


def _duplicate_album_fixture(tmp_path, *, conflicting_attribute=False):
    import sqlite3

    music = tmp_path / "music" / "Artist" / "Album"
    music.mkdir(parents=True)
    first = music / "01.flac"
    second = music / "02.flac"
    cover = music / "cover.jpg"
    first.write_bytes(b"first audio")
    second.write_bytes(b"second audio")
    cover.write_bytes(b"artwork")
    database = tmp_path / "library.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE albums (
                added REAL, album TEXT, albumartist TEXT, artpath BLOB,
                custom_field TEXT, id INTEGER PRIMARY KEY
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB,
                title TEXT, mtime REAL
            );
            CREATE TABLE album_attributes (
                id INTEGER PRIMARY KEY, entity_id INTEGER,
                key TEXT, value TEXT
            );
            CREATE TABLE item_attributes (
                id INTEGER PRIMARY KEY, entity_id INTEGER,
                key TEXT, value TEXT
            );
        """)
        artpath = os.fsencode(cover)
        connection.executemany(
            "INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?)",
            [
                (10.0, "Album", "Artist", artpath, "opaque", 1),
                (20.0, "Album", "Artist", None, "opaque", 2),
            ],
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?)",
            [
                (11, 1, os.fsencode(first), "First", 101.25),
                (12, 2, os.fsencode(second), "Second", 202.5),
            ],
        )
        connection.executemany(
            "INSERT INTO album_attributes VALUES (?, ?, ?, ?)",
            [
                (21, 1, "qobuz_id", "123"),
                (22, 1, "source", "qobuz"),
                (23, 2, "qobuz_id", "123"),
                (
                    24,
                    2,
                    "loser_only" if conflicting_attribute else "source",
                    "must survive" if conflicting_attribute else "qobuz",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO item_attributes VALUES (?, ?, ?, ?)",
            [(31, 11, "token", "one"), (32, 12, "token", "two")],
        )
        connection.commit()
    finally:
        connection.close()
    return database, (first, second, cover)


def _duplicate_album_db_snapshot(database):
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        return tuple(
            (table, connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall())
            for table in ("albums", "items", "album_attributes", "item_attributes")
        )
    finally:
        connection.close()


def _configure_consolidation(monkeypatch, tmp_path, database):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 30)
    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)


def test_duplicate_album_fold_preserves_files_and_all_nonstructural_data(tmp_path, monkeypatch):
    import hashlib
    import sqlite3

    from qobuz_librarian.integrations import beets

    database, files = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    relative_item = os.path.join("Artist", "Album", "01.flac")
    assert beets._consolidation_item_dir(relative_item) == str(files[0].parent)
    assert (
        beets._consolidation_path_is_protected(
            beets._consolidation_item_dir(relative_item),
            {files[0].parent},
        )
        is True
    )
    before_files = [
        (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).digest(),
        )
        for path in files
    ]
    before = _duplicate_album_db_snapshot(database)

    beets._consolidate_duplicate_albums()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT id, album_id, path, title, mtime FROM items ORDER BY id"
        ).fetchall() == [
            (11, 1, os.fsencode(files[0]), "First", 101.25),
            (12, 1, os.fsencode(files[1]), "Second", 202.5),
        ]
        assert connection.execute("SELECT * FROM albums ORDER BY id").fetchall() == [
            before[0][1][0]
        ]
        assert (
            connection.execute("SELECT * FROM album_attributes ORDER BY id").fetchall()
            == before[2][1][:2]
        )
        assert (
            connection.execute("SELECT * FROM item_attributes ORDER BY id").fetchall()
            == before[3][1]
        )
    finally:
        connection.close()
    assert before_files == [
        (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).digest(),
        )
        for path in files
    ]


def test_duplicate_album_fold_requires_one_confined_album_scope(tmp_path, monkeypatch):
    import sqlite3

    from qobuz_librarian.integrations import beets

    database, _ = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    music = tmp_path / "music"
    outside = tmp_path / "outside" / "Album"
    outside.mkdir(parents=True)

    album_rows = [
        (30.0, "Outside", "Artist", None, "opaque", 3),
        (40.0, "Outside", "Artist", None, "opaque", 4),
        (50.0, "Mixed", "Artist", None, "opaque", 5),
        (60.0, "Mixed", "Artist", None, "opaque", 6),
        (70.0, "Multi", "Artist", None, "opaque", 7),
        (80.0, "Multi", "Artist", None, "opaque", 8),
    ]
    item_rows = [
        (31, 3, os.fsencode(outside / "01.flac"), "Outside 1", 1.0),
        (41, 4, os.fsencode(outside / "02.flac"), "Outside 2", 1.0),
        (51, 5, os.fsencode(music / "Artist/Mixed/Shared/01.flac"), "M1", 1.0),
        (52, 5, os.fsencode(music / "Artist/Mixed/Left/02.flac"), "M2", 1.0),
        (61, 6, os.fsencode(music / "Artist/Mixed/Shared/03.flac"), "M3", 1.0),
        (62, 6, os.fsencode(music / "Artist/Mixed/Right/04.flac"), "M4", 1.0),
        (71, 7, os.fsencode("Artist/Multi/Disc 1/01.flac"), "D1", 1.0),
        (81, 8, os.fsencode("Artist/Multi/Disc 2/01.flac"), "D2", 1.0),
    ]
    connection = sqlite3.connect(database)
    try:
        connection.executemany("INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?)", album_rows)
        connection.executemany("INSERT INTO items VALUES (?, ?, ?, ?, ?)", item_rows)
        connection.commit()
    finally:
        connection.close()

    beets._consolidate_duplicate_albums()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id FROM albums ORDER BY id").fetchall() == [
            (1,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
        ]
        assert connection.execute(
            "SELECT id, album_id FROM items WHERE id >= 31 ORDER BY id"
        ).fetchall() == [
            (31, 3),
            (41, 4),
            (51, 5),
            (52, 5),
            (61, 6),
            (62, 6),
            (71, 7),
            (81, 7),
        ]
    finally:
        connection.close()


def test_duplicate_album_fold_refuses_unrepresented_metadata_and_unknown_schema(
    tmp_path, monkeypatch
):
    import sqlite3

    from qobuz_librarian.integrations import beets

    database, _ = _duplicate_album_fixture(tmp_path, conflicting_attribute=True)
    _configure_consolidation(monkeypatch, tmp_path, database)
    recovery = beets._untracked_reimport_file()
    recovery.parent.mkdir(parents=True)
    recovery.write_text("/music/legacy-album\n", encoding="utf-8")
    before = _duplicate_album_db_snapshot(database)

    beets._consolidate_duplicate_albums()

    assert _duplicate_album_db_snapshot(database) == before
    assert recovery.read_text(encoding="utf-8") == "/music/legacy-album\n"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE album_attributes SET key = ?, value = ? WHERE id = ?",
            ("source", "qobuz", 24),
        )
        connection.execute("CREATE TABLE plugin_album_notes (album_id INTEGER, note TEXT)")
        connection.execute(
            "INSERT INTO plugin_album_notes VALUES (?, ?)",
            (2, "must survive"),
        )
    unknown_schema_before = _duplicate_album_db_snapshot(database)

    beets._consolidate_duplicate_albums()

    assert _duplicate_album_db_snapshot(database) == unknown_schema_before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT album_id, note FROM plugin_album_notes").fetchall() == [
            (2, "must survive")
        ]


def test_duplicate_album_fold_rolls_back_an_interrupted_transaction(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import beets

    database, _ = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    before = _duplicate_album_db_snapshot(database)
    fold = beets._fold_duplicate_album_group

    def interrupt_after_mutation(*args, **kwargs):
        assert fold(*args, **kwargs) is True
        raise KeyboardInterrupt

    monkeypatch.setattr(beets, "_fold_duplicate_album_group", interrupt_after_mutation)

    with pytest.raises(KeyboardInterrupt):
        beets._consolidate_duplicate_albums()

    assert _duplicate_album_db_snapshot(database) == before


def test_ownership_hook_preserves_plugins_and_loads_last(monkeypatch):
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/library.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")

    conf = yaml.safe_load(
        beets._build_import_override_yaml(
            {
                "plugins": ["fetchart", "inline", "permissions"],
                "plugin_paths": ["/user/beets-plugins"],
                "disabled": [],
                "musicbrainz_enabled": None,
            },
            ownership_enabled=True,
        )
    )

    assert conf["plugins"] == [
        "fetchart",
        "inline",
        "permissions",
        "qobuz_art_guard",
        "qobuz_ownership",
    ]
    assert conf["pluginpath"][0] == str(Path(beets.__file__).parent / "beets_plugins")
    assert conf["pluginpath"][1:] == ["/user/beets-plugins"]
    assert conf["disabled_plugins"] == []


def _load_art_guard_for_test(monkeypatch, loaded_plugins):
    import importlib.util
    import sys
    import types

    class FakeLog:
        def warning(self, *_args, **_kwargs):
            pass

    class FakeBeetsPlugin:
        def __init__(self):
            self.name = "qobuz_art_guard"
            self._log = FakeLog()

        def register_listener(self, *_args, **_kwargs):
            pass

    plugins_module = types.ModuleType("beets.plugins")
    plugins_module.BeetsPlugin = FakeBeetsPlugin
    plugins_module.find_plugins = lambda: loaded_plugins
    plugins_module.send = lambda *_args, **_kwargs: []
    beets_module = types.ModuleType("beets")
    beets_module.plugins = plugins_module
    monkeypatch.setitem(sys.modules, "beets", beets_module)
    monkeypatch.setitem(sys.modules, "beets.plugins", plugins_module)

    from qobuz_librarian.integrations import beets

    plugin_path = Path(beets.__file__).parent / "beets_plugins" / "qobuz_art_guard.py"
    spec = importlib.util.spec_from_file_location("_qobuz_art_guard_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _art_guard_task(root, staging, album_name):
    import types

    destination_dir = root / "Artist" / album_name
    candidate = staging / f"{album_name}.jpg"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"new artwork")

    class Album:
        albumartist = "Artist"
        album = album_name
        artpath = None
        stored = 0

        def art_destination(self, _candidate, *, item_dir):
            return os.path.join(item_dir, b"cover.jpg")

        def store(self):
            self.stored += 1

    class Item:
        def __init__(self):
            self.id = 1
            self.path = os.fsencode(candidate)

        @staticmethod
        def destination():
            return os.fsencode(destination_dir / "01.flac")

    class Task:
        toppath = os.fsencode(staging)

        def __init__(self):
            self.album = Album()
            self.pruned = []
            self.item = Item()

        def imported_items(self):
            return [self.item]

        def prune(self, path):
            self.pruned.append(path)

    task = Task()
    selected = types.SimpleNamespace(path=os.fsencode(candidate), source_name="filesystem")

    class FetchArt:
        name = "fetchart"
        store_source = False

        def __init__(self):
            self.art_candidates = {task: selected}

        @staticmethod
        def _is_source_file_removal_enabled():
            return False

        @staticmethod
        def _is_candidate_fallback(_candidate):
            return False

    return task, selected, FetchArt(), destination_dir


def test_art_guard_publishes_only_in_a_new_held_album_directory(tmp_path, monkeypatch):
    import gc
    import types

    root = tmp_path / "music"
    staging = tmp_path / "staging"
    root.mkdir()
    loaded = []
    module = _load_art_guard_for_test(monkeypatch, loaded)
    session = types.SimpleNamespace(lib=types.SimpleNamespace(directory=os.fsencode(root)))
    gc.collect()
    descriptor_count = len(os.listdir("/proc/self/fd"))

    existing_task, _, existing_fetchart, existing_dir = _art_guard_task(root, staging, "Existing")
    existing_dir.mkdir(parents=True)
    existing_cover = existing_dir / "cover.jpg"
    existing_cover.write_bytes(b"user artwork")
    loaded[:] = [existing_fetchart]
    plugin = module.QobuzArtGuardPlugin()
    plugin._guard_art(session, existing_task)
    plugin._publish_art(session, existing_task)

    assert existing_cover.read_bytes() == b"user artwork"
    assert existing_task.album.artpath is None
    assert existing_fetchart.art_candidates == {}

    new_task, _, new_fetchart, new_dir = _art_guard_task(root, staging, "Brand New")
    loaded[:] = [new_fetchart]
    plugin._guard_art(session, new_task)
    assert not new_dir.exists()
    new_dir.mkdir(parents=True)
    (new_dir / "01.flac").write_bytes(b"audio")
    new_task.item.path = os.fsencode(new_dir / "01.flac")
    plugin._publish_art(session, new_task)

    assert (new_dir / "cover.jpg").read_bytes() == b"new artwork"
    assert new_task.album.artpath == os.fsencode(new_dir / "cover.jpg")
    assert new_task.album.stored == 1
    assert new_fetchart.art_candidates == {}

    real_copy = module._copy_candidate_to_private

    race_root = tmp_path / "race-music"
    race_root.mkdir()
    race_session = types.SimpleNamespace(
        lib=types.SimpleNamespace(directory=os.fsencode(race_root))
    )
    race_task, _, race_fetchart, race_dir = _art_guard_task(race_root, staging, "Moving Parent")
    loaded[:] = [race_fetchart]
    plugin._guard_art(race_session, race_task)
    race_dir.mkdir(parents=True)
    (race_dir / "01.flac").write_bytes(b"audio")
    race_task.item.path = os.fsencode(race_dir / "01.flac")
    displaced_artist = tmp_path / "displaced-artist-with-art"

    def move_parent_after_copy(parent_fd, candidate_fd):
        copied = real_copy(parent_fd, candidate_fd)
        (race_root / "Artist").rename(displaced_artist)
        (race_root / "Artist").mkdir()
        return copied

    monkeypatch.setattr(module, "_copy_candidate_to_private", move_parent_after_copy)
    plugin._publish_art(race_session, race_task)

    assert race_task.album.artpath is None
    assert not (displaced_artist / "Moving Parent" / "cover.jpg").exists()
    assert not (race_root / "Artist" / "Moving Parent" / "cover.jpg").exists()
    assert len(os.listdir("/proc/self/fd")) == descriptor_count


def test_art_cleanup_restores_an_unexpected_file_moved_to_quarantine(tmp_path, monkeypatch):
    cleanup_dir = tmp_path / "cleanup"
    cleanup_dir.mkdir()
    loaded = []
    module = _load_art_guard_for_test(monkeypatch, loaded)
    real_rename = module._rename_ownership_noreplace

    cleanup_file = cleanup_dir / "candidate.jpg"
    cleanup_file.write_bytes(b"held original")
    displaced = tmp_path / "held-original.jpg"
    cleanup_fd = os.open(cleanup_file, os.O_RDONLY | os.O_NOFOLLOW)
    cleanup_parent_fd = os.open(cleanup_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    raced = False

    def replace_before_cleanup(source_fd, source, destination_fd, destination):
        nonlocal raced
        if not raced:
            raced = True
            cleanup_file.rename(displaced)
            cleanup_file.write_bytes(b"late replacement")
        return real_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(module, "_rename_ownership_noreplace", replace_before_cleanup)
    try:
        assert not module._remove_exact_file(
            cleanup_parent_fd,
            os.fsencode(cleanup_file.name),
            cleanup_fd,
        )
    finally:
        os.close(cleanup_parent_fd)
        os.close(cleanup_fd)

    assert raced is True
    assert cleanup_file.read_bytes() == b"late replacement"
    assert displaced.read_bytes() == b"held original"
    assert not list(cleanup_dir.glob(".qobuz-art-cleanup-*"))


def test_failed_ownership_import_reclaims_only_its_empty_directories(tmp_path, monkeypatch):
    import json
    import os
    import tempfile

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    root = tmp_path / "music"
    empty_album = root / "Empty Artist" / "Empty Album"
    full_album = root / "Full Artist" / "Full Album"
    altered_album = root / "Altered Artist" / "Altered Album"
    marker_unlinked_empty = root / "Marker Unlinked Empty"
    marker_unlinked_nonempty = root / "Marker Unlinked Nonempty"
    marker_unlinked_replaced = root / "Marker Unlinked Replaced"
    empty_album.mkdir(parents=True)
    full_album.mkdir(parents=True)
    altered_album.mkdir(parents=True)
    marker_unlinked_empty.mkdir()
    marker_unlinked_nonempty.mkdir()
    marker_unlinked_replaced.mkdir()
    (full_album / "track.flac").write_bytes(b"audio")
    (altered_album / "track.flac").write_bytes(b"other audio")
    nonce = "a" * 64

    paths = [
        empty_album.parent,
        empty_album,
        full_album.parent,
        full_album,
        altered_album.parent,
        altered_album,
        marker_unlinked_empty,
        marker_unlinked_nonempty,
        marker_unlinked_replaced,
    ]
    for path in paths:
        relative = path.relative_to(root).as_posix()
        marker = path / beets._OWNERSHIP_MARKER
        marker.write_text(
            beets._ownership_marker_token(nonce, relative) + "\n",
            encoding="ascii",
        )

    records = []
    for path in paths:
        relative = path.relative_to(root)
        marker = path / beets._OWNERSHIP_MARKER
        records.append(
            {
                "relative": relative.as_posix(),
                "temporary_relative": (
                    relative.parent / f".qobuz-ownership-test-{relative.name}"
                ).as_posix(),
                "directory": beets._ownership_identity(path.stat()),
                "marker": beets._ownership_identity(marker.stat()),
            }
        )
    altered_marker = altered_album / beets._OWNERSHIP_MARKER
    altered_marker.write_text("changed after receipt\n", encoding="ascii")

    # Model a kill after the plug-in durably unlinked its marker but before it
    # appended marker=None. Only the exact, still-empty directory is safe to
    # recover. A new entry or a replacement at the same path must refuse.
    for path in (
        marker_unlinked_empty,
        marker_unlinked_nonempty,
        marker_unlinked_replaced,
    ):
        (path / beets._OWNERSHIP_MARKER).unlink()
    (marker_unlinked_nonempty / "keep.txt").write_text("unrelated", encoding="utf-8")
    replaced_original = root / "Marker Unlinked Original"
    marker_unlinked_replaced.rename(replaced_original)
    marker_unlinked_replaced.mkdir()

    payload = {
        "version": 1,
        "nonce": nonce,
        "root": os.path.abspath(root),
        "root_identity": beets._ownership_identity(root.stat()),
        "sealed": False,
        "items": [],
        "cleanup_directories": records,
    }
    manifest = tempfile.TemporaryFile(mode="w+b", dir=tmp_path)
    encoded = json.dumps(payload, separators=(",", ":")).encode("ascii")
    manifest.write(encoded + b'\n{"version":1')
    manifest.flush()
    os.fsync(manifest.fileno())
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    try:
        removed = beets._reclaim_ownership_capture(
            {
                "_file": manifest,
                "nonce": nonce,
            }
        )
    finally:
        manifest.close()

    assert removed == 3
    assert not empty_album.parent.exists()
    assert not marker_unlinked_empty.exists()
    assert (marker_unlinked_nonempty / "keep.txt").read_text(encoding="utf-8") == "unrelated"
    assert marker_unlinked_replaced.is_dir()
    assert replaced_original.is_dir()
    assert (full_album / "track.flac").read_bytes() == b"audio"
    assert not (full_album / beets._OWNERSHIP_MARKER).exists()
    assert not (full_album.parent / beets._OWNERSHIP_MARKER).exists()
    assert (altered_album / "track.flac").read_bytes() == b"other audio"
    assert altered_marker.read_text(encoding="ascii") == "changed after receipt\n"


@pytest.fixture
def _need_ffmpeg():
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def _need_flac():
    import shutil

    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _make_silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def test_import_override_yaml_round_trips_through_parser(monkeypatch):
    # The beets override is written by a hand-rolled single-quoted YAML emitter.
    # Feed it a path template with an apostrophe (the one metacharacter it has to
    # escape) and confirm a real parser reads every key back intact.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", "/config/beets/musiclibrary.db")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", "/music")
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "$albumartist's picks/$album %aunique{}")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "Singles/$artist - $title")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "embed")

    parsed = yaml.safe_load(beets._build_import_override_yaml())

    assert parsed["library"] == "/config/beets/musiclibrary.db"
    assert parsed["directory"] == "/music"
    assert parsed["import"]["move"] is True
    assert parsed["import"]["autotag"] is False
    assert parsed["import"]["duplicate_action"] == "merge"
    # the apostrophe survived the single-quote doubling
    assert parsed["paths"]["default"] == "$albumartist's picks/$album %aunique{}"
    assert parsed["paths"]["singleton"] == "Singles/$artist - $title"
    assert "fetchart" in parsed["plugins"] and "embedart" in parsed["plugins"]

    # A relative deployment path must still name the same database and music
    # root after the override is written inside the Beets config directory.
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("music"))
    relative = yaml.safe_load(beets._build_import_override_yaml())
    assert relative["library"] == str(Path("beets/musiclibrary.db").absolute())
    assert relative["directory"] == str(Path("music").absolute())
