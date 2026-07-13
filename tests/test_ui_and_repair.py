"""Tests for the repair sweep/scanner and a few CLI entry points. The bulk of
the coverage here is the data-safety machinery around repair: truncated
originals are backed up before a re-rip, and the backup is only dropped once the
refills are proven back in place and re-verified — an outage or a still-short
re-rip must keep the backup rather than lose the only good copy.
"""
import os
import sqlite3
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs

# ── scan_dir_for_isrc_repairs: the truncation gates ────────────────────

def _track(isrc="GB1234567890", length=240.0, path="/music/track.flac", **kw):
    return {"isrc": isrc, "length": length, "title": "Track", "path": path,
            "sample_rate": 44100, "bits": 16, "channels": 2, "tracknumber": 1, **kw}


def test_scan_isrc_repairs_truncation_gates(tmp_path):
    # Both gates (duration mismatch + decode) must fire for a "verified truncated".
    source = tmp_path / "track.flac"
    source.write_bytes(b"held source")
    track = _track(length=169.0, path=str(source))
    qt = {"duration": 200.0, "title": "T", "track_number": 1}
    with patch("qobuz_librarian.repair_log._read_held_audio_meta", return_value=track), \
         patch("qobuz_librarian.repair_log._qobuz_track_by_isrc", return_value=qt):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1

    # Zero Qobuz duration → no reliable comparison → don't flag healthy files.
    with patch("qobuz_librarian.repair_log._read_held_audio_meta",
               return_value=_track(length=10.0, path=str(source))), \
         patch("qobuz_librarian.repair_log._qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "T", "track_number": 1}), \
         patch("qobuz_librarian.repair_log._flac_decode_ok", return_value=True):
        assert scan_dir_for_isrc_repairs(tmp_path, "token")["verified_ok"] == 1

    # No Qobuz duration BUT decode probe fails → flag corruption.
    bad = _track(length=0.0, path=str(source))
    with patch("qobuz_librarian.repair_log._read_held_audio_meta", return_value=bad), \
         patch("qobuz_librarian.repair_log._qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "T", "track_number": 1}), \
         patch("qobuz_librarian.repair_log._flac_decode_ok", return_value=False):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1


# ── Repair scan: resume from an interrupted sweep ──────────────────────

def test_repair_scan_receipt_refuses_a_replacement_before_backup(
        tmp_path, monkeypatch):
    from qobuz_librarian.library.backup import backup_gap_fill_files
    from qobuz_librarian.modes import repair

    music_root = tmp_path / "Music"
    album_dir = music_root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    source = album_dir / "01.flac"
    source.write_bytes(b"verified-truncated-source")
    monkeypatch.setattr(repair.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(repair.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    track = _track(length=100.0, path=str(source))
    qobuz_track = {
        "duration": 200.0,
        "title": "Track",
        "track_number": 1,
    }
    with patch(
        "qobuz_librarian.repair_log._read_held_audio_meta", return_value=track
    ), patch(
        "qobuz_librarian.repair_log._qobuz_track_by_isrc",
        return_value=qobuz_track,
    ), patch(
        "qobuz_librarian.repair_log.flac_audio_offset", return_value=0
    ), patch(
        "qobuz_librarian.repair_log._flac_decode_ok", return_value=False
    ):
        verified = scan_dir_for_isrc_repairs(
            album_dir, "token")["verified_truncated"]

    assert len(verified) == 1
    expected = repair._verified_repair_source_receipts(verified, album_dir)
    displaced = album_dir / "01.verified.flac"
    source.rename(displaced)
    source.write_bytes(b"unrelated-replacement")

    assert backup_gap_fill_files(
        [source], album_dir, expected_receipts=expected) is None
    assert source.read_bytes() == b"unrelated-replacement"
    assert displaced.read_bytes() == b"verified-truncated-source"
    assert not repair.cfg.UPGRADE_BACKUP_DIR.exists()


def test_repair_scan_refuses_same_name_replacement_during_qobuz_lookup(
        tmp_path, monkeypatch):
    import qobuz_librarian.repair_log as repair_log

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    source = album_dir / "01.flac"
    source.write_bytes(b"damaged source")
    displaced = album_dir / "01.displaced.flac"
    track = _track(length=100.0, path=str(source))

    monkeypatch.setattr(
        repair_log, "_read_held_audio_meta", lambda _source: track)
    monkeypatch.setattr(
        repair_log, "_flac_decode_ok", lambda *a, **k: True)

    def replace_during_lookup(_isrc, _token):
        source.rename(displaced)
        source.write_bytes(b"healthy same-name replacement")
        return {"duration": 200.0, "title": "Track", "track_number": 1}

    monkeypatch.setattr(
        repair_log, "_qobuz_track_by_isrc", replace_during_lookup)

    report = scan_dir_for_isrc_repairs(album_dir, "token", deep=True)

    assert report["verified_truncated"] == []
    assert report["unverified"] == 1
    assert source.read_bytes() == b"healthy same-name replacement"
    assert displaced.read_bytes() == b"damaged source"


def test_repair_scan_resumes_from_checkpoint(tmp_path, monkeypatch):
    """An interrupted repair sweep skips the artists already checked, restores
    the albums it flagged, and clears the checkpoint when it finishes cleanly."""
    from qobuz_librarian.library import scan_checkpoint
    from qobuz_librarian.web import flows
    monkeypatch.setattr("qobuz_librarian.config.SCAN_CHECKPOINT_FILE", tmp_path / "cp.json")

    flagged = {"kind": "repair", "title": "Old Album", "artist": "Artist A",
               "detail": "1 truncated track", "selected": True,
               "payload": {"album_dir": str(tmp_path / "Artist A" / "Old Album"),
                           "artist_name": "Artist A",
                           "verified_truncated": [{"path": "x.flac"}]}}
    scan_checkpoint.save("repair", {"Artist A"}, [flagged], {})

    (tmp_path / "Artist A").mkdir()
    (tmp_path / "Artist B" / "New Album").mkdir(parents=True)
    artists = [tmp_path / "Artist A", tmp_path / "Artist B"]

    class _Job:
        def __init__(self):
            self.candidates = []
            self.cancel_requested = False
        def add_candidate(self, **kw):
            self.candidates.append(dict(kw))
        def push_progress(self, *a, **k):
            pass
    job = _Job()

    checked = []
    def fake_scan(album_dir, token, deep=False):
        checked.append(album_dir.name)
        return {"verified_truncated": [], "verified_ok": 1, "no_isrc_tag": []}

    with patch.object(flows, "list_library_artists", return_value=artists), \
         patch.object(flows, "list_artist_album_dirs",
                      side_effect=lambda d: [p for p in d.iterdir() if p.is_dir()]), \
         patch.object(flows, "clear_scan_caches"), \
         patch("qobuz_librarian.repair_log.scan_dir_for_isrc_repairs", side_effect=fake_scan):
        flows.scan_repairs(job, "token")

    assert checked == ["New Album"]                                  # Artist A skipped
    assert any(c["title"] == "Old Album" for c in job.candidates)    # prior flag restored
    assert scan_checkpoint.load("repair") is None                    # cleared on clean finish


def test_no_isrc_redownload_failure_restores_original_folder(tmp_path, monkeypatch):
    from qobuz_librarian.library.backup import BackupResult
    from qobuz_librarian.web import flows
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    backup_dir = tmp_path / "backup"
    restored = {}
    monkeypatch.setattr(flows, "get_album", lambda *a: {"id": "x"})
    backup = BackupResult(
        backup_dir,
        complete=True,
        receipt={},
        requested=1,
        backed_up=1,
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.backup_album_dir", lambda d: backup)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": False, "n_ok": 0})
    monkeypatch.setattr("qobuz_librarian.library.backup.restore_upgrade_backup",
                        lambda bp, d: restored.update(bp=bp, dir=d) or True)
    res = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"}, "token")
    assert res["n_ok"] == 0
    assert restored == {"bp": backup, "dir": album_dir}


def test_no_isrc_redownload_keeps_originals_until_result_is_exact(
        tmp_path, monkeypatch):
    from qobuz_librarian.library.backup import BackupResult
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "01.flac").write_bytes(b"original")
    backup = BackupResult(
        backup_dir,
        complete=True,
        receipt={"kind": "upgrade"},
        requested=1,
        backed_up=1,
    )
    recoveries = []
    monkeypatch.setattr(flows, "get_album", lambda *a: {"id": "x"})
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.backup_album_dir",
        lambda _directory: backup,
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.dispose_backup",
        lambda *_args, **_kwargs: pytest.fail(
            "Repair must not delete the retained original album"),
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.pin_unverified_upgrade_backup",
        lambda *_args, **_kwargs: True,
    )

    def process_after_recovery(*_args, **_kwargs):
        assert recoveries and recoveries[-1].stage == "backup"
        return {"imported": True, "n_ok": 1, "n_fail": 0}

    monkeypatch.setattr(
        "qobuz_librarian.modes.process.process_album",
        process_after_recovery,
    )
    monkeypatch.setattr(
        "qobuz_librarian.modes.process._upgrade_replacement_verified",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "qobuz_librarian.modes.process._carry_non_audio_from_backup",
        lambda *_args: (album_dir, {"exact": "replacement"}),
    )

    result = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"},
        "token",
        recovery_checkpoint=lambda recovery: recoveries.append(recovery) or True,
    )

    assert result["repair_unverified"] is True
    assert backup_dir.is_dir()
    assert recoveries[-1].retained is True
    assert recoveries[-1].backup is backup


