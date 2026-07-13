"""Tests for queue/builder.py and queue/persistence.py."""
import errno
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.persistence import (
    QueueLoadStatus,
    _deserialize_queue_item,
    _serialize_queue_item,
    clear_pending_queue,
    load_pending_queue,
    offer_resume_pending_queue,
    save_pending_queue,
)


def _qitem(title="Test", **overrides):
    defaults = dict(
        album={"id": "1", "title": title},
        album_dir=Path(f"/music/{title.lower()}"),
        label=title, missing=[], present=[],
        upgrade_only=False, auto_upgrade=False,
    )
    defaults.update(overrides)
    return _build_queue_item(**defaults)


@pytest.fixture(autouse=True)
def _legacy_executor_runtime(monkeypatch):
    from qobuz_librarian import run_lock
    from qobuz_librarian.queue import executor
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    lease = run_lock.current_lease()
    acquired_here = lease is None
    if acquired_here:
        lease = run_lock.acquire()
    assert lease is not None
    monkeypatch.setattr(executor, "plan_durable_new_album", lambda *_a: None)
    monkeypatch.setattr(
        executor,
        "queue_item_may_create_library_backup",
        lambda _item: False,
    )
    monkeypatch.setattr(
        executor,
        "recover_startup_state",
        lambda **_kw: StartupRecoveryResult(StartupRecoveryStatus.CLEAR),
    )
    try:
        yield
    finally:
        if acquired_here:
            lease.close()


def test_build_queue_item_defaults_and_copies_siblings():
    original = [Path("/a"), Path("/b")]
    item = _qitem(siblings_to_delete=original)
    # Runtime accounting starts clean.
    assert item["backup_path"] is None
    assert (item["n_ok"], item["n_fail"]) == (0, 0)
    assert item["imported"] is False and item["result"] is None
    # siblings_to_delete must be a copy — mutating the caller's list mustn't leak in.
    original.append(Path("/c"))
    assert len(item["siblings_to_delete"]) == 2


def test_cli_download_now_persists_fresh_album_before_durable_execution(
        monkeypatch):
    from qobuz_librarian.modes import album as album_mode

    tracks = [
        {"id": "t1", "media_number": 1, "track_number": 1},
        {"id": "t2", "media_number": 1, "track_number": 2},
    ]
    album = {
        "id": "album-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96,
        "tracks": {"items": tracks},
    }
    events = []
    monkeypatch.setattr(album_mode, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        album_mode,
        "find_existing_tracks",
        lambda _album: ([], None),
    )
    monkeypatch.setattr(
        album_mode,
        "compute_missing",
        lambda _tracks, _existing: (tracks, []),
    )
    monkeypatch.setattr(
        album_mode,
        "plan_durable_new_album",
        lambda _item, _args: object(),
    )
    monkeypatch.setattr(
        album_mode,
        "save_pending_queue",
        lambda queue, *, mode: events.append(("save", mode, len(queue))),
    )
    monkeypatch.setattr(
        album_mode,
        "clear_pending_queue",
        lambda: events.append(("clear",)),
    )
    monkeypatch.setattr(
        album_mode,
        "staging_preflight",
        lambda _args: events.append(("preflight",)),
    )
    monkeypatch.setattr(
        album_mode,
        "process_album",
        lambda *_a, **_kw: pytest.fail("fresh album used legacy execution"),
    )

    def fake_execute(queue, _args, token, *, on_progress,
                     consolidate_duplicates):
        assert events == [
            ("preflight",),
            ("save", "album-now", 1),
        ]
        assert token == "token"
        assert consolidate_duplicates is False
        queue.clear()
        on_progress()
        return ([{"result": "downloaded", "imported": True}], True)

    monkeypatch.setattr(
        album_mode,
        "_execute_download_queue",
        fake_execute,
    )
    args = Namespace(
        force=False,
        dry_run=False,
        no_import=False,
        no_downsample=True,
        consolidate=False,
    )

    result = album_mode._download_album_now(
        album,
        args,
        "token",
        already_confirmed=True,
    )

    assert result["imported"] is True
    assert events == [
        ("preflight",),
        ("save", "album-now", 1),
        ("save", "album-now", 0),
        ("clear",),
    ]


def test_cli_download_now_requires_confirmation_before_durable_execution(
        monkeypatch):
    from qobuz_librarian.modes import album as album_mode

    album = {
        "id": "album-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96,
        "tracks": {"items": [{"id": "track-1"}]},
    }
    monkeypatch.setattr(album_mode, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        album_mode,
        "find_existing_tracks",
        lambda _album: ([], None),
    )
    monkeypatch.setattr(
        album_mode,
        "compute_missing",
        lambda tracks, _existing: (tracks, []),
    )
    monkeypatch.setattr(
        album_mode,
        "plan_durable_new_album",
        lambda _item, _args: object(),
    )
    summary = []
    monkeypatch.setattr(
        album_mode,
        "print_album_summary",
        lambda *_args, **_kwargs: summary.append(True),
    )
    monkeypatch.setattr(album_mode, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        album_mode,
        "staging_preflight",
        lambda *_args, **_kwargs: pytest.fail(
            "declined album ran staging preflight"
        ),
    )
    monkeypatch.setattr(
        album_mode,
        "save_pending_queue",
        lambda *_args, **_kwargs: pytest.fail("declined album was persisted"),
    )
    monkeypatch.setattr(
        album_mode,
        "_execute_download_queue",
        lambda *_args, **_kwargs: pytest.fail("declined album was executed"),
    )
    args = Namespace(
        force=False,
        dry_run=False,
        no_import=False,
        no_downsample=True,
        consolidate=False,
        yes=False,
    )

    result = album_mode._download_album_now(album, args, "token")

    assert summary == [True]
    assert result == {"result": "user_skipped", "n_missing": 1}


def test_queue_item_round_trips_and_resets_runtime_fields():
    item = _qitem(album={"id": "42", "title": "Round Trip"}, auto_upgrade=True,
                  siblings_to_delete=[Path("/music/old-edition")], quality=3)
    item["n_ok"] = 5
    item["imported"] = True
    restored = _deserialize_queue_item(_serialize_queue_item(item))
    assert restored["album_dir"] == item["album_dir"]
    assert restored["quality"] == 3
    # Runtime accounting is per-run state, not persisted — it resets on restore.
    assert restored["n_ok"] == 0 and restored["imported"] is False


