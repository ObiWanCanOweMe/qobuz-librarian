"""Tests for queue/builder.py and queue/persistence.py."""
import errno
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.persistence import (
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
    save_pending_queue([_qitem(title="Album A")], mode="album_walk")
    items, mode, saved_at = load_pending_queue()
    assert len(items) == 1 and items[0]["album"]["title"] == "Album A"
    assert mode == "album_walk"
    datetime.fromisoformat(saved_at)        # saved_at is valid ISO
    clear_pending_queue()
    assert not qfile.exists()


def test_pending_queue_rejects_bad_payloads(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
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
    save_pending_queue([_qitem()], mode="walk_queue")
    monkeypatch.setattr("qobuz_librarian.queue.executor._execute_download_queue",
                        lambda items, args, token, **kw: ([], False))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    offer_resume_pending_queue(Namespace(), "tok")
    assert qfile.exists()   # albums left to retry must survive the resume


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

    assert qfile.exists()
    assert retry_file.exists()


def test_executor_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    """Queue-mode gap-fill backs up present tracks before re-ripping."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "02 - kept.flac").write_bytes(b"\x00" * 1000)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gfb,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 1,
        "auto_upgrade": False,
    }
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
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
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert item["_resolved_post_dir"] == final_dir
    assert marked == [final_dir]


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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
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
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert item["_resolved_post_dir"] == final_dir
    assert marked == [final_dir]


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
                     migrate_multi_artist=False, consolidate=False)
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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)

    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: False)
    executor._resolve_queue_item(item, args, imported_globally=True)
    assert backup.exists()                       # unverified → backup kept

    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)
    item["backup_path"] = backup
    executor._resolve_queue_item(item, args, imported_globally=True)
    assert not backup.exists()                   # verified → backup cleared


def test_executor_upgrade_carries_non_audio_companions_from_backup(monkeypatch, tmp_path):
    # Regression: the bulk/web upgrade path (this executor) must carry non-audio
    # companions — booklets, scans, .cue/.log, hand-placed art — out of the backup
    # into the rebuilt album before reaping it, exactly as the single-album
    # process.py path does. The audio-only completeness gate ignores them, so
    # without the carry they'd be destroyed with the backup on every upgrade.
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")            # the upgraded audio
    backup = tmp_path / "backups" / "Album.bak"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"old")               # old audio (not carried)
    (backup / "booklet.pdf").write_bytes(b"the-booklet")   # user companion
    (backup / "scans").mkdir()
    (backup / "scans" / "front.jpg").write_bytes(b"art")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    # Backup reaped, but its non-audio companions carried into the live folder;
    # the upgraded audio is left untouched (the old copy is not carried back).
    assert not backup.exists()
    assert (album_dir / "booklet.pdf").read_bytes() == b"the-booklet"
    assert (album_dir / "scans" / "front.jpg").read_bytes() == b"art"
    assert (album_dir / "01.flac").read_bytes() == b"new"


def test_executor_per_album_isolation_one_album_failure_keeps_others(monkeypatch, tmp_path):
    """The whole point of the per-album pipeline: a beets failure on
    album N leaves albums 1..N-1 already imported and N+1..end still
    importable, instead of taking the whole batch down. The failing album's
    staged dir is parked under BEETS_RETRY_DIR for an import-only retry, and
    every attempted album drops out of the queue — none get re-downloaded."""
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
                     migrate_multi_artist=False, consolidate=False)
    results, drained = executor._execute_download_queue(items, args, token=None)

    assert seen == ["A", "B", "C"]
    assert [r["imported"] for r in results] == [True, False, True]
    # B's staged folder got parked; A and C's are still where the test left
    # them (beets would have moved them in real life, but we stubbed it out).
    assert not (staging / "Artist-B" / "Album B").exists()
    parked = list((staging / cfg.BEETS_RETRY_DIR).rglob("Album B"))
    assert len(parked) == 1
    # All three landed audio, so all three leave the queue: B recovers by
    # re-importing the parked copy, not by re-downloading. Nothing to resume.
    assert items == []
    assert drained is True


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
                     migrate_multi_artist=False, consolidate=False)
    results, drained = executor._execute_download_queue(
        queue, args, token=None, on_progress=lambda: saves.append(list(queue)))

    assert queue == [nothing]      # imported dropped, the empty download kept
    assert drained is False
    assert len(results) == 2       # results stay 1:1 with the items passed in
    assert saves                   # progress persisted as the item dropped


def test_reimport_parked_albums_clears_moved_and_keeps_skipped(monkeypatch, tmp_path):
    """A parked album is cleared only when its audio actually leaves disk on the
    retry import. A beets run that exits 0 while skipping the album (e.g. a
    library duplicate) leaves the files in place — the parked copy must be kept,
    not deleted on the strength of the exit code, since it's the only copy."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    good = staging / cfg.BEETS_RETRY_DIR / "20260101_000000-good"
    skipped = staging / cfg.BEETS_RETRY_DIR / "20260101_000001-skipped"
    (good / "Good Album").mkdir(parents=True)
    (skipped / "Dup Album").mkdir(parents=True)
    good_flac = good / "Good Album" / "01.flac"
    skipped_flac = skipped / "Dup Album" / "01.flac"
    good_flac.write_bytes(b"flac")
    skipped_flac.write_bytes(b"flac")

    def fake_import(dirs):
        # beets moves audio into the library on a real import; simulate that for
        # the good album and leave the skipped one's files where they are.
        if "good" in str(dirs[0]):
            good_flac.unlink()
        return "ok"  # exit 0 either way — the disk, not this, decides cleanup
    monkeypatch.setattr(executor, "beets_import_albums", fake_import)

    assert executor._reimport_parked_albums()[0] is True
    assert not good.exists()           # audio moved out → parking dir cleared
    assert skipped.exists()            # files remain → kept parked, not deleted
    assert skipped_flac.exists()       # the only copy of the skipped track survives


def test_reimport_parked_albums_preserves_non_audio_companions(monkeypatch, tmp_path):
    """When beets imports the audio out of a parked album, the non-audio
    companions it leaves behind (booklets, scans) must be rescued, not deleted
    with the staging husk — the same data-loss shape as the upgrade-backup path."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    data = tmp_path / "data"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    parked = staging / cfg.BEETS_RETRY_DIR / "20260101_000000-grp"
    album = parked / "Some Album"
    album.mkdir(parents=True)
    flac = album / "01.flac"
    booklet = album / "booklet.pdf"
    flac.write_bytes(b"flac")
    booklet.write_bytes(b"%PDF-1.4 booklet")

    def fake_import(dirs):
        flac.unlink()   # beets moves only the audio out, like a real import
        return "ok"
    monkeypatch.setattr(executor, "beets_import_albums", fake_import)

    assert executor._reimport_parked_albums()[0] is True
    assert not parked.exists()                       # husk cleared
    rescued = data / "import_leftovers" / "20260101_000000-grp" / "Some Album" / "booklet.pdf"
    assert rescued.exists()                          # booklet rescued, not deleted
    assert rescued.read_bytes() == b"%PDF-1.4 booklet"


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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
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
    args = Namespace(dry_run=False, no_import=False, migrate_multi_artist=False,
                     consolidate=False)
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


def test_executor_gap_fill_backup_survives_partial_beets_move(monkeypatch, tmp_path):
    """A clean download whose beets import moved only PART of the album into
    the library must not delete the gap-fill backup: the present tracks it
    holds aren't provably back until the folder holds the whole album
    (mirrors the expected-count gate in process.py)."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()
    # beets exited 0 and moved one fresh track in, leaving the rest in staging
    # — the album is 2 tracks on Qobuz but only 1 landed.
    (album_dir / "02 - fresh.flac").write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

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
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

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
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert gfb.exists()
    assert (gfb / "01 - Alpha.flac").read_bytes() == b"the-owned-original"


def test_reimport_keeps_husk_when_companion_move_fails(monkeypatch, tmp_path):
    """beets moved a parked album's audio out, but its booklet can't be moved
    to the leftovers dir — the husk (and the group) must survive, because the
    recursive delete would take down exactly the file the move failed on."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    retry = staging / cfg.BEETS_RETRY_DIR / "Group"
    husk = retry / "Album"
    husk.mkdir(parents=True)
    booklet = husk / "booklet.pdf"
    booklet.write_bytes(b"the-only-booklet")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")

    monkeypatch.setattr(executor, "_import_album_with_retry", lambda dirs: True)
    monkeypatch.setattr(executor, "track_signatures_for_album_dirs", lambda _d: [])
    monkeypatch.setattr(executor, "find_album_dir_by_track_signatures", lambda _s: None)

    def _refused(*_a, **_k):
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(executor.shutil, "move", _refused)

    executor._reimport_parked_albums()

    assert booklet.exists()
    assert booklet.read_bytes() == b"the-only-booklet"


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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
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
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert bp.exists()
    assert (bp / "booklet.pdf").read_bytes() == b"the-only-booklet"


def test_parked_cleanup_distrusts_a_partial_walk(monkeypatch, tmp_path):
    """An unreadable subtree can hide audio or the only companion; the husk
    delete that follows these answers must treat "couldn't see everything" as
    "something may remain"."""
    from qobuz_librarian.queue import executor

    d = tmp_path / "parked"
    d.mkdir()

    def partial(root, errors=None):
        if errors is not None:
            errors.append(OSError("subdir: EIO"))
        return iter(())

    monkeypatch.setattr(executor, "iter_tree_no_symlinks", partial)
    assert executor._dir_has_audio(d) is True
    assert executor._parked_companions(d) is None


def test_executor_keeps_siblings_unless_the_filled_folder_is_whole(monkeypatch, tmp_path):
    """The user picked a canonical folder from a duplicate group; the others
    are deleted only "on successful fill". beets reports success when it moved
    ANY audio, so a partial import with clean download flags must not delete a
    sibling — it may hold the only copy of what's missing."""
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    sibling = tmp_path / "music" / "Artist" / "Album (Deluxe)"
    sibling.mkdir(parents=True)
    (sibling / "02 - Beta.flac").write_bytes(b"the-only-copy")
    # Only one of the two expected tracks actually landed in the folder.
    (album_dir / "01 - Alpha.flac").write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

    def item():
        return {
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

    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item(), args, imported_globally=True)
    assert sibling.exists()
    assert (sibling / "02 - Beta.flac").read_bytes() == b"the-only-copy"

    # Once the folder verifiably holds every expected track, the promised
    # deletion goes through.
    (album_dir / "02 - Beta.flac").write_bytes(b"\x00" * 1000)
    executor._resolve_queue_item(item(), args, imported_globally=True)
    assert not sibling.exists()


def test_executor_keeps_redundant_copies_until_replacement_is_durable(
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
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(executor, "folder_holds_all_tracks", lambda *_a: True)
    monkeypatch.setattr(executor, "replacement_tree_durable", lambda _d: False)

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
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert sibling.exists()
    assert gap_backup.exists()