# ── Repair: relocate refilled tracks back to the album folder ─────────

def _repair_relocation_dirs(tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair

    music_root = tmp_path / "Music"
    album_dir = music_root / "Artist" / "First Fires (2013)"
    landed_dir = music_root / "Artist" / "The North Borders (2013)"
    album_dir.mkdir(parents=True)
    landed_dir.mkdir()
    monkeypatch.setattr(repair.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(repair.cfg, "BEETS_DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(
        repair, "_read_repair_isrc", lambda _fd: "GBCFB1300101")
    return repair, album_dir, landed_dir


def _receipt_identity(path):
    value = os.stat(path, follow_symlinks=False)
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "modified_ns": value.st_mtime_ns,
        "changed_ns": value.st_ctime_ns,
    }


def _sealed_import_receipt(
        root, files, album_scope, *, relatives=None, scope_relative=None,
        created_directories=None):
    relative_values = (
        relatives
        if relatives is not None
        else [path.relative_to(root).as_posix() for path in files]
    )
    scope_value = (
        scope_relative
        if scope_relative is not None
        else album_scope.relative_to(root).as_posix()
    )
    if created_directories is None:
        created_directories = [(scope_value, album_scope)]
    created_records = [
        {"relative": relative, **_receipt_identity(path)}
        for relative, path in created_directories
    ]
    return {
        "version": 1,
        "root": str(root),
        "root_identity": _receipt_identity(root),
        "sealed": True,
        "items": [
            {
                "relative": relative,
                "file": _receipt_identity(path),
                "album_scope": {
                    "relative": scope_value,
                    "directory": _receipt_identity(album_scope),
                },
                "created_directories": [
                    dict(record) for record in created_records
                ],
            }
            for path, relative in zip(files, relative_values)
        ],
    }


def test_repair_relocation_preserves_unowned_companions(tmp_path, monkeypatch):
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    source_disc = landed_dir / "Disc 2"
    source_disc.mkdir()
    refill = source_disc / "01 - First Fires.flac"
    refill.write_bytes(b"flac-bytes")
    booklet = landed_dir / "booklet.pdf"
    booklet.write_bytes(b"not-created-by-repair")
    preexisting = landed_dir / "Keep Empty"
    preexisting.mkdir()
    scope_relative = landed_dir.relative_to(
        repair.cfg.MUSIC_ROOT).as_posix()

    moved = repair._relocate_refilled_into_album_dir(
        album_dir,
        landed_dir,
        {"GBCFB1300101"},
        before_names=set(),
        ownership_receipt=_sealed_import_receipt(
            repair.cfg.MUSIC_ROOT,
            [refill],
            landed_dir,
            created_directories=[
                (scope_relative, landed_dir),
                (f"{scope_relative}/Disc 2", source_disc),
            ],
        ),
        expected_refills=1,
    )
    assert moved == 1
    assert (album_dir / "Disc 2" / refill.name).exists()
    assert not refill.exists()
    assert not source_disc.exists()
    assert booklet.read_bytes() == b"not-created-by-repair"
    assert preexisting.is_dir()
    assert landed_dir.is_dir()


def test_repair_relocation_refuses_a_symlinked_refill_folder(
        tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair

    music_root = tmp_path / "Music"
    album_dir = music_root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    refill = outside / "01.flac"
    refill.write_bytes(b"outside")
    landed_dir = music_root / "Artist" / "Refill"
    landed_dir.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(repair.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(repair.cfg, "BEETS_DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(
        repair, "_read_repair_isrc", lambda _fd: "GBCFB1300101")

    with pytest.raises(repair._RepairRelocationUncertain):
        repair._relocate_refilled_into_album_dir(
            album_dir,
            landed_dir,
            {"GBCFB1300101"},
            before_names=set(),
            ownership_receipt=_sealed_import_receipt(
                music_root,
                [refill],
                outside,
                relatives=["Artist/Refill/01.flac"],
                scope_relative="Artist/Refill",
            ),
            expected_refills=1,
        )
    assert refill.read_bytes() == b"outside"
    assert not (album_dir / refill.name).exists()


def test_repair_relocation_rolls_back_when_beets_rejects_the_path(
        tmp_path, monkeypatch):
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"flac-bytes")
    database = tmp_path / "library.db"
    old_path = b"Artist/The North Borders (2013)/01 - First Fires.flac"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB NOT NULL)")
        connection.execute("INSERT INTO items (path) VALUES (?)", (old_path,))
        connection.execute(
            "CREATE TRIGGER reject_repair BEFORE UPDATE OF path ON items "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    monkeypatch.setattr(repair.cfg, "BEETS_DB_PATH", database)

    with pytest.raises(OSError):
        repair._relocate_refilled_into_album_dir(
            album_dir,
            landed_dir,
            {"GBCFB1300101"},
            before_names=set(),
            ownership_receipt=_sealed_import_receipt(
                repair.cfg.MUSIC_ROOT, [refill], landed_dir),
            expected_refills=1,
        )
    assert refill.read_bytes() == b"flac-bytes"
    assert not (album_dir / refill.name).exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT path FROM items").fetchone()[0] == old_path


def test_repair_compensation_does_not_report_success_after_close_failure(
        monkeypatch):
    from qobuz_librarian.modes import repair

    class Cursor:
        def fetchone(self):
            return 1, b"old"

    class Connection:
        def execute(self, _sql, _parameters=None):
            return Cursor()

        def rollback(self):
            pass

    class Transaction:
        published = False
        durable = False
        uncertain = False

        def __init__(self, *_args, **_kwargs):
            pass

        def open(self):
            return Connection()

        def close(self):
            self.uncertain = True
            raise OSError("simulated cleanup failure")

    monkeypatch.setattr(repair, "AtomicSQLiteWrite", Transaction)
    monkeypatch.setattr(
        repair, "_require_no_migration_items_triggers", lambda _connection: None)
    monkeypatch.setattr(
        repair, "_require_migration_delete_journal_mode", lambda _connection: None)

    state, deferred = repair._compensate_repair_item_rows(
        {},
        ("id", "path"),
        {1: (1, b"old")},
        {1: (1, b"new")},
        lambda: True,
    )

    assert state == "unknown"
    assert isinstance(deferred, OSError)
    assert str(deferred) == "simulated cleanup failure"


def test_repair_relocation_restores_the_file_when_interrupted(
        tmp_path, monkeypatch):
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"flac-bytes")

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(repair, "_sync_repair_beets_row", interrupt)
    with pytest.raises(KeyboardInterrupt):
        repair._relocate_refilled_into_album_dir(
            album_dir,
            landed_dir,
            {"GBCFB1300101"},
            before_names=set(),
            ownership_receipt=_sealed_import_receipt(
                repair.cfg.MUSIC_ROOT, [refill], landed_dir),
            expected_refills=1,
        )
    assert refill.read_bytes() == b"flac-bytes"
    assert not (album_dir / refill.name).exists()


def test_repair_quarantines_the_exact_refill_before_moving_it(
        tmp_path, monkeypatch):
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"verified-refill")
    receipt = _sealed_import_receipt(
        repair.cfg.MUSIC_ROOT, [refill], landed_dir)
    rename_noreplace = repair._rename_noreplace_at
    raced = False

    def replace_before_quarantine(source_fd, source_name, destination_fd,
                                  destination_name):
        nonlocal raced
        if (
            not raced
            and source_fd == destination_fd
            and source_name == refill.name
            and destination_name.startswith(".qobuz-repair-source-")
        ):
            raced = True
            os.rename(
                source_name,
                "verified-refill.displaced",
                src_dir_fd=source_fd,
                dst_dir_fd=source_fd,
            )
            replacement_fd = os.open(
                source_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_fd,
            )
            try:
                os.write(replacement_fd, b"unrelated-replacement")
            finally:
                os.close(replacement_fd)
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(
        repair, "_rename_noreplace_at", replace_before_quarantine)
    with pytest.raises(repair._RepairRelocationUncertain):
        repair._relocate_refilled_into_album_dir(
            album_dir,
            landed_dir,
            {"GBCFB1300101"},
            before_names=set(),
            ownership_receipt=receipt,
            expected_refills=1,
        )

    assert raced is True
    assert refill.read_bytes() == b"unrelated-replacement"
    assert (landed_dir / "verified-refill.displaced").read_bytes() == b"verified-refill"
    assert not (album_dir / refill.name).exists()
    assert not any(
        path.name.startswith(".qobuz-repair-source-")
        for path in landed_dir.iterdir()
    )