def test_pending_queue_round_trips_and_clears(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    save_pending_queue([_qitem(title="Album A")], mode="album_walk")
    items, mode, saved_at = load_pending_queue()
    assert len(items) == 1 and items[0]["album"]["title"] == "Album A"
    assert mode == "album_walk"
    datetime.fromisoformat(saved_at)        # saved_at is valid ISO
    clear_pending_queue(explicit_discard=True)
    assert load_pending_queue().status is QueueLoadStatus.ABSENT


def test_pending_queue_rejects_bad_payloads(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    # A future schema version is ignored rather than mis-parsed.
    qfile.write_text(json.dumps({"version": 99, "items": [], "mode": "x", "count": 0}))
    assert load_pending_queue()[0] is None
    # A file that parses as a list/string must not crash startup.
    qfile.write_text('["a", "b"]', encoding="utf-8")
    assert load_pending_queue() == (None, None, None)
    # A power loss can leave the (not-fsync'd) file zero-length or truncated;
    # load must discard it cleanly so the walk rebuilds, not raise.
    qfile.write_text("", encoding="utf-8")
    assert load_pending_queue() == (None, None, None)
    qfile.write_text('{"version": 1, "items": [{"al', encoding="utf-8")
    assert load_pending_queue() == (None, None, None)


def test_resume_keeps_pending_file_when_not_drained(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    save_pending_queue([_qitem()], mode="walk_queue")
    monkeypatch.setattr("qobuz_librarian.queue.executor._execute_download_queue",
                        lambda items, args, token, **kw: ([], False))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    offer_resume_pending_queue(Namespace(), "tok")
    assert load_pending_queue()[0]  # albums left to retry must survive the resume


def test_cli_startup_recovery_offers_active_journal_without_rewriting_it(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian import run_lock
    from qobuz_librarian.completion import (
        CompletionExpectation,
        CompletionInput,
        CompletionOrigin,
        CompletionOriginKind,
        CompletionScope,
        QualityTarget,
        RecoveryOwner,
    )
    from qobuz_librarian.queue import executor, persistence, startup_recovery
    from qobuz_librarian.queue import journal as queue_state

    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    cfg.STAGING_DIR.mkdir()

    item = _qitem(
        album={"id": "1", "title": "Interrupted"},
        album_dir=None,
        missing=[{"id": "track-1"}],
    )
    saved = queue_state.save_queue_journal(
        queue_state.create_queue_journal([item], mode="album_walk")
    )
    item_id = saved.items[0].item_id
    active = queue_state.transition_journal_item(
        saved,
        item_id,
        queue_state.QueuePhase.ACTIVE,
        completion_input=CompletionInput(
            owner=RecoveryOwner(saved.operation_id, item_id),
            origin=CompletionOrigin(
                CompletionOriginKind.CLI,
                "download-queue",
            ),
            expectation=CompletionExpectation(
                album_id="1",
                scope=CompletionScope.ALBUM,
                catalogue_slots=("qobuz:track-1",),
                requested_slots=("qobuz:track-1",),
                quality_targets=(QualityTarget(
                    "qobuz:track-1",
                    16,
                    44_100,
                ),),
            ),
            effective_tier=2,
        ),
    )
    recovery = startup_recovery.recover_startup_state(
        authority=run_lock.current_lease(),
    )
    assert recovery.status is startup_recovery.StartupRecoveryStatus.RESUME_REQUIRED
    assert recovery.items[0].action is (
        startup_recovery.StartupRecoveryAction.RESUME_DOWNLOAD
    )

    captured = {}

    def fake_execute(items, _args, token, **kwargs):
        captured.update(items=items, token=token, kwargs=kwargs)
        return [], False

    monkeypatch.setattr(executor, "_execute_download_queue", fake_execute)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(
        persistence,
        "save_pending_queue",
        lambda *_args, **_kwargs: pytest.fail(
            "typed recovery was rewritten through the pending-only adapter"
        ),
    )

    persistence.offer_resume_startup_recovery(
        Namespace(dry_run=False),
        "tok",
        recovery,
    )

    assert captured["token"] == "tok"
    assert captured["kwargs"]["on_progress"] is None
    assert queue_state._serialize_queue_item(captured["items"][0]) == (
        active.items[0].planned
    )
    unchanged = queue_state.load_queue_journal(saved.operation_id)
    assert unchanged.status is queue_state.QueueLoadStatus.READY
    assert unchanged.journal == active


def test_saved_work_choices_reach_menu_without_download_preflight(
        tmp_path, monkeypatch):
    from qobuz_librarian import cli
    from qobuz_librarian.integrations import lyric_fetch
    from qobuz_librarian.integrations.lyrics import save_lyric_retry

    qfile = tmp_path / "queue.json"
    retry_file = tmp_path / "lyrics-retry.json"
    retry_track = tmp_path / "retry.flac"
    retry_track.write_bytes(b"audio")
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", retry_file)
    save_pending_queue([_qitem()], mode="walk_queue")
    save_lyric_retry([str(retry_track)])

    args = Namespace(
        reset_walk_seen=False, dry_run=False, verbose=False, quiet=False,
        no_color=False, migrate=False, lyrics_walk=False,
        check_new_releases=False, downsample_walk=False, artist=None,
        upgrade_walk=False, query=None, auto_upgrade=False,
    )
    monkeypatch.setattr(cli, "parse_args", lambda: args)
    monkeypatch.setattr(cli, "attach_file_handler", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "banner", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "acquire_run_lock", lambda: object())
    monkeypatch.setattr(cli, "cleanup_old_upgrade_backups", lambda: 0)
    monkeypatch.setattr(cli, "_prune_lyric_state_orphans", lambda: None)
    monkeypatch.setattr("qobuz_librarian.web.settings_store.load", lambda: None)
    monkeypatch.setattr("qobuz_librarian.library.flac_cache.prune_missing",
                        lambda: None)
    monkeypatch.setattr("qobuz_librarian.library.repair_cache.prune_expired",
                        lambda: None)
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)

    def unexpected_preflight(*_args, **_kwargs):
        pytest.fail("download/Qobuz preflight ran before a saved-work choice")

    monkeypatch.setattr(cli, "check_rip", unexpected_preflight)
    monkeypatch.setattr(cli, "check_media_tools", unexpected_preflight)
    monkeypatch.setattr(cli, "load_qobuz_token", unexpected_preflight)
    answers = iter(("k", "k", "q"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    cli.main()

    assert load_pending_queue()[0]
    assert retry_file.exists()


def test_executor_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    """Queue-mode gap-fill backs up present tracks before re-ripping."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "02 - kept.flac").write_bytes(b"\x00" * 1000)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gfb,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 1,
        "auto_upgrade": False,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert owned.exists()
    assert owned.read_bytes() == b"the-owned-original"


def test_executor_auto_downsample_marker_uses_signature_fallback(
        monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    final_dir = tmp_path / "music" / "Bill Evans" / "Waltz For Debby (2023)"
    final_dir.mkdir(parents=True)
    (final_dir / "01.flac").write_bytes(b"audio")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(executor, "find_album_dir_by_track_signatures",
                        lambda _sigs: final_dir, raising=False)
    marked = []
    monkeypatch.setattr(executor, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    item = {
        "album": {"id": "q8m2", "title": "Waltz For Debby",
                  "artist": {"name": "Bill Evans Trio"}, "tracks": {"items": []}},
        "album_dir": None,
        "backup_path": None,
        "gap_fill_backup_path": None,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
        "resampled_n": 1,
        "post_import_signatures": ["sig"],
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert item["_resolved_post_dir"] == final_dir
    assert marked == [final_dir]


def _path_identity(path):
    value = path.stat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "modified_ns": value.st_mtime_ns,
        "changed_ns": value.st_ctime_ns,
    }


def _sealed_ownership_payload(root, track, album_dir):
    scope = album_dir.relative_to(root)
    ancestors = []
    for length in range(1, len(scope.parts)):
        ancestor = root.joinpath(*scope.parts[:length])
        ancestors.append({
            "relative": ancestor.relative_to(root).as_posix(),
            "directory": _path_identity(ancestor),
        })
    return {
        "version": 1,
        "sealed": True,
        "root": str(root),
        "root_identity": _path_identity(root),
        "items": [{
            "relative": track.relative_to(root).as_posix(),
            "file": _path_identity(track),
            "album_scope": {
                "relative": scope.as_posix(),
                "directory": _path_identity(album_dir),
                "ancestors": ancestors,
            },
            "created_directories": [],
            "companions": [],
        }],
        "cleanup_directories": [],
    }


def test_executor_uses_sealed_import_path_for_a_custom_disc_layout(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    root = tmp_path / "music"
    final_dir = root / "Jazz" / "Bill Evans" / "Waltz For Debby"
    track = final_dir / "Disc 2" / "01.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")

    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    monkeypatch.setattr(
        executor, "find_album_dir_by_track_signatures", lambda _sigs: None)
    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: None)
    marked = []
    monkeypatch.setattr(executor, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    item = {
        "album": {"id": "q8m2", "title": "Waltz For Debby",
                  "artist": {"name": "Bill Evans"}, "tracks": {"items": []}},
        "album_dir": None,
        "backup_path": None,
        "gap_fill_backup_path": None,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
        "resampled_n": 1,
        "post_import_signatures": ["sig"],
        "_capture_import_ownership": True,
        "_import_ownership": _sealed_ownership_payload(
            root, track, final_dir
        ),
    }
    args = Namespace(no_import=False, consolidate=False)

    executor._resolve_queue_item(item, args, imported_globally=True)

    assert item["_resolved_post_dir"] == final_dir
    assert marked == [final_dir]


def test_sealed_import_scope_keeps_a_real_album_named_disc_2(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    root = tmp_path / "music"
    album_dir = root / "Artist" / "Disc 2"
    track = album_dir / "01.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    item = {
        "_import_ownership": _sealed_ownership_payload(
            root, track, album_dir
        )
    }

    assert executor._verified_import_album_dir(item) == album_dir


def test_sealed_import_scope_refuses_a_swapped_ancestor(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    root = tmp_path / "music"
    artist = root / "Artist"
    album_dir = artist / "Album"
    track = album_dir / "01.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    item = {
        "_import_ownership": _sealed_ownership_payload(
            root, track, album_dir
        )
    }
    parked = tmp_path / "parked-artist"
    real_open = executor.os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "Artist" and dir_fd is not None and not swapped:
            swapped = True
            artist.rename(parked)
            artist.mkdir()
        return descriptor

    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    monkeypatch.setattr(executor.os, "open", swapping_open)

    assert executor._verified_import_album_dir(item) is None


def test_executor_auto_downsample_marker_prefers_signature_over_old_folder(
        monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    old_dir = tmp_path / "music" / "Bill Evans Trio" / "Waltz For Debby"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old")
    final_dir = tmp_path / "music" / "Bill Evans" / "Waltz For Debby (2023)"
    final_dir.mkdir(parents=True)
    (final_dir / "01.flac").write_bytes(b"new")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: old_dir)
    monkeypatch.setattr(executor, "find_album_dir_by_track_signatures",
                        lambda _sigs: final_dir, raising=False)
    monkeypatch.setattr(executor, "_is_split_album_merge",
                        lambda *a, **k: False)
    marked = []
    monkeypatch.setattr(executor, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    item = {
        "album": {"id": "q8m2", "title": "Waltz For Debby",
                  "artist": {"name": "Bill Evans Trio"}, "tracks": {"items": []}},
        "album_dir": old_dir,
        "backup_path": None,
        "gap_fill_backup_path": None,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
        "resampled_n": 1,
        "post_import_signatures": ["sig"],
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert item["_resolved_post_dir"] == final_dir
    assert marked == [final_dir]


def test_executor_leaves_split_multi_artist_folders_unchanged(
        monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    old_dir = tmp_path / "music" / "Artist, Guest" / "Album"
    old_dir.mkdir(parents=True)
    old_track = old_dir / "01.flac"
    old_track.write_bytes(b"old")
    final_dir = tmp_path / "music" / "Artist" / "Album"
    final_dir.mkdir(parents=True)
    new_track = final_dir / "02.flac"
    new_track.write_bytes(b"new")

    monkeypatch.setattr(
        executor, "find_album_dir_by_track_signatures", lambda _sigs: final_dir)
    monkeypatch.setattr(executor, "_is_split_album_merge", lambda *_args: True)

    item = {
        "album": {"artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": old_dir,
        "backup_path": None,
        "gap_fill_backup_path": None,
        "siblings_to_delete": [],
        "n_ok": 1,
        "n_fail": 0,
        "n_lossy": 0,
        "auto_upgrade": False,
        "post_import_signatures": ["sig"],
    }
    executor._resolve_queue_item(
        item, Namespace(no_import=False, consolidate=False),
        imported_globally=True)

    assert old_track.read_bytes() == b"old"
    assert new_track.read_bytes() == b"new"


def test_executor_self_heal_retry_no_files_keeps_first_download_state(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    first_staged = staging / "Artist" / "Album"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": [{"id": 1, "title": "A", "track_number": 1}]}}
    item = _qitem(title="Album", album=album, album_dir=None,
                  missing=album["tracks"]["items"], present=[])
    queue = [item]

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs",
                        lambda _d, _a: ([], 0))
    monkeypatch.setattr(executor, "track_signatures_for_album_dirs",
                        lambda _d: [])
    monkeypatch.setattr(
        executor, "_staged_album_dirs",
        lambda _item: [first_staged] if first_staged.exists() else [],
    )
    monkeypatch.setattr(executor, "_resolve_queue_item",
                        lambda item, _args, imported: {
                            "result": "imported" if imported else item.get("result"),
                            "n_ok": item.get("n_ok", 0),
                        })

    imported_dirs = []
    monkeypatch.setattr(executor, "_import_album_with_retry",
                        lambda dirs: imported_dirs.append(list(dirs)) or True)

    def fake_download(**kw):
        result = kw["result"]
        if kw.get("quality") == 4:
            result.update(n_ok=0, n_fail=1, n_lossy=0, failed_tracks=["A"],
                          lossy_tracks=[], broken_tracks=[], elapsed=0.0,
                          gap_fill_backup_path=None)
        else:
            first_staged.mkdir(parents=True, exist_ok=True)
            (first_staged / "01.flac").write_bytes(b"first")
            result.update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                          lossy_tracks=[], broken_tracks=[], elapsed=0.0,
                          gap_fill_backup_path=None)
        return result

    def fake_verify(_album, _staged_dirs, *, redownload_at_max, **_kw):
        dirs = redownload_at_max()
        return {"under": True, "recovered": False, "retried": True,
                "n_below": 1, "served": (16, 44100),
                "target": (24, 96000), "staged_dirs": dirs}

    monkeypatch.setattr(executor, "run_album_download", fake_download)
    monkeypatch.setattr(executor, "verify_and_recover", fake_verify)

    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     consolidate=False)
    results, drained = executor._execute_download_queue(
        queue, args, token="tok")

    assert drained is True
    assert queue == []
    assert item["n_ok"] == 1
    assert results[-1]["n_ok"] == 1
    assert imported_dirs == [[first_staged]]
    assert (first_staged / "01.flac").read_bytes() == b"first"


def test_executor_upgrade_runs_completeness_gate_before_dropping_backup(monkeypatch, tmp_path):
    # The artist/upgrade walks bulk-upgrade through this executor, so it must run
    # the same completeness gate process.py does: a decode-clean import whose
    # rebuilt folder isn't verifiably as complete as the backup KEEPS the backup.
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")
    backup = tmp_path / "backups" / "Album.bak"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"old")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(no_import=False, consolidate=False)

    carried = []
    disposed = []
    monkeypatch.setattr(proc, "_carry_non_audio_from_backup",
                        lambda *_a, **_k: carried.append(True))
    monkeypatch.setattr(executor, "dispose_backup",
                        lambda *_a, **_k: disposed.append(True) or True)
    monkeypatch.setattr(executor, "pin_unverified_upgrade_backup",
                        lambda *_a, **_k: True)
    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: False)
    executor._resolve_queue_item(item, args, imported_globally=True)
    assert not carried and not disposed

    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)
    monkeypatch.setattr(proc, "_carry_non_audio_from_backup",
                        lambda *_a, **_k: (album_dir, {"sealed": True}))
    item["backup_path"] = backup
    executor._resolve_queue_item(item, args, imported_globally=True)
    assert disposed == [True]


def test_executor_upgrade_carries_non_audio_companions_from_backup(monkeypatch, tmp_path):
    # Regression: the bulk/web upgrade path (this executor) must carry non-audio
    # companions — booklets, scans, .cue/.log, hand-placed art — out of the backup
    # into the rebuilt album before reaping it, exactly as the single-album
    # process.py path does. The audio-only completeness gate ignores them, so
    # without the carry they'd be destroyed with the backup on every upgrade.
    from qobuz_librarian.library.backup import backup_album_dir
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"old")
    (album_dir / "booklet.pdf").write_bytes(b"the-booklet")
    (album_dir / "scans").mkdir()
    (album_dir / "scans" / "front.jpg").write_bytes(b"art")
    monkeypatch.setattr(executor.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(executor.cfg, "UPGRADE_BACKUP_DIR",
                        tmp_path / "backups")
    backup = backup_album_dir(album_dir)
    assert backup is not None and backup.complete is True
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)
    monkeypatch.setattr(proc, "_upgrade_trees_verified", lambda *_a: True)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    # Backup reaped, but its non-audio companions carried into the live folder;
    # the upgraded audio is left untouched (the old copy is not carried back).
    assert not backup.path.exists()
    assert (album_dir / "booklet.pdf").read_bytes() == b"the-booklet"
    assert (album_dir / "scans" / "front.jpg").read_bytes() == b"art"
    assert (album_dir / "01.flac").read_bytes() == b"new"


def test_executor_per_album_isolation_one_album_failure_keeps_others(monkeypatch, tmp_path):
    """The whole point of the per-album pipeline: a beets failure on
    album N leaves albums 1..N-1 already imported and N+1..end still
    importable, instead of taking the whole batch down. The failing album's
    staged dir is parked under BEETS_RETRY_DIR for an import-only retry, while
    its queue entry stays pending until that import is actually accepted."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    items = []
    for tag in ("A", "B", "C"):
        items.append({
            "album": {"id": tag, "title": f"Album {tag}",
                      "artist": {"name": f"Artist-{tag}"},
                      "tracks": {"items": []}},
            "album_dir": None,
            "auto_upgrade": False,
            "missing": [], "present": [], "upgrade_only": False,
            "label": tag,
            "n_ok": 1, "n_fail": 0, "n_lossy": 0,
            "failed_tracks": [], "lossy_tracks": [],
            "rate_limited": False, "elapsed": 0.0,
        })

    def fake_download(item):
        tag = item["label"]
        d = staging / f"Artist-{tag}" / f"Album {tag}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "01.flac").write_bytes(b"")

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_download_for_queue_item", fake_download)
    monkeypatch.setattr(
        executor,
        "_staged_album_dirs",
        lambda item: [
            staging / f"Artist-{item['label']}" / f"Album {item['label']}"
        ],
    )
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs",
                        lambda _d, _a: ([], 0))
    # Item B fails beets ("error" = non-retryable); A and C succeed.
    by_label = {"A": "ok", "B": "error", "C": "ok"}
    seen = []

    def fake_import(album_dirs):
        # Map back to the item by its album-dir grandparent name ("Artist-X").
        artist = album_dirs[0].parent.name.split("-")[-1]
        seen.append(artist)
        return by_label[artist]

    monkeypatch.setattr(executor, "beets_import_albums", fake_import)
    monkeypatch.setattr(
        executor, "retire_empty_download_staging", lambda _item: True)
    monkeypatch.setattr(
        executor, "retain_download_staging", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "_resolve_queue_item",
                        lambda item, args, imported_globally: {
                            "dir": item["album_dir"], "imported": imported_globally,
                            "result": "downloaded" if imported_globally else "failed",
                            "n_ok": item.get("n_ok", 0),
                            "n_fail": item.get("n_fail", 0),
                            "n_lossy": item.get("n_lossy", 0),
                            "auto_upgrade": False,
                        })

    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     consolidate=False)
    results, drained = executor._execute_download_queue(items, args, token=None)

    assert seen == ["A", "B", "C"]
    assert [r["imported"] for r in results] == [True, False, True]
    # B's staged folder got parked; A and C's are still where the test left
    # them (beets would have moved them in real life, but we stubbed it out).
    assert not (staging / "Artist-B" / "Album B").exists()
    from qobuz_librarian.integrations.staging import list_groups
    parked = [
        tree for group in list_groups(kind="beets") for tree in group.trees
        if tree.original_relative.endswith("Album B")
    ]
    assert len(parked) == 1
    assert [item["label"] for item in items] == ["B"]
    assert drained is False


def test_partial_beets_import_retains_exact_run_and_queue_item(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.staging import (
        create_staging_run,
        list_groups,
    )
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    album = {
        "id": "partial-import",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [{"id": "1"}, {"id": "2"}]},
    }
    item = _qitem(
        title="Album",
        album=album,
        album_dir=None,
        missing=album["tracks"]["items"],
        present=[],
    )
    queue = [item]
    staged = {}

    def fake_download(queue_item):
        run = create_staging_run()
        album_dir = run.path / "Artist" / "Album"
        album_dir.mkdir(parents=True)
        first = album_dir / "01.flac"
        second = album_dir / "02.flac"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        queue_item.update(
            _staging_run=run.to_record(),
            n_ok=2,
            n_fail=0,
            n_lossy=0,
            elapsed=0.0,
        )
        staged.update(run=run, album=album_dir, first=first, second=second)

    def fake_partial_import(_dirs):
        staged["first"].unlink()
        return False

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(executor, "_download_for_queue_item", fake_download)
    monkeypatch.setattr(
        executor, "_staged_album_dirs", lambda _item: [staged["album"]])
    monkeypatch.setattr(
        executor, "_run_pre_import_hooks_for_dirs", lambda _d, _a: ([], 0))
    monkeypatch.setattr(
        executor, "track_signatures_for_album_dirs", lambda _d: [])
    monkeypatch.setattr(executor, "_import_album_with_retry", fake_partial_import)
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda queue_item, _args, imported: {
            "result": queue_item.get("result"),
            "imported": imported,
            "n_ok": queue_item.get("n_ok", 0),
        },
    )

    args = Namespace(
        dry_run=False,
        no_import=False,
        no_downsample=True,
        consolidate=False,
    )
    results, drained = executor._execute_download_queue(
        queue, args, token=None)

    assert drained is False
    assert queue == [item]
    assert results[0]["result"] == "import_failed"
    assert item["_staging_run_retained"] is True
    assert not staged["run"].path.exists()
    retained = list_groups(kind="interrupted")
    assert len(retained) == 1
    residual = retained[0].trees[0]
    assert [relative for relative, _identity in residual.files] == [
        "Artist/Album/02.flac",
    ]
    assert (residual.path / "Artist" / "Album" / "02.flac").read_bytes() == b"second"


def test_executor_keeps_only_failed_downloads_for_retry(monkeypatch, tmp_path):
    """A flush drops every album that landed audio and keeps only the ones
    that downloaded nothing, so a resume re-downloads the genuine failures
    and never the albums already on disk."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    def make(tag, n_ok):
        return {
            "album": {"id": tag, "title": tag, "artist": {"name": tag},
                      "tracks": {"items": []}},
            "album_dir": None, "auto_upgrade": False,
            "missing": [], "present": [], "upgrade_only": False, "label": tag,
            "n_ok": n_ok, "n_fail": 0, "n_lossy": 0,
            "failed_tracks": [], "lossy_tracks": [],
            "rate_limited": False, "elapsed": 0.0,
        }

    imported = make("imported", 1)
    nothing = make("nothing", 0)
    queue = [imported, nothing]

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_download_for_queue_item", lambda _i: None)
    monkeypatch.setattr(executor, "_staged_album_dirs",
                        lambda item: [staging / item["label"]] if item["n_ok"] else [])
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs",
                        lambda _d, _a: ([], 0))
    monkeypatch.setattr(executor, "beets_import_albums", lambda _d: "ok")
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "_resolve_queue_item",
                        lambda item, args, ok: {
                            "dir": None, "imported": ok,
                            "result": "downloaded" if item["n_ok"] else "failed",
                            "n_ok": item["n_ok"], "n_fail": 0, "n_lossy": 0,
                            "auto_upgrade": False})

    saves = []
    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     consolidate=False)
    results, drained = executor._execute_download_queue(
        queue, args, token=None, on_progress=lambda: saves.append(list(queue)))

    assert queue == [nothing]      # imported dropped, the empty download kept
    assert drained is False
    assert len(results) == 2       # results stay 1:1 with the items passed in
    assert saves                   # progress persisted as the item dropped


def test_executor_stops_when_queue_progress_cannot_be_persisted(
        monkeypatch, tmp_path):
    """A failed journal commit must stop before the next album starts."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    queue = [
        _qitem("first", album_dir=None),
        _qitem("second", album_dir=None),
    ]
    started = []
    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    def download(item):
        started.append(item["label"])
        item["n_ok"] = 1

    monkeypatch.setattr(executor, "_download_for_queue_item", download)
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda item, _args, _imported: {"result": item["label"]},
    )

    args = Namespace(
        dry_run=False,
        no_import=True,
        no_downsample=True,
        consolidate=False,
    )

    def fail_save():
        raise OSError("journal write failed")

    with pytest.raises(OSError, match="journal write failed"):
        executor._execute_download_queue(
            queue,
            args,
            token=None,
            on_progress=fail_save,
        )

    assert started == ["first"]


def test_failed_import_parking_refuses_a_same_name_replacement(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.staging import capture_tree
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    album = staging / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    receipt = capture_tree(album)
    assert receipt is not None
    displaced = staging / "displaced"
    album.rename(displaced)
    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement")

    assert executor._move_to_beets_retry(
        [album], "failed", expected=[receipt]) == []
    assert (album / "01.flac").read_bytes() == b"replacement"
    assert (displaced / "01.flac").read_bytes() == b"original"


def test_reimport_parked_albums_clears_moved_and_keeps_skipped(monkeypatch, tmp_path):
    """A parked album is cleared only when its audio actually leaves disk on the
    retry import. A beets run that exits 0 while skipping the album (e.g. a
    library duplicate) leaves the files in place — the parked copy must be kept,
    not deleted on the strength of the exit code, since it's the only copy."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.staging import park_trees
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    good_source = staging / "Good Album"
    skipped_source = staging / "Dup Album"
    good_source.mkdir(parents=True)
    skipped_source.mkdir(parents=True)
    good_flac = good_source / "01.flac"
    skipped_flac = skipped_source / "01.flac"
    good_flac.write_bytes(b"flac")
    skipped_flac.write_bytes(b"flac")
    good_group = park_trees([good_source], "good")
    skipped_group = park_trees([skipped_source], "skipped")
    assert good_group is not None and skipped_group is not None
    good = good_group.path
    skipped = skipped_group.path
    good_flac = good_group.trees[0].path / "01.flac"
    skipped_flac = skipped_group.trees[0].path / "01.flac"

    def fake_import(dirs):
        # beets moves audio into the library on a real import; simulate that for
        # the good album and leave the skipped one's files where they are.
        if dirs[0] == good_group.trees[0].path:
            good_flac.unlink()
        return "ok"  # exit 0 either way — the disk, not this, decides cleanup
    monkeypatch.setattr(executor, "beets_import_albums", fake_import)

    assert executor._reimport_parked_albums()[0] is True
    assert not good.exists()           # audio moved out → parking dir cleared
    assert skipped.exists()            # files remain → kept parked, not deleted
    assert skipped_flac.exists()       # the only copy of the skipped track survives


def test_reimport_parked_albums_ignores_legacy_name_only_group(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    album = (staging / cfg.BEETS_RETRY_DIR
             / "20260101_000000-legacy" / "Album")
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"unproved")
    imported = []
    monkeypatch.setattr(
        executor, "beets_import_albums",
        lambda dirs: imported.append(tuple(dirs)) or "ok")

    assert executor._reimport_parked_albums() == (False, [])
    assert imported == []
    assert track.read_bytes() == b"unproved"


def test_reimport_parked_albums_preserves_non_audio_companions(monkeypatch, tmp_path):
    """When beets imports the audio out of a parked album, the non-audio
    companions it leaves behind (booklets, scans) must be rescued, not deleted
    with the staging husk — the same data-loss shape as the upgrade-backup path."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.staging import park_trees
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    data = tmp_path / "data"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    album = staging / "Some Album"
    album.mkdir(parents=True)
    flac = album / "01.flac"
    booklet = album / "booklet.pdf"
    flac.write_bytes(b"flac")
    booklet.write_bytes(b"%PDF-1.4 booklet")
    group = park_trees([album], "companions")
    assert group is not None
    parked = group.path
    album = group.trees[0].path
    flac = album / "01.flac"
    booklet = album / "booklet.pdf"

    def fake_import(dirs):
        flac.unlink()   # beets moves only the audio out, like a real import
        return "ok"
    monkeypatch.setattr(executor, "beets_import_albums", fake_import)

    assert executor._reimport_parked_albums()[0] is True
    assert parked.exists()                           # unproved husk is retained
    assert booklet.read_bytes() == b"%PDF-1.4 booklet"


def test_queue_runs_post_download_truncation_recheck_on_success(monkeypatch, tmp_path):
    # The post-download length recheck must fire on the QUEUE path too — walk,
    # artist/album queue, resume, repair refill and the web single-track grab all
    # flow through _execute_download_queue, so a clean truncation in a freshly
    # filled album would otherwise never be surfaced on the bulk-fill workflow.
    from qobuz_librarian.queue import executor

    post_dir = tmp_path / "music" / "Artist" / "Album"
    post_dir.mkdir(parents=True)
    (post_dir / "01.flac").write_bytes(b"\x00" * 1000)

    rechecked = []
    monkeypatch.setattr(executor, "staging_preflight", lambda args: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(executor, "_download_for_queue_item",
                        lambda item: item.update(n_ok=1, n_fail=0, n_lossy=0, elapsed=0.0))
    monkeypatch.setattr(executor, "_staged_album_dirs", lambda item: [post_dir])
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs",
                        lambda dirs, args: ([], 0))
    monkeypatch.setattr(executor, "_import_album_with_retry", lambda dirs: True)
    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: post_dir)
    monkeypatch.setattr(executor, "_count_audio_files_in", lambda _d: 1)
    monkeypatch.setattr(executor, "_is_split_album_merge", lambda *a: False)
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "write_post_import_sidecars", lambda dirs: None)
    monkeypatch.setattr(executor, "log_fetch", lambda payload: None)
    monkeypatch.setattr(executor, "warn_if_download_truncated",
                        lambda d, token, label: rechecked.append((d, token, label)) or [])

    item = _qitem(title="Album",
                  album={"id": "A", "title": "Album", "artist": {"name": "Artist"},
                         "tracks": {"items": []}},
                  album_dir=post_dir)
    args = Namespace(dry_run=False, no_import=False, consolidate=False)
    results, drained = executor._execute_download_queue([item], args, "tok")

    assert results and results[0]["result"] == "downloaded"
    assert rechecked == [(post_dir, "tok", "Album")]