def test_repair_refuses_a_replaced_preheld_album(tmp_path, monkeypatch):
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"verified-refill")
    receipt = _sealed_import_receipt(
        repair.cfg.MUSIC_ROOT, [refill], landed_dir)
    held_root = repair._HeldMusicRoot(repair.cfg.MUSIC_ROOT)
    held_album = repair._HeldRepairDirectory(
        held_root, repair._repair_relative_parts(held_root, album_dir))
    displaced = album_dir.with_name("First Fires.displaced")
    album_dir.rename(displaced)
    album_dir.mkdir()
    (album_dir / "keep.txt").write_text("replacement")
    try:
        with pytest.raises(repair._RepairRelocationUncertain):
            repair._relocate_refilled_into_album_dir(
                album_dir,
                landed_dir,
                {"GBCFB1300101"},
                before_names=set(),
                ownership_receipt=receipt,
                expected_refills=1,
                held_root=held_root,
                held_album=held_album,
            )
    finally:
        held_album.close()
        held_root.close()

    assert refill.read_bytes() == b"verified-refill"
    assert (album_dir / "keep.txt").read_text() == "replacement"
    assert not (displaced / refill.name).exists()


def test_empty_source_cleanup_restores_a_late_replacement(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import catalog

    parent = tmp_path / "parent"
    source = parent / "source"
    displaced = tmp_path / "displaced-source"
    source.mkdir(parents=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    rename_noreplace = catalog._rename_noreplace_at
    replaced = False

    def replace_before_cleanup(
            source_parent_fd, source_name,
            destination_parent_fd, destination_name):
        nonlocal replaced
        if destination_name.startswith(".qobuz-migrate-empty-") and not replaced:
            replaced = True
            source.rename(displaced)
            source.mkdir()
            (source / "user-file.txt").write_text("keep me", encoding="utf-8")
        return rename_noreplace(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(catalog, "_rename_noreplace_at", replace_before_cleanup)
    try:
        removed = catalog._remove_empty_migration_directory_at(
            parent_fd, "source", source_fd)
    finally:
        os.close(source_fd)
        os.close(parent_fd)

    assert replaced and removed is False
    assert (source / "user-file.txt").read_text(encoding="utf-8") == "keep me"
    assert displaced.is_dir()
    assert not list(parent.glob(".qobuz-migrate-empty-*"))


def test_empty_source_cleanup_reports_first_arrival_blocked_by_second(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import catalog

    parent = tmp_path / "parent"
    source = parent / "source"
    displaced = tmp_path / "displaced-source"
    source.mkdir(parents=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    rename_noreplace = catalog._rename_noreplace_at
    first_installed = False
    second_installed = False

    def race_cleanup(
            source_parent_fd, source_name,
            destination_parent_fd, destination_name):
        nonlocal first_installed, second_installed
        if (
            destination_name.startswith(".qobuz-migrate-empty-")
            and source_name == "source"
            and not first_installed
        ):
            first_installed = True
            source.rename(displaced)
            source.mkdir()
            (source / "first.txt").write_text("first", encoding="utf-8")
        elif (
            source_name.startswith(".qobuz-migrate-empty-")
            and destination_name == "source"
            and first_installed
            and not second_installed
        ):
            second_installed = True
            source.mkdir()
            (source / "second.txt").write_text("second", encoding="utf-8")
        return rename_noreplace(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(catalog, "_rename_noreplace_at", race_cleanup)
    try:
        with pytest.raises(catalog._MigrationEntryPreserved) as caught:
            catalog._remove_empty_migration_directory_at(
                parent_fd, "source", source_fd)
    finally:
        os.close(source_fd)
        os.close(parent_fd)

    assert first_installed and second_installed
    assert (source / "second.txt").read_text(encoding="utf-8") == "second"
    assert displaced.is_dir()
    preserved = Path(caught.value.location)
    assert (preserved / "first.txt").read_text(encoding="utf-8") == "first"


def test_refills_present_in_counts_duplicate_isrcs(tmp_path, monkeypatch):
    # Two truncated originals sharing one ISRC (a .1.flac collision pair, or the
    # same recording on two discs) both go to backup. The presence gate must
    # require BOTH back before the backup is trusted as redundant — a set-based
    # check passed when only one returned, deleting the backup and losing the
    # other file.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"GBCFB1300101": 2})

    # Only one file with the ISRC is back → not yet present.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "GBCFB1300101"}])
    assert repair._refills_present_in(tmp_path, wanted, Counter()) is False

    # Both back → present.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "GBCFB1300101"}, {"isrc": "gbcfb1300101"}])
    assert repair._refills_present_in(tmp_path, wanted, Counter()) is True


def test_refills_intact_requires_every_wanted_isrc_to_reverify(tmp_path, monkeypatch):
    # Before the truncated originals' backup is trusted as redundant, the rebuilt
    # folder is re-scanned and EVERY backed-up ISRC must positively re-verify.
    # Checking only "not flagged truncated" was unsafe: an ISRC whose re-lookup
    # transiently returned nothing lands in isrc_no_match, not verified_truncated,
    # so it would read as intact and the only good copy's backup would be deleted
    # while the refill is still short. Verified ISRCs come back in Qobuz's own
    # casing, so the gate normalizes them first.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"GBCFB1300101": 1, "USRC11700001": 1})

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs":
                                         Counter({"gbcfb1300101": 1, "USRC1-17-00001": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is True

    # One ISRC didn't re-verify → keep the backup.
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": Counter({"GBCFB1300101": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False


def test_refills_intact_counts_duplicate_isrcs_as_a_multiset(tmp_path, monkeypatch):
    # Two truncated files can share one ISRC (a .1.flac collision pair, or the
    # same recording on two discs). The presence gate already counts the
    # multiset; the intact gate must too — one verified-good twin must not
    # vouch for the other, still-truncated file and let the backup holding
    # both originals be deleted.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"GBCFB1300101": 2})

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": Counter({"gbcfb1300101": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": Counter({"gbcfb1300101": 2})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is True


def test_refills_intact_propagates_qobuz_outage(tmp_path, monkeypatch):
    # A token loss or Qobuz outage during re-verification must propagate, not
    # collapse to "still truncated" — an outage is not a verdict on the refill.
    from collections import Counter

    from qobuz_librarian.modes import repair

    wanted = Counter({"GBCFB1300101": 1})

    def raise_authlost(*a, **k):
        raise repair.AuthLost("token lost")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", raise_authlost)
    with pytest.raises(repair.AuthLost):
        repair._refills_intact(tmp_path, wanted, "tok", Counter())

    def raise_unavailable(*a, **k):
        raise repair.QobuzUnavailable("upstream down")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", raise_unavailable)
    with pytest.raises(repair.QobuzUnavailable):
        repair._refills_intact(tmp_path, wanted, "tok", Counter())


def test_refills_intact_keeps_backup_on_an_unexpected_rescan_error(tmp_path, monkeypatch):
    # Any non-outage failure of the re-scan stays conservative: return False so
    # the caller keeps the backup rather than delete originals on an error we
    # can't interpret.
    from collections import Counter

    from qobuz_librarian.modes import repair

    wanted = Counter({"GBCFB1300101": 1})

    def boom(*a, **k):
        raise ValueError("malformed scan result")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", boom)
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False


def test_repair_leaves_a_preexisting_track_sharing_the_recording_alone(tmp_path, monkeypatch):
    # A track that was already in the target dir's sibling album under the
    # same ISRC must NOT be moved — it isn't a refill, it's an existing copy.
    repair, album_dir, owned_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    owned = owned_dir / "01 - First Fires.flac"
    owned.write_bytes(b"already-here")
    refill = owned_dir / "02 - First Fires refill.flac"
    refill.write_bytes(b"receipt-owned-refill")

    moved = repair._relocate_refilled_into_album_dir(
        album_dir,
        owned_dir,
        {"GBCFB1300101"},
        before_names={"01 - First Fires.flac"},
        ownership_receipt=_sealed_import_receipt(
            repair.cfg.MUSIC_ROOT, [refill], owned_dir),
        expected_refills=1,
    )
    assert moved == 1 and owned.read_bytes() == b"already-here"
    assert not (album_dir / "01 - First Fires.flac").exists()
    assert (album_dir / refill.name).read_bytes() == b"receipt-owned-refill"


# ── CLI parse_args guards ───────────────────────────────────────────────

def _parse_argv(argv):
    import sys

    from qobuz_librarian.cli import parse_args
    with patch.object(sys, "argv", ["qobuz-librarian", *argv]):
        return parse_args()


def test_parse_args_rejects_incompatible_flag_combos():
    # Each of these combos silently dropped one side before — reject at parse.
    invalid = [
        ["--auto-safe", "Some Artist - Album"],
        ["--force", "--artist", "Radiohead"],
        ["--artist", ""],
        ["--artist", "   "],
        ["--no-catalog", "Some Artist - Album"],
        ["--include-comps", "--upgrade-walk"],
        ["--no-upgrade", "--upgrade-walk"],
        ["--include-singles", "--upgrade-walk"],
        ["--artist", "Radiohead", "--upgrade-walk"],
        ["--artist", "Four Tet", "some album"],
        ["--reset-walk-seen", "--artist", "Radiohead"],
        ["--reset-walk-seen", "Some Artist - Album"],
        ["--quiet"],
        # the local-only walk/migrate modes read none of these flags either
        ["--force", "--downsample-walk"],
        ["--include-singles", "--lyrics-walk"],
        ["--include-comps", "--migrate"],
        ["--no-catalog", "--lyrics-walk"],
    ]
    for argv in invalid:
        with pytest.raises(SystemExit):
            _parse_argv(argv)


# ── Repair: backup resolution branches (the core data-safety machinery) ─

def _call_repair_album_dir(tmp_path, monkeypatch, *, n_ok, n_fail, imported,
                           present=True, intact=True, recovery_checkpoint=None,
                           execute_calls=None, relocation_error=None):
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.library.backup import capture_gap_fill_source_receipt

    album_dir = tmp_path / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    # Parent-album resolution prefers the folder match; with none, it falls back
    # to the most-common ISRC album (get_album above). Stub it so the test stays
    # off the network and focused on backup resolution.
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir",
                        lambda *a, **k: None)

    def fake_execute(queue, args, token):
        if execute_calls is not None:
            execute_calls.append(queue)
        for qi in queue:
            qi["n_ok"] = n_ok
            qi["n_fail"] = n_fail
            qi["imported"] = imported

    monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute)
    def relocate(*_args, **_kwargs):
        if relocation_error is not None:
            raise relocation_error
        return 0

    monkeypatch.setattr(
        repair_mod, "_relocate_refilled_into_album_dir", relocate)
    monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)
    # The dummy file isn't a real FLAC, so drive the post-refill verification
    # gate directly: `present` = the refilled tracks returned to album_dir,
    # `intact` = the re-scan found them no longer truncated.
    monkeypatch.setattr(repair_mod, "_refills_present_in", lambda d, w, b: present)
    monkeypatch.setattr(repair_mod, "_refills_intact", lambda d, w, t, b: intact)

    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": capture_gap_fill_source_receipt(
               track, album_dir)}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    return repair_mod.repair_album_dir(
        album_dir,
        vt,
        "Artist",
        args,
        "tok",
        recovery_checkpoint=recovery_checkpoint,
    ), tmp_path


def _backup_files(tmp_path):
    root = tmp_path / "backups"
    return list(root.rglob("*")) if root.exists() else []


def test_repair_backup_is_retained_until_exact_result_can_be_proven(
        tmp_path, monkeypatch):
    # The current checks can verify a useful refill, but not the exact final
    # requested-track inventory, so the original remains available for review.
    result, p = _call_repair_album_dir(tmp_path / "ok", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=True)
    assert [f for f in _backup_files(p) if f.is_file()]
    assert result["n_ok"] == 0
    assert result["imported"] is False
    assert result["backup"] is not None

    # Re-downloaded but still truncated (a short re-rip passing the decode
    # gate): the originals' backup is KEPT, not deleted on presence alone, and
    # the repair isn't reported as a success.
    result, p = _call_repair_album_dir(tmp_path / "short", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=False)
    assert [f for f in _backup_files(p) if f.is_file()]
    assert result["n_ok"] == 0

    # Silent beets failure (downloads succeeded but import didn't, so nothing
    # returned to the folder): roll back to the pre-repair originals.
    result, p = _call_repair_album_dir(tmp_path / "silent", monkeypatch,
                                       n_ok=1, n_fail=0, imported=False,
                                       present=False)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert (p / "Artist" / "Album (2020)" / "01 - Track.flac").exists()
    assert result["imported"] is False


def test_repair_backup_kept_when_downloads_fail_and_skipped_when_backup_fails(tmp_path, monkeypatch):
    # Downloads fail → backup is preserved for manual recovery.
    _call_repair_album_dir(tmp_path / "kept", monkeypatch, n_ok=0, n_fail=1, imported=False)
    assert _backup_files(tmp_path / "kept")

    # Backup itself fails → original must NOT be queued for replacement.
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.library.backup import (
        BackupResult,
        capture_gap_fill_source_receipt,
    )
    album_dir = tmp_path / "nb" / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(repair_mod.cfg, "MUSIC_ROOT", tmp_path / "nb")
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir",
                        lambda *a, **k: None)
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    monkeypatch.setattr(
        repair_mod,
        "backup_gap_fill_files",
        lambda paths, d, **kwargs: None,
    )
    monkeypatch.setattr(repair_mod, "_execute_download_queue",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("must not run when backup fails")))
    vt = [{"path": str(track), "title": "Track 01",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": capture_gap_fill_source_receipt(
               track, album_dir)}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    res = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")
    assert track.exists() and res["n_fail"] == len(vt)

    partial_dir = tmp_path / "nb" / "backups" / "partial"

    def partial_backup(_paths, directory, **_kwargs):
        partial_dir.mkdir(parents=True)
        track.replace(partial_dir / track.name)
        return BackupResult(
            partial_dir,
            complete=False,
            receipt={"kind": "gap-fill", "origin": str(directory)},
            requested=2,
            backed_up=1,
        )

    def restore_partial(carried, directory):
        assert carried.path == partial_dir
        (partial_dir / track.name).replace(directory / track.name)
        partial_dir.rmdir()
        return 1

    monkeypatch.setattr(repair_mod, "backup_gap_fill_files", partial_backup)
    monkeypatch.setattr(repair_mod, "restore_gap_fill_backup", restore_partial)
    res = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")
    assert track.exists() and res["backup"] is None


def test_repair_will_not_refill_without_a_durable_recovery_checkpoint(
        tmp_path, monkeypatch):
    seen = []
    execute_calls = []

    result, root = _call_repair_album_dir(
        tmp_path,
        monkeypatch,
        n_ok=1,
        n_fail=0,
        imported=True,
        recovery_checkpoint=lambda recovery: seen.append(recovery) or False,
        execute_calls=execute_calls,
    )

    retained = [recovery for recovery in seen if recovery.retained]
    assert len(retained) == 1
    assert retained[0].backup.receipt is not None
    assert retained[0].backup.backed_up == 1
    assert seen[-1].retained is False
    assert execute_calls == []
    assert (root / "Artist" / "Album (2020)" / "01 - Track.flac").exists()
    assert result["backup"] is None


def test_repair_persists_a_preserved_refill_location(
        tmp_path, monkeypatch):
    import qobuz_librarian.modes.repair as repair_mod

    preserved = tmp_path / "kept-refill"
    preserved.mkdir()
    (preserved / "first.txt").write_text("first", encoding="utf-8")
    seen = []
    error = repair_mod._RepairRelocationUncertain(
        "refill cleanup needs review",
        recovery_location=preserved,
    )

    result, _root = _call_repair_album_dir(
        tmp_path,
        monkeypatch,
        n_ok=1,
        n_fail=0,
        imported=True,
        recovery_checkpoint=lambda recovery: seen.append(recovery) or True,
        relocation_error=error,
    )

    placement = [recovery for recovery in seen if recovery.stage == "placement"]
    assert len(placement) == 1
    record = placement[0].as_record()
    assert str(preserved) in record["reason"]
    assert "/proc/self/fd/" not in record["reason"]
    assert (preserved / "first.txt").read_text(encoding="utf-8") == "first"
    assert result["backup"] is not None


# ── Walk-seen state: crash-safe atomic write ───────────────────────────

def test_walk_seen_records_idempotently_and_survives_a_crashed_rename(tmp_path, monkeypatch):
    import qobuz_librarian.modes.walk as walk_mod
    from qobuz_librarian.modes.walk import load_walk_seen, record_walk_seen
    f = tmp_path / "walk_seen.txt"
    monkeypatch.setattr("qobuz_librarian.config.WALK_SEEN_FILE", f)
    record_walk_seen("Radiohead")
    record_walk_seen("Radiohead")  # idempotent
    assert "radiohead" in load_walk_seen()
    prior = f.read_bytes()

    # If os.replace fails the file must not be half-written.
    monkeypatch.setattr(walk_mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("crashed")))
    record_walk_seen("Portishead")
    assert f.read_bytes() == prior
    assert load_walk_seen() == {"radiohead"}