def test_cooldown_sleep_wakes_on_cancel():
    # The inter-album cooldown after a rate-limited rip can be 30s+; a Stop must
    # cut it short instead of leaving the worker blocked for the whole window.
    import time as _t

    from qobuz_librarian.queue import executor

    start = _t.monotonic()
    assert executor._sleep_unless_cancelled(30, lambda: True, step=0.05) is True
    assert _t.monotonic() - start < 1.0

    start = _t.monotonic()
    assert executor._sleep_unless_cancelled(0.1, lambda: False, step=0.02) is False
    assert _t.monotonic() - start >= 0.1


def test_executor_refuses_staging_without_current_run_lock(monkeypatch):
    from qobuz_librarian.queue import executor

    preflight_called = []
    monkeypatch.setattr(executor.run_lock, "current_lease", lambda: None)
    monkeypatch.setattr(
        executor,
        "staging_preflight",
        lambda _args: preflight_called.append(True),
    )

    args = Namespace(dry_run=False, no_import=False, no_downsample=True)
    with pytest.raises(executor.DurableAlbumUnavailable, match="run lock"):
        executor._execute_download_queue([_qitem()], args, token=None)
    assert preflight_called == []


def test_executor_refuses_legacy_fallback_for_library_backup_work(monkeypatch):
    from qobuz_librarian.queue import executor
    from qobuz_librarian.queue.durable_album import (
        queue_item_may_create_library_backup,
    )

    preflight_called = []
    monkeypatch.setattr(
        executor,
        "queue_item_may_create_library_backup",
        queue_item_may_create_library_backup,
    )
    monkeypatch.setattr(
        executor,
        "staging_preflight",
        lambda _args: preflight_called.append(True),
    )

    args = Namespace(dry_run=False, no_import=False, no_downsample=True)
    tracks = [{"id": str(index)} for index in range(1, 5)]
    destructive_items = (
        _qitem(album_dir=None, siblings_to_delete=[Path("/music/old")]),
        _qitem(
            album={"id": "1", "title": "Test", "tracks": {"items": tracks}},
            missing=tracks,
            present=[{"id": "present"}],
            upgrade_only=True,
            auto_upgrade=False,
        ),
    )
    for item in destructive_items:
        with pytest.raises(
            executor.DurableAlbumUnavailable,
            match="crash-safe recovery",
        ):
            executor._execute_download_queue([item], args, token=None)
    assert preflight_called == []


def test_web_durable_lane_requires_one_job_bound_album():
    from qobuz_librarian.queue import executor

    item = _qitem(album={"id": "album-1", "title": "Album"})
    other = _qitem(album={"id": "album-2", "title": "Other"})
    assert executor._durable_plan_allowed(
        [item],
        item,
        execution_mode="web-job:job-1",
        web_album_id="album-1",
    ) is True
    assert executor._durable_plan_allowed(
        [item, other],
        item,
        execution_mode="web-job:job-1",
        web_album_id="album-1",
    ) is False
    assert executor._durable_plan_allowed(
        [item],
        item,
        execution_mode="web-job:job-1",
        web_album_id="album-2",
    ) is False


def test_web_completion_ack_is_bound_to_current_job_and_planned_album(
        monkeypatch):
    from qobuz_librarian.completion import (
        CompletionOrigin,
        CompletionOriginKind,
        RecoveryOwner,
    )
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.web import jobs

    job = jobs.Job(id="job-1", album_id="album-1")
    item = _qitem(album={"id": "album-1", "title": "Album"})
    planned = queue_state._serialize_queue_item(item)
    origin = CompletionOrigin(CompletionOriginKind.WEB_JOB, job.id)
    owner = RecoveryOwner("operation-1", "item-1")
    calls = []
    monkeypatch.setattr(
        jobs.job_persistence,
        "acknowledge_durable_completion",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    jobs._TLS.current_job = job
    try:
        assert jobs.acknowledge_current_job_durable_completion(
            origin,
            owner,
            album_id="album-1",
            completion_hash="a" * 64,
            planned=planned,
            post_dir="/music/Artist/Album",
        ) is True
        changed = dict(planned)
        changed["album"] = {"id": "album-2", "title": "Other"}
        assert jobs.acknowledge_current_job_durable_completion(
            origin,
            owner,
            album_id="album-1",
            completion_hash="a" * 64,
            planned=changed,
            post_dir="/music/Artist/Album",
        ) is False
    finally:
        jobs._TLS.current_job = None

    assert len(calls) == 1


def test_durable_retry_stops_without_legacy_resolution(monkeypatch):
    from qobuz_librarian.completion import (
        CompletionOrigin,
        CompletionOriginKind,
    )
    from qobuz_librarian.queue import executor
    from qobuz_librarian.queue.durable_runner import (
        DurableAlbumResult,
        DurableAlbumStatus,
    )

    item = _qitem(
        album={
            "id": "album-1",
            "title": "Album",
            "artist": {"name": "Artist"},
            "tracks": {"items": [{"id": "track-1"}]},
        },
        album_dir=None,
        missing=[{"id": "track-1"}],
    )
    queue = [item]
    monkeypatch.setattr(executor, "plan_durable_new_album", lambda *_a: object())
    monkeypatch.setattr(
        executor,
        "_durable_execution_origin",
        lambda: (
            CompletionOrigin(CompletionOriginKind.CLI, "download-queue"),
            "cli:download-queue",
            None,
            None,
        ),
    )
    monkeypatch.setattr(executor, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(
        executor,
        "execute_durable_new_album",
        lambda *_a, **_kw: DurableAlbumResult(
            DurableAlbumStatus.RETRY,
            "safe-retry",
        ),
    )
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda *_a, **_kw: pytest.fail("legacy resolution was used"),
    )

    args = Namespace(
        dry_run=False,
        no_import=False,
        no_downsample=True,
        consolidate=False,
    )
    results, drained = executor._execute_download_queue(queue, args, token=None)

    assert queue == [item]
    assert drained is False
    assert results[0]["result"] == "retry"


def test_recovered_completion_is_saved_and_published_without_execution(
        monkeypatch, tmp_path):
    from qobuz_librarian.completion import (
        CompletionOrigin,
        CompletionOriginKind,
        RecoveryOwner,
    )
    from qobuz_librarian.queue import executor
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    album = {
        "id": "album-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [{"id": "track-1"}]},
    }
    item = _qitem(
        album=album,
        album_dir=None,
        missing=album["tracks"]["items"],
    )
    queue = [item]
    post_dir = tmp_path / "music" / "Artist" / "Album"
    post_dir.mkdir(parents=True)
    origin = CompletionOrigin(CompletionOriginKind.CLI, "download-queue")
    owner = RecoveryOwner("operation-1", "item-1")
    planned = queue_state._serialize_queue_item(item)
    order = []
    recovery_calls = 0

    monkeypatch.setattr(executor, "plan_durable_new_album", lambda *_a: object())
    monkeypatch.setattr(
        executor,
        "_durable_execution_origin",
        lambda: (origin, "cli:download-queue", None, None),
    )
    monkeypatch.setattr(
        executor,
        "_recovered_completion_details",
        lambda *_a, **_kw: (post_dir, 1, 0, 0),
    )
    monkeypatch.setattr(
        executor,
        "_recovered_owner_settled",
        lambda candidate: candidate == owner,
    )

    def fake_recovery(*, authority, acknowledge_completion):
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            order.append("recovery-start")
            assert acknowledge_completion(
                origin,
                owner,
                album_id="album-1",
                completion_hash="a" * 64,
                planned=planned,
                post_dir=str(post_dir),
            ) is True
            assert queue == []
            order.append("recovery-settled")
        return StartupRecoveryResult(StartupRecoveryStatus.CLEAR)

    monkeypatch.setattr(executor, "recover_startup_state", fake_recovery)
    monkeypatch.setattr(
        executor,
        "execute_durable_new_album",
        lambda *_a, **_kw: pytest.fail("download runner was used"),
    )
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda *_a, **_kw: pytest.fail("legacy resolution was used"),
    )
    monkeypatch.setattr(
        executor,
        "staging_preflight",
        lambda _args: order.append("preflight"),
    )
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(
        executor,
        "write_post_import_sidecars",
        lambda _dirs: None,
    )
    monkeypatch.setattr(executor, "log_fetch", lambda _payload: None)

    args = Namespace(
        dry_run=False,
        no_import=False,
        no_downsample=True,
        consolidate=False,
    )
    results, drained = executor._execute_download_queue(
        queue,
        args,
        token=None,
        on_progress=lambda: order.append("persisted"),
    )

    assert order.index("recovery-settled") < order.index("persisted")
    assert queue == [] and drained is True
    assert results[0]["dir"] == post_dir
    assert results[0]["result"] == "downloaded"
    assert results[0]["n_ok"] == 1