# ── Scan-report-repair classifications ──────────────────────────────────

def _call_scan_report(tmp_path, monkeypatch, *, repair_result=None,
                     verified_truncated=None, yes=True, input_return="y"):
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.modes.repair import _scan_report_repair
    album_dir = tmp_path / "Artist" / "Album (2022)"
    album_dir.mkdir(parents=True)
    (album_dir / "01 Track.flac").write_bytes(b"\x00" * 200)
    if verified_truncated is None:
        verified_truncated = [{"path": str(album_dir / "01 Track.flac"),
                                "title": "Track 01", "isrc": "USRC12345678",
                                "track_number": 1, "file_length": 5.0,
                                "qobuz_duration": 180.0,
                                "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "A1"}}}]
    monkeypatch.setattr(repair_mod, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_truncated": verified_truncated,
                                         "verified_ok": 0, "isrc_no_match": [], "no_isrc_tag": []})
    if repair_result is not None:
        monkeypatch.setattr(repair_mod, "repair_album_dir", lambda *a, **k: repair_result)
    monkeypatch.setattr(repair_mod, "section", lambda *a: None)
    args = Namespace(force=False, yes=yes, prefer_hires=False, consolidate=False, no_upgrade=False)
    with patch("builtins.input", return_value=input_return):
        return _scan_report_repair(album_dir, "Artist", args, "tok")


def test_scan_report_classifies_repair_outcomes(tmp_path, monkeypatch):
    from qobuz_librarian.library.backup import BackupResult

    # Repair succeeds → "repaired".
    assert _call_scan_report(tmp_path / "ok", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": True, "backup": None}) == "repaired"
    # Downloads succeeded but beets failed silently → classified as failure.
    assert _call_scan_report(tmp_path / "silent", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": False, "backup": None}) == "failed"
    recovery = BackupResult(
        tmp_path / "kept-originals",
        complete=False,
        receipt={"kind": "gap-fill"},
        requested=2,
        backed_up=1,
    )
    assert _call_scan_report(
        tmp_path / "recovery",
        monkeypatch,
        repair_result={
            "n_ok": 0,
            "n_fail": 1,
            "imported": False,
            "backup": recovery,
        },
    ) == "recovery"
    # Nothing truncated → "clean".
    assert _call_scan_report(tmp_path / "clean", monkeypatch, verified_truncated=[]) == "clean"
    # User declines the prompt → "skipped".
    assert _call_scan_report(tmp_path / "skip", monkeypatch,
                             yes=False, input_return="n") == "skipped"


def test_execute_repairs_does_not_count_an_unverified_redownload_as_repaired(monkeypatch):
    # A whole-album re-download that imported but failed the completeness check
    # keeps the backup and must not render "Repaired 1/1" — the active copy is
    # an unverified, possibly incomplete replacement.
    from qobuz_librarian.web import flows

    class _Job:
        cancel_requested = False
        _progress_scope = None
        _imported_any = False
        summary = ""
        error = ""

        def push_progress(self, *a, **k):
            pass

    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "build_args", lambda: Namespace())
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *a, **k: None)
    callback_seen = False

    def unverified_redownload(_payload, _token, *, recovery_checkpoint=None):
        nonlocal callback_seen
        assert callable(recovery_checkpoint)
        callback_seen = True
        return {"imported": True, "n_ok": 8,
                "n_fail": 0, "repair_unverified": True}

    monkeypatch.setattr(
        flows, "_redownload_damaged_album", unverified_redownload)
    monkeypatch.setattr(flows.time, "sleep", lambda _s: None)

    job = _Job()
    chosen = [{"kind": "redownload", "title": "Album",
               "payload": {"artist_name": "Artist", "album_dir": "/x"}}]
    flows.execute_repairs(job, chosen, "tok")

    assert callback_seen
    assert "Repaired 0/1" in job.summary
    assert job.error


def test_refill_gates_require_refills_on_top_of_the_baseline(tmp_path, monkeypatch):
    # A healthy PRE-EXISTING file sharing the wanted ISRC (a twin on another
    # disc that was never truncated) must not vouch for a refill that never
    # came back — both gates count against the post-backup baseline, and an
    # unreadable baseline (None) is unverifiable, never a pass.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"USRC11111111": 1})
    baseline = Counter({"USRC11111111": 1})

    # Only the healthy twin is on disk; the refill is absent.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "USRC11111111"}])
    assert repair._refills_present_in(tmp_path, wanted, baseline) is False
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs":
                                         Counter({"USRC11111111": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", baseline) is False

    # Twin plus the returned refill: both gates clear.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "USRC11111111"},
                                   {"isrc": "USRC11111111"}])
    assert repair._refills_present_in(tmp_path, wanted, baseline) is True
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs":
                                         Counter({"USRC11111111": 2})})
    assert repair._refills_intact(tmp_path, wanted, "tok", baseline) is True

    assert repair._refills_present_in(tmp_path, wanted, None) is False
    assert repair._refills_intact(tmp_path, wanted, "tok", None) is False