def test_executor_resumes_exact_saved_album_before_staging_preflight(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.completion import CompletionOriginKind
    from qobuz_librarian.queue import executor, startup_recovery
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.durable_runner import (
        DurableAlbumResult,
        DurableAlbumStatus,
    )

    staging = tmp_path / "staging"
    post_dir = tmp_path / "music" / "Artist" / "Album"
    staging.mkdir()
    post_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")

    album = {
        "id": "album-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [{"id": "track-1"}]},
    }
    item = _qitem(
        title="Album",
        album=album,
        album_dir=None,
        missing=album["tracks"]["items"],
        present=[],
    )
    queue = [item]
    saved = queue_state.save_queue_journal(
        queue_state.create_queue_journal(queue, mode="album_walk")
    )
    owner = (saved.operation_id, saved.items[0].item_id)
    plan = object()
    order = []
    captured = {}

    monkeypatch.setattr(
        executor,
        "recover_startup_state",
        startup_recovery.recover_startup_state,
    )
    monkeypatch.setattr(executor, "plan_durable_new_album", lambda *_a: plan)
    monkeypatch.setattr(
        executor,
        "staging_preflight",
        lambda _args: order.append("preflight"),
    )
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(
        executor,
        "_run_pre_import_hooks_for_dirs",
        lambda _dirs, _args, *, on_sources_changed: (
            order.append("hooks") or ([], 0)
        ),
    )

    def fake_durable(
            caller_queue, caller_item, _args, *, origin, mode, resume_owner,
            prepare_staged, **_kwargs):
        order.append("runner")
        captured.update(origin=origin, mode=mode, owner=resume_owner)
        prepare_staged(
            [staging / "Artist" / "Album"],
            lambda _kind: None,
        )
        caller_item.update(n_ok=1, n_fail=0, n_lossy=0, elapsed=2.0)
        caller_queue.remove(caller_item)
        queue_state.clear_queue_journal(
            resume_owner.operation_id,
            explicit_discard=True,
        )
        return DurableAlbumResult(
            DurableAlbumStatus.COMPLETE,
            post_dir=post_dir,
            operation_id=resume_owner.operation_id,
            item_id=resume_owner.item_id,
        )

    monkeypatch.setattr(executor, "execute_durable_new_album", fake_durable)
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda *_a, **_kw: pytest.fail("legacy resolution was used"),
    )
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "_resolve_signatures_to_paths", lambda *_a: [])
    monkeypatch.setattr(
        executor,
        "write_post_import_sidecars",
        lambda _dirs: None,
    )
    monkeypatch.setattr(executor, "log_fetch", lambda _payload: None)

    args = Namespace(
        dry_run=False,
        no_import=False,
        no_downsample=True,
        consolidate=False,
    )
    progress = []
    results, drained = executor._execute_download_queue(
        queue,
        args,
        token=None,
        on_progress=lambda: progress.append(list(queue)),
    )

    assert captured["mode"] == "album_walk"
    assert (captured["owner"].operation_id, captured["owner"].item_id) == owner
    assert captured["origin"].kind is CompletionOriginKind.CLI
    assert captured["origin"].reference == "download-queue"
    assert order[:2] == ["runner", "hooks"]
    assert order.index("runner") < order.index("preflight")
    assert progress and progress[-1] == []
    assert queue == [] and drained is True
    assert results[0]["dir"] == post_dir
    assert results[0]["result"] == "downloaded"


def test_executor_gap_fill_backup_survives_partial_beets_move(monkeypatch, tmp_path):
    """A clean download whose beets import moved only PART of the album into
    the library must not delete the gap-fill backup: the present tracks it
    holds aren't provably back until the folder holds the whole album
    (mirrors the expected-count gate in process.py)."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()
    # beets exited 0 and moved one fresh track in, leaving the rest in staging
    # — the album is 2 tracks on Qobuz but only 1 landed.
    (album_dir / "02 - fresh.flac").write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"},
                  "tracks": {"items": [{"id": 1}, {"id": 2}]}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gfb,
        "siblings_to_delete": [],
        "n_ok": 2, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert gfb.exists()
    assert (gfb / "01 - owned.flac").read_bytes() == b"the-owned-original"


def test_executor_gap_fill_backup_kept_when_extras_satisfy_the_count(monkeypatch, tmp_path):
    """The resolved folder holds ENOUGH audio files, but an expected track is
    still absent — extras make up the number. A raw file count reads that as
    whole and deletes the gap-fill backup, the moved-aside track's only copy;
    the gate has to match every expected track one-to-one."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - Alpha.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()
    # Two audio files land — the expected count (2) is satisfied — but the
    # moved-aside "Alpha" never came back; a bonus file makes up the number.
    (album_dir / "02 - Beta.flac").write_bytes(b"\x00" * 1000)
    (album_dir / "09 - Bonus.flac").write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"},
                  "tracks": {"items": [{"id": 1, "title": "Alpha"},
                                       {"id": 2, "title": "Beta"}]}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gfb,
        "siblings_to_delete": [],
        "n_ok": 2, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert gfb.exists()
    assert (gfb / "01 - Alpha.flac").read_bytes() == b"the-owned-original"