def test_backup_sources_keep_both_same_isrc_originals(tmp_path):
    # Two originals can share an ISRC with distinct disc/track tags and art;
    # collapsing them to one path stamps one twin's metadata onto both refills
    # and lets the "successful" repair delete the other's only copy.
    from qobuz_librarian.modes import repair

    album = tmp_path / "Album"
    (album / "CD 2").mkdir(parents=True)
    bk = tmp_path / "bk"
    (bk / "CD 2").mkdir(parents=True)
    (bk / "01 - Song.flac").write_bytes(b"a")
    (bk / "CD 2" / "01 - Song.flac").write_bytes(b"b")
    vt = [{"isrc": "USRC11111111", "path": str(album / "01 - Song.flac")},
          {"isrc": "USRC11111111", "path": str(album / "CD 2" / "01 - Song.flac")}]

    out = repair._backup_source_by_isrc(vt, album, bk)
    assert out == {"USRC11111111": [bk / "01 - Song.flac",
                                    bk / "CD 2" / "01 - Song.flac"]}


def test_retag_marks_an_unconsumed_twin_source_failed(tmp_path, monkeypatch):
    # Two same-ISRC originals but only one refill surfaced in staging: one
    # original's tags/art never landed anywhere, so the ISRC must read as
    # failed and the backup (their only copy) kept.
    from qobuz_librarian.modes import repair

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "01 - Song.flac").write_bytes(b"x")
    monkeypatch.setattr(repair, "_FLAC", lambda fp: {"isrc": ["USRC11111111"]})
    monkeypatch.setattr(repair, "_snapshot_flac_metadata", lambda src: {"of": str(src)})
    monkeypatch.setattr(repair, "_restore_flac_metadata", lambda fp, snap: True)

    sources = {"USRC11111111": [tmp_path / "a.flac", tmp_path / "b.flac"]}
    failed = repair._retag_refills_in_staging([staged], sources)
    assert failed == {"USRC11111111"}