def test_executor_upgrade_backup_kept_when_companion_carry_fails(monkeypatch, tmp_path):
    """A verified upgrade whose booklet/scan copy-out fails must keep the
    backup — deleting it would take the only copy of the companions with it."""
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01 - new.flac").write_bytes(b"\x00" * 1000)
    bp = tmp_path / "backups" / "Artist - Album.upgrade"
    bp.mkdir(parents=True)
    (bp / "01 - old.flac").write_bytes(b"old-audio")
    (bp / "booklet.pdf").write_bytes(b"the-only-booklet")

    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr("qobuz_librarian.modes.process._upgrade_replacement_verified",
                        lambda *_a, **_k: True)
    monkeypatch.setattr("qobuz_librarian.modes.process.find_album_dir_filesystem",
                        lambda _a: album_dir)

    def _no_space(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr("shutil.copy2", _no_space)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir,
        "backup_path": bp,
        "gap_fill_backup_path": None,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert bp.exists()
    assert (bp / "booklet.pdf").read_bytes() == b"the-only-booklet"


def test_executor_keeps_a_changed_pre_download_sibling(monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    sibling = music / "Artist" / "Album (Deluxe)"
    sibling.mkdir(parents=True)
    (sibling / "02 - Beta.flac").write_bytes(b"the-only-copy")
    (album_dir / "01 - Alpha.flac").write_bytes(b"\x00" * 1000)
    (album_dir / "02 - Beta.flac").write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(executor.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    item = {
        "album": {"id": "A", "artist": {"name": "Artist"},
                  "tracks": {"items": [{"id": 1, "title": "Alpha"},
                                       {"id": 2, "title": "Beta"}]}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": None,
        "siblings_to_delete": [sibling],
        "n_ok": 2, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
    }

    executor._seal_queue_item_siblings(item)
    original = sibling.with_name("Album (Deluxe) - original")
    sibling.rename(original)
    sibling.mkdir()
    (sibling / "replacement.flac").write_bytes(b"replacement")
    args = Namespace(no_import=False, consolidate=False)
    result = executor._resolve_queue_item(item, args, imported_globally=True)

    assert (original / "02 - Beta.flac").read_bytes() == b"the-only-copy"
    assert (sibling / "replacement.flac").read_bytes() == b"replacement"
    assert result["siblings_preserved"] == [str(sibling)]


def test_executor_keeps_redundant_copies_when_replacement_cannot_be_sealed(
        monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01 - Song.flac").write_bytes(b"new")
    sibling = tmp_path / "music" / "Artist" / "Album (Old)"
    sibling.mkdir()
    (sibling / "01 - Song.flac").write_bytes(b"old sibling")
    gap_backup = tmp_path / "backups" / "gap"
    gap_backup.mkdir(parents=True)
    (gap_backup / "01 - Song.flac").write_bytes(b"old gap copy")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(
        executor, "folder_holds_all_tracks", lambda *_a, **_k: True)
    monkeypatch.setattr(executor, "capture_album_source_receipt", lambda _d: None)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"},
                  "tracks": {"items": [{"id": 1, "title": "Song"}]}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gap_backup,
        "siblings_to_delete": [sibling],
        "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": False,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert sibling.exists()
    assert gap_backup.exists()