def test_retag_callback_records_total_failure_on_exception(monkeypatch):
    # The executor catches and logs a retag exception, so the carry state is
    # unknown to the backup resolution — every source must already be marked
    # failed, or the empty set reads as "all tags carried" and the only copy
    # of the originals' metadata is deleted.
    from pathlib import Path

    from qobuz_librarian.modes import repair

    failed = set()
    sources = {"ISRC1": [Path("/x")], "ISRC2": [Path("/y")]}

    def boom(_dirs, _sources):
        raise RuntimeError("tag write exploded")
    monkeypatch.setattr(repair, "_retag_refills_in_staging", boom)

    cb = repair._make_retag_callback(sources, failed)
    with pytest.raises(RuntimeError):
        cb([Path("/staged")])
    assert failed == {"ISRC1", "ISRC2"}


def test_repair_pins_the_backup_when_the_tag_carry_fails(tmp_path, monkeypatch):
    # Audio verifiably repaired but the originals' tags couldn't be carried:
    # the backup is kept AND pinned — the age sweep proves redundancy by
    # same-path same-or-larger bytes, which the refill satisfies, so without
    # the pin the only copy of those tags is reaped on schedule.
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.library.backup import (
        _UNVERIFIED_UPGRADE_SENTINEL,
        capture_gap_fill_source_receipt,
    )

    album_dir = tmp_path / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(repair_mod.cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir", lambda *a, **k: None)

    def fake_execute(queue, args, token):
        for qi in queue:
            qi["n_ok"] = 1
            qi["n_fail"] = 0
            qi["imported"] = True
            retag = qi.get("pre_import_retag")
            if callable(retag):
                # No staged refill carries any tags — the whole carry fails.
                retag([])

    monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(
        repair_mod, "_relocate_refilled_into_album_dir", lambda *a, **k: 0)
    monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)
    monkeypatch.setattr(repair_mod, "_refills_present_in", lambda d, w, b: True)
    monkeypatch.setattr(repair_mod, "_refills_intact", lambda d, w, t, b: True)

    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": capture_gap_fill_source_receipt(
               track, album_dir)}]
    args = Namespace(force=False, yes=True, prefer_hires=False,
                     consolidate=False, no_upgrade=False)
    repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")

    backups = tmp_path / "backups"
    pins = list(backups.rglob(_UNVERIFIED_UPGRADE_SENTINEL))
    assert pins, "the kept backup must carry a never-reap pin"
    kept = list(backups.rglob("01 - Track.flac"))
    assert kept and kept[0].read_bytes() == b"\x00" * 200


def test_strict_confirm_reasks_on_a_typo(monkeypatch):
    # The downsample keep-vs-delete answer is SAVED as the standing default,
    # so a typo must not read as "delete the originals from now on" — strict
    # mode re-asks until it gets a real yes or no. Non-strict prompts keep
    # the old contract (anything not yes is No).
    from qobuz_librarian.ui_cli import prompts

    answers = iter(["maybe", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert prompts.confirm("Keep?", default_yes=True, strict=True) is True

    answers = iter(["whatever", "n"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert prompts.confirm("Keep?", default_yes=True, strict=True) is False

    monkeypatch.setattr("builtins.input", lambda _p: "maybe")
    assert prompts.confirm("Keep?", default_yes=True) is False
