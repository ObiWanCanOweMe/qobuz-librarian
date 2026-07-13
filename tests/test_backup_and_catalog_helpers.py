"""Tests for backup/restore safety and consolidation helpers."""
import errno
import os
import shutil
import signal
import stat

import pytest

from qobuz_librarian.library.backup import (
    backup_album_dir,
    backup_gap_fill_files,
    cleanup_old_upgrade_backups,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_librarian.modes.consolidate import (
    execute_consolidation,
    match_sibling_track,
)


def _need_audio_tools():
    if not (shutil.which("ffmpeg") and shutil.which("flac")):
        pytest.skip("ffmpeg/flac not available")


def _real_flac(path, *, seconds=2):
    """Encode a short white-noise FLAC that actually decodes with ``flac -t``."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=duration={seconds}:color=white:amplitude=0.5",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
         str(path)], check=True)


def _seal_test_backup(bk, path, origin, *, kind="gap-fill", complete=True):
    """Give a hand-built fixture the same carried receipt production uses."""
    descriptor = bk._open_backup_directory(path)
    try:
        if bk._named_entry_missing(descriptor, bk._ORIGIN_SIDECAR):
            assert bk._write_backup_origin_durable(descriptor, origin)
        manifest = bk._tree_manifest(descriptor)
        assert manifest is not None
        result = bk._seal_backup_result(
            path,
            descriptor,
            origin,
            kind=kind,
            complete=complete,
            requested=len(manifest),
            backed_up=len(manifest),
        )
        assert result.receipt is not None
        return result
    finally:
        os.close(descriptor)


def test_receipt_cleanup_preserves_a_replacement_arriving_during_move(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    backup = tmp_path / "backup"
    backup.mkdir()
    receipt = backup / bk._RECEIPT_SIDECAR
    receipt.write_bytes(b"app receipt")
    real_rename = bk._rename_noreplace_at
    replaced = False

    def replace_before_move(source_fd, source_name, destination_fd,
                            destination_name):
        nonlocal replaced
        if source_name == bk._RECEIPT_SIDECAR and not replaced:
            replaced = True
            os.rename(
                source_name,
                "original-receipt",
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
                os.write(replacement_fd, b"late user file")
            finally:
                os.close(replacement_fd)
        return real_rename(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(bk, "_rename_noreplace_at", replace_before_move)
    directory_fd = bk._open_backup_directory(backup)
    try:
        assert bk._remove_written_receipt(directory_fd) is False
    finally:
        os.close(directory_fd)

    assert receipt.read_bytes() == b"late user file"
    assert (backup / "original-receipt").read_bytes() == b"app receipt"
    assert not list(backup.glob(".ql-receipt-remove-*"))


def test_held_snapshot_cleanup_releases_every_file_after_one_close_failure():
    import qobuz_librarian.library.backup as bk

    closed = []

    class Lease:
        def __init__(self, name, *, fail=False):
            self.name = name
            self.fail = fail

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise OSError("injected close failure")

    descriptors = [os.open(os.devnull, os.O_RDONLY) for _ in range(4)]
    held = {
        "first.flac": {
            "lease": Lease("first"),
            "descriptor": descriptors[0],
            "parents": [descriptors[1]],
        },
        "second.flac": {
            "lease": Lease("second", fail=True),
            "descriptor": descriptors[2],
            "parents": [descriptors[3]],
        },
    }

    with pytest.raises(OSError, match="injected close failure"):
        bk._release_held_snapshot_files(held)

    assert closed == ["second", "first"]
    assert held == {}
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_snapshot_hold_releases_adopted_file_when_lease_refuses(
        monkeypatch):
    import qobuz_librarian.library.backup as bk

    descriptors = [os.open(os.devnull, os.O_RDONLY) for _ in range(2)]
    closed = []

    class RefusingLease:
        def intact(self):
            return False

        def close(self):
            closed.append(True)
            raise OSError("injected close failure")

    expected = {"sha256": "unused"}
    monkeypatch.setattr(
        bk,
        "_open_tree_file",
        lambda _root, _relative: (
            [descriptors[0]], descriptors[0], descriptors[1]),
    )
    monkeypatch.setattr(
        bk, "acquire_inode_write_exclusion", lambda _fd: RefusingLease())
    monkeypatch.setattr(bk, "_snapshot_file", lambda *_args: expected)
    held = {}

    with pytest.raises(OSError, match="active or uncertain writer"):
        bk._hold_snapshot_files(
            -1, {"files": {"track.flac": expected}}, held)

    assert closed == [True]
    assert held == {}
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_snapshot_resource_handoff_defers_real_sigint():
    import qobuz_librarian.library.backup as bk

    adopted = []
    observed = []
    previous = signal.getsignal(signal.SIGINT)

    def observe(_signum, _frame):
        observed.append(tuple(adopted))

    signal.signal(signal.SIGINT, observe)
    try:
        def adopt():
            signal.raise_signal(signal.SIGINT)
            adopted.append("owned")

        bk._run_backup_sigint_deferred(adopt)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert observed == [("owned",)]


def test_snapshot_release_defers_real_sigint_until_every_resource_is_closed():
    import qobuz_librarian.library.backup as bk

    descriptor = os.open(os.devnull, os.O_RDONLY)
    observed = []
    previous = signal.getsignal(signal.SIGINT)

    class InterruptingLease:
        def close(self):
            signal.raise_signal(signal.SIGINT)

    held = {
        "track.flac": {
            "lease": InterruptingLease(),
            "descriptor": descriptor,
            "parents": [],
        },
    }

    def observe(_signum, _frame):
        with pytest.raises(OSError):
            os.fstat(descriptor)
        observed.append(held.copy())

    signal.signal(signal.SIGINT, observe)
    try:
        bk._release_held_snapshot_files(held)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert observed == [{}]


def test_copy_preserves_a_published_inode_when_its_writer_lease_breaks(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    source = tmp_path / "source.flac"
    destination = tmp_path / "destination"
    source.write_bytes(b"original audio")
    destination.mkdir()
    source_fd = os.open(source, os.O_RDONLY)
    destination_fd = bk._open_backup_directory(destination)
    captured_leases = []
    attempted_write = False
    real_acquire = bk.acquire_inode_write_exclusion
    real_fsync_directories = bk._fsync_directory_fds

    def capture_lease(descriptor):
        lease = real_acquire(descriptor)
        if lease is not None:
            captured_leases.append(lease)
        return lease

    def refuse_commit_after_writer_arrives(*descriptors):
        nonlocal attempted_write
        if not attempted_write and (destination / "published.flac").exists():
            attempted_write = True
            with pytest.raises(BlockingIOError) as refused:
                os.open(
                    "published.flac",
                    os.O_WRONLY | os.O_NONBLOCK,
                    dir_fd=destination_fd,
                )
            assert refused.value.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
            assert captured_leases and not captured_leases[-1].intact()
            return False
        return real_fsync_directories(*descriptors)

    monkeypatch.setattr(bk, "acquire_inode_write_exclusion", capture_lease)
    monkeypatch.setattr(
        bk, "_fsync_directory_fds", refuse_commit_after_writer_arrives)
    publication = bk._CopyPublication(destination_fd, "published.flac")
    try:
        with pytest.raises(OSError):
            bk._copy_file_noreplace_at(source_fd, publication)
    finally:
        bk._release_copy_publication(publication)
        os.close(destination_fd)
        os.close(source_fd)

    published = destination / "published.flac"
    assert attempted_write
    assert published.read_bytes() == b"original audio"
    published.write_bytes(b"later writer data")
    assert published.read_bytes() == b"later writer data"


def test_copy_owner_reconciles_interruption_after_file_transfer(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    source = tmp_path / "source.flac"
    destination = tmp_path / "destination"
    source.write_bytes(b"original audio")
    destination.mkdir()
    source_fd = os.open(source, os.O_RDONLY)
    destination_fd = bk._open_backup_directory(destination)
    real_bind = bk._CopyPublication.bind_file
    interrupted = False

    def interrupt_after_bind(publication, file_object, temporary_name):
        nonlocal interrupted
        real_bind(publication, file_object, temporary_name)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        bk._CopyPublication, "bind_file", interrupt_after_bind)
    publication = bk._CopyPublication(destination_fd, "published.flac")
    try:
        with pytest.raises(KeyboardInterrupt):
            bk._copy_file_noreplace_at(source_fd, publication)
    finally:
        bk._release_copy_publication(publication)
        os.close(destination_fd)
        os.close(source_fd)

    assert interrupted
    assert list(destination.iterdir()) == []


def test_downsample_interrupt_after_copy_return_seals_recovery_receipt(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    source = album / "01.flac"
    source.write_bytes(b"hi-res original")
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    real_copy = bk._copy_file_noreplace_at

    def interrupt_after_copy(source_fd, publication):
        real_copy(source_fd, publication)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        bk, "_copy_file_noreplace_at", interrupt_after_copy)
    with pytest.raises(KeyboardInterrupt):
        bk.stash_downsample_originals([source], album)

    backup_dirs = [path for path in backups.iterdir() if path.is_dir()]
    assert len(backup_dirs) == 1
    backup = backup_dirs[0]
    backup_fd = bk._open_backup_directory(backup)
    try:
        receipt = bk._read_backup_receipt(backup_fd)
    finally:
        os.close(backup_fd)
    assert receipt is not None and receipt["complete"] is False
    assert (backup / "01.flac").read_bytes() == b"hi-res original"
    assert not (backup / bk._REAP_AFTER_RETENTION_SENTINEL).exists()
    assert source.read_bytes() == b"hi-res original"



def test_cross_fs_backup_rejects_same_size_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    monkeypatch.setattr("qobuz_librarian.library.backup._same_filesystem",
                        lambda a, b: False)
    album = tmp_path / "Album (2026)"
    album.mkdir()
    original = b"REAL-FLAC-AUDIO-CONTENT"
    (album / "01.flac").write_bytes(original)

    real_copytree = shutil.copytree

    def corrupt_copytree(src, dst, *a, **k):
        real_copytree(src, dst, *a, **k)
        for f in (tmp_path / "backups").rglob("*"):
            if f.is_file():
                f.write_bytes(b"\x00" * f.stat().st_size)
        return dst

    monkeypatch.setattr("qobuz_librarian.library.backup.shutil.copytree",
                        corrupt_copytree)
    bp = backup_album_dir(album)
    assert bp is None
    assert (album / "01.flac").read_bytes() == original


def test_cross_fs_backup_retires_a_multifile_source_exactly(tmp_path,
                                                            monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"first")
    (album / "Disc 2").mkdir()
    (album / "Disc 2" / "02.flac").write_bytes(b"second")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk, "_same_filesystem", lambda *_: False)

    result = bk.backup_album_dir(album)

    assert result is not None and result.complete
    assert not album.exists()
    assert (result.path / "01.flac").read_bytes() == b"first"
    assert (result.path / "Disc 2" / "02.flac").read_bytes() == b"second"


def test_incomplete_upgrade_recovers_an_exact_hidden_source_without_overwrite(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"first original")
    (album / "02.flac").write_bytes(b"second original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk, "_same_filesystem", lambda *_: False)

    real_remove = bk._remove_exact_tree_at
    forced = False

    def leave_partly_retired_source(parent_fd, name, expected_fd, *, prefix,
                                    **kwargs):
        nonlocal forced
        if prefix != "ql-backup-remove" or forced:
            return real_remove(
                parent_fd, name, expected_fd, prefix=prefix, **kwargs)
        forced = True
        quarantine_name = ".ql-backup-remove-forced"
        os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
        quarantine_fd = bk._open_backup_directory(
            quarantine_name, dir_fd=parent_fd)
        try:
            assert bk._rename_exact_noreplace_at(
                parent_fd,
                name,
                quarantine_fd,
                "held",
                expected_fd,
            ) is None
            os.unlink("01.flac", dir_fd=expected_fd)
            assert bk._fsync_directory_fds(expected_fd, quarantine_fd)
        finally:
            os.close(quarantine_fd)
        return False

    monkeypatch.setattr(bk, "_remove_exact_tree_at",
                        leave_partly_retired_source)
    recovery = bk.backup_album_dir(album)

    assert recovery is not None and not recovery.complete
    assert recovery.receipt is not None
    recovery = bk.load_backup_result(recovery.path)
    assert recovery is not None and not recovery.complete
    assert not album.exists()
    hidden = album.parent / ".ql-backup-remove-forced" / "held"
    assert not (hidden / "01.flac").exists()
    assert (hidden / "02.flac").read_bytes() == b"second original"

    album.mkdir()
    (album / "unrelated.txt").write_bytes(b"leave me alone")
    refused = bk.restore_incomplete_upgrade_backup(recovery, album)
    assert refused == bk.IncompleteUpgradeRestoreOutcome(0, 0, 2, False)
    assert (album / "unrelated.txt").read_bytes() == b"leave me alone"
    assert recovery.path.exists() and hidden.exists()

    shutil.rmtree(album)
    real_rename = bk._rename_exact_noreplace_at
    interrupted = False

    def interrupt_after_restored_file(source_fd, source_name, destination_fd,
                                      destination_name, expected_fd):
        nonlocal interrupted
        result = real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            expected_fd,
        )
        if destination_name == "01.flac" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("fatal after restored-file publication")
        return result

    monkeypatch.setattr(
        bk, "_rename_exact_noreplace_at", interrupt_after_restored_file)
    with pytest.raises(KeyboardInterrupt):
        bk.restore_incomplete_upgrade_backup(recovery, album)
    assert interrupted
    assert (recovery.path / bk._PARTIAL_RESTORE_SENTINEL).is_file()
    monkeypatch.setattr(bk, "_rename_exact_noreplace_at", real_rename)

    restored = bk.restore_incomplete_upgrade_backup(recovery, album)
    assert restored == bk.IncompleteUpgradeRestoreOutcome(0, 2, 0, True)
    assert (album / "01.flac").read_bytes() == b"first original"
    assert (album / "02.flac").read_bytes() == b"second original"
    assert not recovery.path.exists()
    assert not (album.parent / ".ql-backup-remove-forced").exists()


def test_whole_album_backup_breaks_source_hardlinks(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"shared inode")
    os.link(first, second)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")

    result = bk.backup_album_dir(album)

    assert result is not None and result.complete
    backed_first = result.path / "01.flac"
    backed_second = result.path / "02.flac"
    assert backed_first.read_bytes() == backed_second.read_bytes() == b"shared inode"
    assert backed_first.stat().st_ino != backed_second.stat().st_ino
    assert backed_first.stat().st_nlink == backed_second.stat().st_nlink == 1


def test_gap_fill_backup_breaks_source_hardlinks(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"shared inode")
    os.link(first, second)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")

    result = bk.backup_gap_fill_files([first, second], album)

    assert result is not None and result.complete
    assert not first.exists() and not second.exists()
    backed_first = result.path / "01.flac"
    backed_second = result.path / "02.flac"
    assert backed_first.read_bytes() == backed_second.read_bytes() == b"shared inode"
    assert backed_first.stat().st_ino != backed_second.stat().st_ino
    assert backed_first.stat().st_nlink == backed_second.stat().st_nlink == 1


def test_backup_album_dir_moves_and_refuses_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    album = tmp_path / "My Album"
    album.mkdir()
    (album / "track.flac").write_bytes(b"audio")
    bp = backup_album_dir(album)
    assert bp is not None and bp.exists() and not album.exists()

    target = tmp_path / "real_album"
    target.mkdir()
    (target / "track.flac").write_bytes(b"audio")
    link = tmp_path / "linked_album"
    link.symlink_to(target)
    assert backup_album_dir(link) is None
    assert target.exists()


def test_same_fs_backup_reconciles_interrupt_after_rename(tmp_path,
                                                          monkeypatch):
    import qobuz_librarian.library.backup as bk

    owner = {
        "operation_id": "a" * 64,
        "item_id": "b" * 64,
    }
    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original")
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    real_rename = bk._rename_noreplace_at
    interrupted = False
    intents = []

    def checkpoint(intent):
        assert intent["kind"] == "upgrade"
        assert intent["owner"] == owner
        assert intent["source_receipt"]["origin"] == str(album)
        assert (album / "01.flac").read_bytes() == b"original"
        assert not os.path.lexists(intent["path"])
        intents.append(intent)

    def failed_checkpoint(_intent):
        raise OSError("journal checkpoint failed")

    with pytest.raises(OSError, match="journal checkpoint failed"):
        bk.backup_album_dir(
            album, owner=owner, on_intent=failed_checkpoint)
    assert (album / "01.flac").read_bytes() == b"original"
    assert not list(backups.iterdir())

    def interrupt_after_rename(source_fd, source_name, destination_fd,
                               destination_name):
        nonlocal interrupted
        real_rename(source_fd, source_name, destination_fd, destination_name)
        if source_name == "Album" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(bk, "_rename_noreplace_at", interrupt_after_rename)

    with pytest.raises(KeyboardInterrupt):
        bk.backup_album_dir(album, owner=owner, on_intent=checkpoint)
    assert (album / "01.flac").read_bytes() == b"original"
    assert not list(backups.iterdir())
    assert len(intents) == 1

    monkeypatch.setattr(bk, "_rename_noreplace_at", real_rename)
    completed = bk.backup_album_dir(
        album, owner=owner, on_intent=checkpoint)
    assert completed is not None and completed.complete
    assert completed.receipt["version"] == 2
    assert completed.receipt["owner"] == owner
    assert bk._backup_receipt_schema_valid(completed.receipt)
    assert bk.load_backup_result(
        completed.path, expected_owner=owner) == completed
    wrong_owner = {
        "operation_id": "c" * 64,
        "item_id": "d" * 64,
    }
    assert bk.load_backup_result(
        completed.path, expected_owner=wrong_owner) is None
    assert bk.restore_upgrade_backup(completed, album) is False
    assert bk.restore_upgrade_backup(
        completed, album, expected_owner=wrong_owner) is False
    assert not album.exists()
    assert completed.path.exists()

    assert bk.restore_upgrade_backup(
        completed, album, expected_owner=owner) is True
    assert (album / "01.flac").read_bytes() == b"original"


def test_cross_fs_backup_preserves_a_replaced_source_path(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original audio")
    moved_original = tmp_path / "moved-original"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk, "_same_filesystem", lambda *_: False)
    write_origin = bk._write_backup_origin

    def replace_source(bp, origin):
        written = write_origin(bp, origin)
        album.rename(moved_original)
        album.mkdir()
        (album / "unrelated.txt").write_text("leave this alone", encoding="utf-8")
        return written

    monkeypatch.setattr(bk, "_write_backup_origin", replace_source)

    retained = bk.backup_album_dir(album)
    assert retained is not None and not retained.complete
    assert retained.receipt is not None
    assert bk.restore_incomplete_upgrade_backup(retained, album) is None
    assert (album / "unrelated.txt").read_text(encoding="utf-8") == "leave this alone"
    assert (moved_original / "01.flac").read_bytes() == b"original audio"
    assert any((tmp_path / "backups").glob("*"))

    monkeypatch.setattr(bk, "_write_backup_origin", write_origin)
    interrupted = music / "Artist" / "Interrupted"
    interrupted.mkdir()
    (interrupted / "01.flac").write_bytes(b"still here")
    copytree = shutil.copytree

    def interrupt_copy(src, dst, *args, **kwargs):
        copytree(src, dst, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(bk.shutil, "copytree", interrupt_copy)
    with pytest.raises(KeyboardInterrupt):
        bk.backup_album_dir(interrupted)
    assert (interrupted / "01.flac").read_bytes() == b"still here"
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_backup_refuses_reserved_metadata_names(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")

    album = music / "Artist" / "Album"
    (album / "Disc 1").mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio")
    reserved = album / "Disc 1" / bk._RECEIPT_SIDECAR
    reserved.write_text("user content", encoding="utf-8")

    assert bk.backup_album_dir(album) is None
    assert reserved.read_text(encoding="utf-8") == "user content"
    assert (album / "01.flac").read_bytes() == b"audio"


def test_restore_overwrites_partial_but_keeps_larger_good_file(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)

    a = tmp_path / "a"
    (a / "bk").mkdir(parents=True)
    (a / "album").mkdir()
    (a / "bk" / "01.flac").write_bytes(b"FULL-ORIGINAL-CONTENT-XXXXXX")
    (a / "album" / "01.flac").write_bytes(b"partial")
    backup = _seal_test_backup(bk, a / "bk", a / "album")
    assert bk.restore_gap_fill_backup(backup, a / "album") == 1
    assert (a / "album" / "01.flac").read_bytes() == b"FULL-ORIGINAL-CONTENT-XXXXXX"

    _need_audio_tools()
    b = tmp_path / "b"
    (b / "bk").mkdir(parents=True)
    (b / "album").mkdir()
    (b / "bk" / "01.flac").write_bytes(b"trunc")
    _real_flac(b / "album" / "01.flac")
    good = (b / "album" / "01.flac").read_bytes()
    backup = _seal_test_backup(bk, b / "bk", b / "album")
    assert bk.restore_gap_fill_backup(backup, b / "album") == 1
    assert (b / "album" / "01.flac").read_bytes() == good
    assert not (b / "bk").exists()


def test_restore_does_not_keep_larger_but_corrupt_dst(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)
    _need_audio_tools()
    c = tmp_path / "c"
    (c / "bk").mkdir(parents=True)
    (c / "album").mkdir()
    _real_flac(c / "bk" / "01.flac", seconds=2)
    good = (c / "bk" / "01.flac").read_bytes()
    (c / "album" / "01.flac").write_bytes(b"\x00" * (len(good) + 4096))
    backup = _seal_test_backup(bk, c / "bk", c / "album")
    assert bk.restore_gap_fill_backup(backup, c / "album") == 1
    assert (c / "album" / "01.flac").read_bytes() == good


def test_age_sweep_keeps_any_backup_it_cannot_prove_redundant(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr(bk.cfg, "DATA_DIR", tmp_path)

    bp = backup_root / "20200101_000000_naked"
    bp.mkdir()
    (bp / "01.flac").write_bytes(b"x" * 5000)
    assert not bk._backup_safe_to_reap(bp)
    removed = bk.cleanup_old_upgrade_backups(retention_days=1, force=True)
    assert bp.exists() and (bp / "01.flac").exists()
    assert removed == 0
    assert any(e == bp for e, _origin in bk.find_only_copy_backups())


def test_unverified_upgrade_backup_is_pinned_from_age_sweep(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(bk.cfg, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()

    album = tmp_path / "music" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01 - A.flac").write_bytes(b"a" * 3000)
    (album / "02 - B.flac").write_bytes(b"b" * 3000)
    bp = bk.backup_album_dir(album)
    assert bp is not None

    album.mkdir(parents=True, exist_ok=True)
    (album / "01 - A.flac").write_bytes(b"A" * 9000)
    (album / "02 - B.flac").write_bytes(b"B" * 9000)
    assert bk._backup_safe_to_reap(bp)
    bk.pin_unverified_upgrade_backup(bp)
    assert not bk._backup_safe_to_reap(bp)

    aged = bp.with_name("20200101_000000_aged")
    bp.rename(aged)
    assert bk.cleanup_old_upgrade_backups(force=True) == 0
    assert (aged / "01 - A.flac").exists()


def test_backup_refuses_rather_than_leave_unprotected_sole_copy(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(bk, "_write_backup_origin", lambda bp, origin: False)

    album = tmp_path / "music" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio-1")
    (album / "02.flac").write_bytes(b"audio-2")
    assert bk.backup_album_dir(album) is None
    assert (album / "01.flac").read_bytes() == b"audio-1"
    assert (album / "02.flac").exists()
    assert not any((tmp_path / "backups").glob("*")) if (tmp_path / "backups").exists() else True

    g1 = album / "01.flac"
    before = g1.read_bytes()
    assert bk.backup_gap_fill_files([str(g1)], album) is None
    assert g1.exists() and g1.read_bytes() == before




def test_restore_preserves_preexisting_workspace_names(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.beets.forget_beets_entries",
        lambda _paths: 0,
    )

    gap_backup = backups / "gap"
    gap_backup.mkdir(parents=True)
    (gap_backup / "01.flac").write_bytes(b"the backed-up original")
    album = music / "Artist" / "Gap album"
    album.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unrelated")
    public_tmp = album / "01.flac.restore_tmp"
    public_tmp.symlink_to(outside)

    gap_result = _seal_test_backup(bk, gap_backup, album)
    assert bk.restore_gap_fill_backup(
        gap_result, album, keep_larger_dst=False) == 1
    assert (album / "01.flac").read_bytes() == b"the backed-up original"
    assert public_tmp.is_symlink()
    assert outside.read_bytes() == b"unrelated"

    upgrade_backup = backups / "upgrade"
    upgrade_backup.mkdir()
    (upgrade_backup / "01.flac").write_bytes(b"full album" * 100)
    partial = music / "Artist" / "Upgrade album"
    partial.mkdir()
    (partial / "01.flac").write_bytes(b"partial")
    public_trash = partial.with_name(partial.name + ".restore_trash")
    public_trash.mkdir()
    (public_trash / "user.txt").write_bytes(b"keep me")

    upgrade_result = _seal_test_backup(
        bk, upgrade_backup, partial, kind="upgrade")
    assert bk.restore_upgrade_backup(upgrade_result, partial) is True
    assert (partial / "01.flac").read_bytes() == b"full album" * 100
    assert (public_trash / "user.txt").read_bytes() == b"keep me"


def test_upgrade_restore_defers_beets_mutation_until_filesystem_commit(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    backup = backups / "failing-upgrade"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"full album" * 100)
    partial = music / "Artist" / "Album"
    partial.mkdir(parents=True)
    partial_track = partial / "01.flac"
    partial_track.write_bytes(b"partial")
    backup_result = _seal_test_backup(
        bk, backup, partial, kind="upgrade")

    forgotten = []
    monkeypatch.setattr(
        "qobuz_librarian.integrations.beets.forget_beets_entries",
        lambda paths: forgotten.extend(paths) or len(paths),
    )
    real_rename = bk._rename_noreplace_at

    def fail_backup_publication(source_fd, source_name, destination_fd,
                                destination_name):
        if source_name == backup.name and destination_name == partial.name:
            raise OSError(errno.EIO, "forced publication failure")
        return real_rename(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(bk, "_rename_noreplace_at", fail_backup_publication)

    assert bk.restore_upgrade_backup(backup_result, partial) is False
    assert partial_track.read_bytes() == b"partial"
    assert (backup / "01.flac").exists()
    assert forgotten == []


def test_upgrade_restore_does_not_remove_beets_rows_by_restored_path(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    backup = backups / "upgrade"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"complete album" * 100)
    partial = music / "Artist" / "Album"
    partial.mkdir(parents=True)
    (partial / "01.flac").write_bytes(b"partial")
    backup_result = _seal_test_backup(
        bk, backup, partial, kind="upgrade")
    forgotten = []
    monkeypatch.setattr(
        "qobuz_librarian.integrations.beets.forget_beets_entries",
        lambda paths: forgotten.extend(paths) or len(paths),
    )

    assert bk.restore_upgrade_backup(backup_result, partial) is True
    assert forgotten == []


def test_upgrade_restore_reconciles_interrupt_after_holding_partial(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    backup = backups / "upgrade"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"complete album" * 100)
    partial = music / "Artist" / "Album"
    partial.mkdir(parents=True)
    partial_track = partial / "01.flac"
    partial_track.write_bytes(b"partial")
    backup_result = _seal_test_backup(
        bk, backup, partial, kind="upgrade")
    rename_noreplace = bk._rename_noreplace_at
    interrupted = False

    def interrupt_after_rename(source_fd, source_name, destination_fd,
                               destination_name):
        nonlocal interrupted
        rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)
        if destination_name == "held" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(bk, "_rename_noreplace_at", interrupt_after_rename)

    with pytest.raises(KeyboardInterrupt):
        bk.restore_upgrade_backup(backup_result, partial)
    assert partial_track.read_bytes() == b"partial"
    assert (backup / "01.flac").read_bytes() == b"complete album" * 100


def test_restore_upgrade_backup_exdev_verifies_before_dropping_backup(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bkmod
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", tmp_path)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "track1.flac").write_bytes(b"a" * 50_000)
    (backup / "track2.flac").write_bytes(b"b" * 50_000)
    original = tmp_path / "Album"
    backup_result = _seal_test_backup(
        bkmod, backup, original, kind="upgrade")

    monkeypatch.setattr(bkmod, "_same_filesystem", lambda *_: False)
    real_copy_tree = bkmod._copy_tree_manifest_at

    def corrupt_staged_copy(source_fd, destination_fd, manifest):
        copied = real_copy_tree(source_fd, destination_fd, manifest)
        if copied:
            staged = bkmod._held_directory_path(destination_fd) / "track1.flac"
            staged.write_bytes(b"corrupt")
        return copied

    monkeypatch.setattr(
        bkmod, "_copy_tree_manifest_at", corrupt_staged_copy)

    assert restore_upgrade_backup(backup_result, original) is False
    assert backup.exists()
    assert (backup / "track1.flac").exists() and (backup / "track2.flac").exists()
    assert not original.exists()
    assert not list(tmp_path.glob(".ql-restore-stage-*"))



def test_gap_fill_restore_handles_failure_partial_and_interrupt(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bkmod
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)

    if os.geteuid() != 0:
        album = tmp_path / "ro_album"
        album.mkdir()
        (album / "t.flac").write_bytes(b"original")
        bp = backup_gap_fill_files([str(album / "t.flac")], album)
        os.chmod(album, 0o500)
        try:
            assert restore_gap_fill_backup(bp, album) == 0
        finally:
            os.chmod(album, 0o700)
        assert bp.exists()

    album2 = tmp_path / "partial_album"
    album2.mkdir()
    track = album2 / "t.flac"
    track.write_bytes(b"the-good-original")
    bp = backup_gap_fill_files([str(track)], album2)
    track.write_bytes(b"partial-junk")
    assert restore_gap_fill_backup(bp, album2) == 1
    assert track.read_bytes() == b"the-good-original"
    assert not bp.exists()

    album3 = tmp_path / "ki_album"
    album3.mkdir()
    tr = album3 / "t.flac"
    tr.write_bytes(b"precious-audio")
    bp = backup_gap_fill_files([str(tr)], album3)
    monkeypatch.setattr(
        bkmod,
        "_copy_file_noreplace_at",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        restore_gap_fill_backup(bp, album3)
    assert bp.exists() and len(list(bp.rglob("*.flac"))) == 1
    assert not list(album3.rglob("*.restore_tmp"))


def test_gap_fill_backup_preserves_replaced_source_files(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)

    same_album = music / "Artist" / "Same device"
    same_album.mkdir(parents=True)
    same_track = same_album / "01.flac"
    same_track.write_bytes(b"same-device original")
    moved_same = tmp_path / "moved-same.flac"
    real_rename_noreplace = bk._rename_noreplace_at
    injected = False

    def replace_before_move(source_fd, source_name, destination_fd,
                            destination_name):
        nonlocal injected
        if source_name == "01.flac" and not injected:
            injected = True
            os.rename(source_name, moved_same, src_dir_fd=source_fd)
            same_track.write_bytes(b"same-device replacement")
        return real_rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(bk, "_rename_noreplace_at", replace_before_move)
    assert bk.backup_gap_fill_files([same_track], same_album) is None
    assert same_track.read_bytes() == b"same-device replacement"
    assert moved_same.read_bytes() == b"same-device original"

    cross_album = music / "Artist" / "Cross device"
    cross_album.mkdir()
    cross_track = cross_album / "02.flac"
    cross_track.write_bytes(b"cross-device original")
    moved_cross = tmp_path / "moved-cross.flac"
    monkeypatch.setattr(bk, "_rename_noreplace_at", real_rename_noreplace)
    monkeypatch.setattr(bk, "_same_filesystem", lambda *_: False)
    real_copy = bk._copy_file_noreplace_at

    def replace_after_copy(source_fd, publication):
        copied = real_copy(source_fd, publication)
        cross_track.rename(moved_cross)
        cross_track.write_bytes(b"cross-device replacement")
        return copied

    monkeypatch.setattr(bk, "_copy_file_noreplace_at", replace_after_copy)
    cross_result = bk.backup_gap_fill_files([cross_track], cross_album)
    assert cross_result is not None and not cross_result.complete
    assert cross_track.read_bytes() == b"cross-device replacement"
    assert moved_cross.read_bytes() == b"cross-device original"
    assert any(
        path.read_bytes() == b"cross-device original"
        for path in backups.rglob("02.flac")
    )


def test_partial_gap_fill_backup_returns_its_recovery_path(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"first original")
    second.write_bytes(b"second original")
    displaced = tmp_path / "displaced-second.flac"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    rename_noreplace = bk._rename_noreplace_at
    replaced = False

    def replace_second(source_fd, source_name, destination_fd,
                       destination_name):
        nonlocal replaced
        if source_name == "02.flac" and not replaced:
            replaced = True
            second.rename(displaced)
            second.write_bytes(b"second replacement")
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(bk, "_rename_noreplace_at", replace_second)

    result = bk.backup_gap_fill_files([first, second], album)

    assert result is not None
    assert result.complete is False
    assert result.path.exists()
    assert not first.exists()
    assert second.read_bytes() == b"second replacement"
    assert displaced.read_bytes() == b"second original"
    assert any(
        path.read_bytes() == b"first original"
        for path in result.path.rglob("01.flac")
    )


def test_dispose_backup_refuses_a_replacement_tree(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    result = bk.backup_gap_fill_files([track], album)
    assert result is not None and result.complete
    held = tmp_path / "held-backup"
    result.path.rename(held)
    shutil.copytree(held, result.path)

    assert bk.dispose_backup(result) is False
    assert (result.path / "01.flac").read_bytes() == b"original"
    assert (held / "01.flac").read_bytes() == b"original"


def test_receipt_bound_disposal_revalidates_exact_replacement(tmp_path,
                                                              monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)

    source_receipt = bk.capture_album_source_receipt(album)
    backup = bk.backup_album_dir(
        album, expected_receipt=source_receipt)
    assert backup is not None and backup.complete
    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement")
    replacement_receipt = bk.capture_album_source_receipt(album)
    assert replacement_receipt is not None

    (album / "01.flac").write_bytes(b"changed replacement")
    assert not bk.dispose_backup(
        backup,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=lambda *_: True,
    )
    assert backup.path.exists()

    replacement_receipt = bk.capture_album_source_receipt(album)
    seen_descriptor_views = []

    def replacement_is_good(replacement_view, backup_view):
        from qobuz_librarian.library.scanner import iter_tree_no_symlinks

        seen_descriptor_views.extend((replacement_view, backup_view))
        return (
            any(path.name == "01.flac"
                for path in iter_tree_no_symlinks(replacement_view))
            and any(path.name == "01.flac"
                    for path in iter_tree_no_symlinks(backup_view))
            and (replacement_view / "01.flac").read_bytes()
                == b"changed replacement"
            and (backup_view / "01.flac").read_bytes() == b"original"
        )

    assert bk.dispose_backup(
        backup,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=replacement_is_good,
    )
    assert not backup.path.exists()
    assert not list(backups.glob(".ql-dispose-backup-*"))
    assert all(path.parent == seen_descriptor_views[0].parent
               for path in seen_descriptor_views)
    assert album not in seen_descriptor_views
    assert backup.path not in seen_descriptor_views


def _ownerless_disposal_case(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    monkeypatch.setattr(bk.cfg, "DATA_DIR", tmp_path / "data")

    backup = bk.backup_album_dir(album)
    assert backup is not None and backup.complete
    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement")
    replacement_receipt = bk.capture_album_source_receipt(album)
    assert replacement_receipt is not None
    quarantine = backups / bk._library_backup_disposal_quarantine_name(
        backup.receipt, None)
    return bk, backup, album, replacement_receipt, quarantine


def _hard_kill_ownerless_backup_disposal(
        tmp_path, monkeypatch, *, before_manifest=False,
        after_first_unlink=False):
    bk, backup, album, replacement_receipt, quarantine = (
        _ownerless_disposal_case(tmp_path, monkeypatch)
    )

    child = os.fork()
    if child == 0:
        if before_manifest:
            bk._write_ownerless_disposal_manifest_at = (
                lambda *_args, **_kwargs:
                    os.kill(os.getpid(), signal.SIGKILL)
            )
        elif after_first_unlink:
            original_unlink = bk._unlink_exact_at

            def kill_after_unlink(*args, **kwargs):
                original_unlink(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)

            bk._unlink_exact_at = kill_after_unlink
        else:
            bk._delete_exact_tree_contents = (
                lambda *_args, **_kwargs:
                    os.kill(os.getpid(), signal.SIGKILL)
            )
        bk.dispose_backup(
            backup,
            replacement_path=album,
            expected_replacement_receipt=replacement_receipt,
            replacement_validator=lambda *_paths: True,
        )
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    return bk, backup, album, quarantine


def test_retention_clears_empty_ownerless_disposal_reservation(
        tmp_path, monkeypatch):
    bk, backup, album, quarantine = _hard_kill_ownerless_backup_disposal(
        tmp_path, monkeypatch, before_manifest=True)

    assert backup.path.is_dir()
    assert quarantine.is_dir()
    assert not tuple(quarantine.iterdir())

    sweep_stamp = bk.cfg.DATA_DIR / ".last_backup_sweep"
    sweep_stamp.parent.mkdir(parents=True)
    sweep_stamp.touch()
    assert bk.cleanup_old_upgrade_backups(retention_days=999999) == 0

    assert not quarantine.exists()
    assert (backup.path / "01.flac").read_bytes() == b"original"
    assert (album / "01.flac").read_bytes() == b"replacement"


def test_retention_reconciles_full_ownerless_disposal_after_hard_kill(
        tmp_path, monkeypatch):
    bk, backup, album, quarantine = _hard_kill_ownerless_backup_disposal(
        tmp_path, monkeypatch)

    assert not backup.path.exists()
    assert sorted(path.name for path in quarantine.iterdir()) == [
        bk._DISPOSAL_MANIFEST,
        "held",
    ]

    def refuse_same_sweep_disposal(*_args, **_kwargs):
        pytest.fail("a just-restored carrier was reaped in the same sweep")

    monkeypatch.setattr(
        bk, "_dispose_retention_candidate", refuse_same_sweep_disposal)
    assert bk.cleanup_old_upgrade_backups(
        retention_days=-1, force=True) == 0

    assert not quarantine.exists()
    assert (backup.path / "01.flac").read_bytes() == b"original"
    assert (album / "01.flac").read_bytes() == b"replacement"


def test_retention_refuses_partial_ownerless_disposal_after_hard_kill(
        tmp_path, monkeypatch):
    bk, backup, album, quarantine = _hard_kill_ownerless_backup_disposal(
        tmp_path, monkeypatch, after_first_unlink=True)

    def residue():
        return {
            path.relative_to(quarantine).as_posix(): path.read_bytes()
            for path in quarantine.rglob("*")
            if path.is_file()
        }

    before = residue()
    assert bk._DISPOSAL_MANIFEST in before
    assert not backup.path.exists()

    assert bk.cleanup_old_upgrade_backups(
        retention_days=-1, force=True) == 0

    assert not backup.path.exists()
    assert quarantine.is_dir()
    assert residue() == before
    bk._only_copy_cache = None
    assert (quarantine, album) in bk.find_only_copy_backups()


def test_live_partial_ownerless_disposal_stays_quarantined(
        tmp_path, monkeypatch):
    bk, backup, album, replacement_receipt, quarantine = (
        _ownerless_disposal_case(tmp_path, monkeypatch)
    )
    original_unlink = bk._unlink_exact_at
    unlinked = False

    def fail_after_first_unlink(*args, **kwargs):
        nonlocal unlinked
        if unlinked:
            raise OSError("injected partial disposal failure")
        unlinked = True
        return original_unlink(*args, **kwargs)

    monkeypatch.setattr(bk, "_unlink_exact_at", fail_after_first_unlink)
    assert not bk.dispose_backup(
        backup,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=lambda *_paths: True,
    )

    assert not backup.path.exists()
    assert (quarantine / bk._DISPOSAL_MANIFEST).is_file()
    assert (quarantine / "held").is_dir()


def test_live_partial_owned_disposal_stays_quarantined(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    original = album / "01.flac"
    original.write_bytes(b"original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    backup = bk.backup_gap_fill_files(
        [original],
        album,
        owner=owner,
        on_intent=lambda _record: None,
    )
    assert backup is not None and backup.complete
    carrier = bk.library_backup_record(backup, expected_owner=owner)
    disposal = bk.library_backup_disposal_record(
        backup, expected_owner=owner)
    assert carrier is not None and disposal is not None

    original.write_bytes(b"replacement")
    replacement_receipt = bk.capture_album_source_receipt(album)
    assert replacement_receipt is not None
    quarantine = backups / disposal["quarantine_name"]
    original_unlink = bk._unlink_exact_at
    unlinked = False

    def fail_after_first_unlink(*args, **kwargs):
        nonlocal unlinked
        if unlinked:
            raise OSError("injected partial disposal failure")
        unlinked = True
        return original_unlink(*args, **kwargs)

    monkeypatch.setattr(bk, "_unlink_exact_at", fail_after_first_unlink)
    assert not bk.dispose_backup(
        backup,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=lambda *_paths: True,
        expected_owner=owner,
        expected_disposal_record=disposal,
    )

    assert not backup.path.exists()
    assert (quarantine / "held").is_dir()
    assert bk.reconcile_library_backup_disposal(
        carrier,
        disposal,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        expected_owner=owner,
    ).state == "attention"


def test_dispose_backup_refuses_a_retargeted_validator_view(tmp_path,
                                                            monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)

    backup = bk.backup_album_dir(album)
    assert backup is not None and backup.complete
    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement")
    replacement_receipt = bk.capture_album_source_receipt(album)
    assert replacement_receipt is not None

    def retarget_backup_view(replacement_view, backup_view):
        replacement_target = os.readlink(replacement_view)
        backup_view.unlink()
        os.symlink(replacement_target, backup_view, target_is_directory=True)
        return (
            (replacement_view / "01.flac").read_bytes()
            == (backup_view / "01.flac").read_bytes()
        )

    assert not bk.dispose_backup(
        backup,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=retarget_backup_view,
    )
    assert (backup.path / "01.flac").read_bytes() == b"original"


def test_carry_backup_companions_never_overwrites_a_swapped_entry(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original audio")
    (album / "booklet.pdf").write_bytes(b"booklet")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    backup = bk.backup_album_dir(album)
    assert backup is not None and backup.complete

    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement audio")
    replacement_receipt = bk.capture_album_source_receipt(album)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (album / "booklet.pdf").symlink_to(outside)

    assert bk.carry_backup_companions(
        backup,
        album,
        expected_replacement_receipt=replacement_receipt,
    ) is None
    assert outside.read_bytes() == b"outside"
    assert backup.path.exists()

    (album / "booklet.pdf").unlink()
    replacement_receipt = bk.capture_album_source_receipt(album)
    carried_receipt = bk.carry_backup_companions(
        backup,
        album,
        expected_replacement_receipt=replacement_receipt,
    )
    assert carried_receipt is not None
    assert (album / "booklet.pdf").read_bytes() == b"booklet"


def test_carry_backup_companions_restarts_after_fatal_publication(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    backups = tmp_path / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original audio")
    (album / "Artwork").mkdir()
    (album / "Artwork" / "booklet.pdf").write_bytes(b"booklet")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backups)
    backup = bk.backup_album_dir(album)
    assert backup is not None and backup.complete

    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement audio")
    original_receipt = bk.capture_album_source_receipt(album)
    real_rename = bk._rename_exact_noreplace_at
    interrupted = False

    def interrupt_after_publication(source_fd, source_name, destination_fd,
                                    destination_name, expected_fd):
        nonlocal interrupted
        result = real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            expected_fd,
        )
        if destination_name == "booklet.pdf" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("fatal after companion publication")
        return result

    monkeypatch.setattr(
        bk, "_rename_exact_noreplace_at", interrupt_after_publication)
    with pytest.raises(KeyboardInterrupt):
        bk.carry_backup_companions(
            backup,
            album,
            expected_replacement_receipt=original_receipt,
        )

    assert interrupted
    assert (album / "Artwork" / "booklet.pdf").read_bytes() == b"booklet"
    monkeypatch.setattr(bk, "_rename_exact_noreplace_at", real_rename)

    restarted_receipt = bk.carry_backup_companions(
        backup,
        album,
        expected_replacement_receipt=original_receipt,
    )

    assert restarted_receipt is not None
    assert restarted_receipt == bk.capture_album_source_receipt(album)
    assert backup.path.exists()
    assert not list(album.parent.glob(".ql-companion-carry-*"))


def test_companion_copy_residue_stays_in_its_private_workspace(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"original audio")
    (album / "booklet.pdf").write_bytes(b"booklet")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    backup = bk.backup_album_dir(album)
    assert backup is not None and backup.complete
    album.mkdir()
    (album / "01.flac").write_bytes(b"replacement audio")
    original_receipt = bk.capture_album_source_receipt(album)
    real_file_io = bk.io.FileIO
    interrupted = False

    def interrupt_after_create(*args, **kwargs):
        nonlocal interrupted
        result = real_file_io(*args, **kwargs)
        if not interrupted and len(args) > 1 and args[1] == "x+b":
            interrupted = True
            result.close()
            raise KeyboardInterrupt("fatal after private file creation")
        return result

    monkeypatch.setattr(bk.io, "FileIO", interrupt_after_create)
    with pytest.raises(KeyboardInterrupt):
        bk.carry_backup_companions(
            backup,
            album,
            expected_replacement_receipt=original_receipt,
        )

    assert interrupted
    assert sorted(path.name for path in album.iterdir()) == ["01.flac"]
    assert not list(album.parent.glob(".ql-companion-carry-*"))
    assert not bk._backup_safe_to_reap(backup.path)

    monkeypatch.setattr(bk.io, "FileIO", real_file_io)
    assert bk.carry_backup_companions(
        backup,
        album,
        expected_replacement_receipt=original_receipt,
    ) is not None



def test_cleanup_old_upgrade_backups_respects_dates_and_throttle(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    music = tmp_path / "music"
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", music)
    monkeypatch.setattr(bk, "flac_audio_ok", lambda _path: True)
    monkeypatch.setattr(bk, "_audio_duration_seconds", lambda _path: 60.0)

    def _make_completed(name):
        bp = backup_root / name
        bp.mkdir()
        (bp / "01.flac").write_bytes(b"a" * 100)
        origin = music / name.split("_", 2)[-1]
        origin.mkdir(parents=True, exist_ok=True)
        (origin / "01.flac").write_bytes(b"a" * 9000)
        bk._write_backup_origin(bp, origin)
        descriptor = bk._open_backup_directory(bp)
        try:
            assert bk._seal_backup_result(
                bp, descriptor, origin, kind="upgrade", complete=True,
                requested=1, backed_up=1,
            ).complete
        finally:
            os.close(descriptor)
        return bp
    old = _make_completed("20200101_120000_old_album")
    legacy = backup_root / "my_hand_restored_album"
    legacy.mkdir()
    os.utime(legacy, (0, 0))

    assert cleanup_old_upgrade_backups(retention_days=1) == 1
    assert not old.exists() and legacy.exists()

    old = _make_completed("20200101_120000_old_album")
    assert cleanup_old_upgrade_backups(retention_days=1) == 0
    assert old.exists()
    assert cleanup_old_upgrade_backups(retention_days=1, force=True) == 1



def test_match_sibling_track_requires_duration_to_confirm_a_duplicate():
    t = lambda **kw: {"isrc": "", "mb_trackid": "", "title": "Intro",
                      "discnumber": 1, "tracknumber": 0, "length": 0.0,
                      "size": 0, **kw}
    assert match_sibling_track(t(length=30.0), [t(length=30.4)]) is None
    assert match_sibling_track(
        t(tracknumber=1, length=30.0),
        [t(tracknumber=1, length=30.4)],
    ) is not None
    assert match_sibling_track(t(length=30.0), [t(length=120.0)]) is None
    assert match_sibling_track(t(length=0.0, size=5_000_000),
                               [t(length=0.0, size=5_050_000)]) is None
    tag = lambda n, ln: {"isrc": "", "mb_trackid": "", "title": "Song",
                         "discnumber": 1, "tracknumber": n, "length": ln, "size": 0}
    assert match_sibling_track(tag(3, 200.0), [tag(3, 260.0)]) is None
    assert match_sibling_track(tag(3, 200.0), [tag(3, 200.5)]) is not None
    assert match_sibling_track(tag(3, 200.0), [tag(4, 200.0)]) is None
    assert match_sibling_track(
        tag(3, 200.0),
        [tag(3, 200.0), {**tag(3, 200.0), "title": "Different"}],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "title": "東京 (Remaster)"},
        [{**tag(3, 200.0), "title": "大阪 (Remaster)"}],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "title": "東京 [Remaster]"},
        [{**tag(3, 200.0), "title": "大阪 [Remaster]"}],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "tracknumber": True},
        [tag(1, 200.0)],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "length": True},
        [{**tag(3, 200.0), "length": 1.0}],
    ) is None


def test_match_sibling_track_refuses_conflicting_musicbrainz_ids():
    recording_one = "12345678-1234-1234-1234-1234567890ab"
    recording_one_compact = "123456781234123412341234567890ab"
    recording_two = "abcdefab-cdef-abcd-efab-cdefabcdefab"
    sibling = {
        "isrc": "",
        "mb_trackid": recording_one,
        "title": "Same title",
        "discnumber": 1,
        "tracknumber": 2,
        "length": 180.0,
    }
    primary = [{
        **sibling,
        "mb_trackid": recording_two,
    }]

    assert match_sibling_track(
        sibling, [{**sibling, "mb_trackid": recording_one_compact}]
    ) is not None
    assert match_sibling_track(sibling, primary) is None
    assert match_sibling_track(
        {**sibling, "isrc": "USAAA1234567"},
        [{**primary[0], "isrc": "USAAA1234567"}],
    ) is None
    assert match_sibling_track(
        {**sibling, "mb_trackid": recording_one},
        [{**sibling, "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "mb_trackid": ""},
        [{**sibling, "mb_trackid": recording_one}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "USAAA1234567", "mb_trackid": ""},
        [{**sibling, "isrc": "", "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "", "mb_trackid": ""},
        [{**sibling, "isrc": "USAAA1234567", "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "N/A", "mb_trackid": ""},
        [{**sibling, "isrc": "N/A", "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "", "mb_trackid": "N/A"},
        [{**sibling, "isrc": "", "mb_trackid": "N/A"}],
    ) is None
    for field, placeholder in (
        ("isrc", "000000000000"),
        ("mb_trackid", "00000000-0000-0000-0000-000000000000"),
    ):
        assert match_sibling_track(
            {**sibling, "isrc": "", "mb_trackid": "", field: placeholder},
            [{**sibling, "isrc": "", "mb_trackid": "", field: placeholder,
              "title": "Different"}],
        ) is None


def _bind_consolidation_summary(summary, tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.modes import consolidate as consolidation

    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets.db")
    primary = tmp_path / "Primary"
    primary.mkdir(exist_ok=True)
    primary_seal = consolidation._seal_album(primary)
    sibling_seal = consolidation._seal_album(summary["dir"])
    consolidation._bind_summary(summary, primary_seal, sibling_seal)
    return primary_seal, sibling_seal, summary["_binding"]


def test_find_sibling_album_dirs_does_not_group_distinct_years(tmp_path, monkeypatch):
    from qobuz_librarian.modes import consolidate as c
    artist = tmp_path / "Queen"
    primary = artist / "Live at Wembley 1990"
    other = artist / "Live at Wembley 1992"
    same = artist / "Live at Wembley"
    for d in (primary, other, same):
        d.mkdir(parents=True)
    album = {"title": "Live at Wembley 1990"}
    sibs = {d.name for d, _ in c.find_sibling_album_dirs(album, primary)}
    assert "Live at Wembley 1992" not in sibs
    assert "Live at Wembley" in sibs


def test_execute_consolidation_moves_overlap_to_recoverable_backup(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    sib = tmp_path / "Album (Deluxe)"
    sib.mkdir()
    f1 = sib / "track.flac"
    f1.write_bytes(b"audio-1")
    f2 = sib / "other.flac"
    f2.write_bytes(b"audio-2")
    cover = sib / "cover.jpg"
    cover.write_bytes(b"album-art")
    import sqlite3

    database = tmp_path / "beets.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE albums ("
            "id INTEGER PRIMARY KEY, artpath BLOB)"
        )
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, album_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE album_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE item_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO albums (id, artpath) VALUES (10, ?)",
            (os.fsencode(cover),),
        )
        connection.executemany(
            "INSERT INTO items (id, path, album_id) VALUES (?, ?, 10)",
            ((1, os.fsencode(f1)), (2, os.fsencode(f2))),
        )
        connection.execute(
            "INSERT INTO album_attributes (id, entity_id, key, value) "
            "VALUES (1, 10, 'source', 'reviewed')"
        )
        connection.execute(
            "INSERT INTO item_attributes (id, entity_id, key, value) "
            "VALUES (1, 1, 'source', 'reviewed')"
        )
    connection.close()
    summary = {"dir": str(sib),
               "overlap": [({"path": str(f1)}, {}), ({"path": str(f2)}, {})],
               "unique": []}

    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, tmp_path, monkeypatch)
    try:
        removed, n_fail = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()

    assert n_fail == 0
    assert sorted(p.name for p in removed) == ["other.flac", "track.flac"]
    assert not f1.exists() and not f2.exists()
    recovered = {p.name for p in (tmp_path / "backups").rglob("*.flac")}
    assert recovered == {"track.flac", "other.flac"}
    assert cover.read_bytes() == b"album-art"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM items").fetchall() == []
        assert connection.execute(
            "SELECT id FROM item_attributes"
        ).fetchall() == []
        assert connection.execute("SELECT id FROM albums").fetchall() == []
        assert connection.execute(
            "SELECT id FROM album_attributes"
        ).fetchall() == []
    connection.close()


def test_execute_consolidation_does_not_drop_rows_after_partial_backup(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk

    album = tmp_path / "Album"
    album.mkdir()
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    retained = tmp_path / "retained"
    retained.mkdir()

    def partial_backup(_paths, _album, *, expected_receipts):
        assert expected_receipts
        first.rename(retained / first.name)
        return bk.BackupResult(retained, False, None, 2, 1)

    monkeypatch.setattr(bk, "backup_gap_fill_files", partial_backup)
    summary = {
        "dir": str(album),
        "overlap": [
            ({"path": str(first)}, {}),
            ({"path": str(second)}, {}),
        ],
        "unique": [],
    }

    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, tmp_path, monkeypatch)
    try:
        removed, n_failed = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()

    assert removed == [] and n_failed == 2
    assert (retained / first.name).exists() and second.exists()


def test_execute_consolidation_refuses_a_same_name_replacement(
        tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg

    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    sibling = tmp_path / "Album Deluxe"
    sibling.mkdir()
    reviewed = sibling / "01.flac"
    reviewed.write_bytes(b"reviewed original")
    summary = {
        "dir": sibling,
        "overlap": [({"path": str(reviewed)}, {})],
        "unique": [],
    }
    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, tmp_path, monkeypatch)
    preserved = tmp_path / "reviewed.flac"
    reviewed.rename(preserved)
    reviewed.write_bytes(b"same-name replacement")
    try:
        removed, n_failed = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()

    assert removed == [] and n_failed == 1
    assert preserved.read_bytes() == b"reviewed original"
    assert reviewed.read_bytes() == b"same-name replacement"
    assert not (tmp_path / "backups").exists()


def test_consolidate_albums_is_a_noop_under_dry_run(monkeypatch):
    from argparse import Namespace

    import qobuz_librarian.library.catalog as catmod
    import qobuz_librarian.modes.consolidate as cmod

    def _boom(*a, **k):
        raise AssertionError("dry-run consolidation must not look up or touch files")
    monkeypatch.setattr(catmod, "find_album_dir_filesystem", _boom)

    album = {"id": "x", "title": "Revolver", "artist": {"name": "The Beatles"}}
    assert cmod.consolidate_albums(album, Namespace(dry_run=True, consolidate=True,
                                                    yes=False)) == 0


def test_downsample_stash_copies_and_marks_the_undo_window(tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    album = tmp_path / "music" / "A" / "Album (2020)"
    (album / "CD1").mkdir(parents=True)
    f1 = album / "01.flac"
    f1.write_bytes(b"HIRES-1")
    f2 = album / "CD1" / "02.flac"
    f2.write_bytes(b"HIRES-2")
    bp, copied = bk.stash_downsample_originals([f1, f2], album)
    assert copied == {f1, f2}
    assert (bp / "01.flac").read_bytes() == b"HIRES-1"
    assert (bp / "CD1" / "02.flac").read_bytes() == b"HIRES-2"
    assert (bp / bk._REAP_AFTER_RETENTION_SENTINEL).is_file()
    assert bk._read_backup_origin(bp) == album
    assert f1.read_bytes() == b"HIRES-1"


def test_downsample_receipt_excludes_a_replaced_source(tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    stable = album / "01.flac"
    replaced = album / "02.flac"
    stable.write_bytes(b"stable master")
    replaced.write_bytes(b"original master")
    moved = tmp_path / "moved-master.flac"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    real_copy = bk._copy_file_noreplace_at

    def replace_after_copy(source_fd, publication):
        copied = real_copy(source_fd, publication)
        if publication.destination_name == "02.flac":
            replaced.rename(moved)
            replaced.write_bytes(b"replacement master")
        return copied

    monkeypatch.setattr(bk, "_copy_file_noreplace_at", replace_after_copy)
    bp, copied, receipts = bk.stash_downsample_originals(
        [stable, replaced],
        album,
        include_identity_receipts=True,
    )

    assert bp is not None
    assert copied == set()
    assert receipts == {}
    assert bp.complete is False
    assert not (bp / bk._REAP_AFTER_RETENTION_SENTINEL).exists()
    assert replaced.read_bytes() == b"replacement master"
    assert moved.read_bytes() == b"original master"
    assert any(
        path.read_bytes() == b"original master"
        for path in bp.rglob("02.flac")
    )


def test_downsample_preserves_a_copy_when_its_transferred_lease_breaks(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    source = album / "01.flac"
    source.write_bytes(b"hi-res original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    real_copy = bk._copy_file_noreplace_at
    attempted_write = False

    def break_transferred_lease(source_fd, publication):
        nonlocal attempted_write
        copied = real_copy(source_fd, publication)
        lease = publication.lease
        with pytest.raises(BlockingIOError) as refused:
            os.open(
                publication.destination_name,
                os.O_WRONLY | os.O_NONBLOCK,
                dir_fd=publication.destination_parent_fd,
            )
        assert refused.value.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
        assert not lease.intact()
        attempted_write = True
        return copied

    monkeypatch.setattr(
        bk, "_copy_file_noreplace_at", break_transferred_lease)
    result, copied, receipts = bk.stash_downsample_originals(
        [source], album, include_identity_receipts=True)

    assert attempted_write
    assert result is not None and not result.complete
    assert copied == set()
    assert receipts == {}
    assert source.read_bytes() == b"hi-res original"
    assert (result.path / "01.flac").read_bytes() == b"hi-res original"


def test_retention_sweep_reaps_only_proven_expired_undo_copy(tmp_path, monkeypatch):
    import time as _time

    from qobuz_librarian.library import backup as bk
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(bk, "flac_audio_ok", lambda path: path.is_file())
    monkeypatch.setattr(bk, "_audio_duration_seconds", lambda _path: 60.0)
    base = tmp_path / "bk"
    old = base / "20200101_000000_000000_downsample_Album"
    old.mkdir(parents=True)
    (old / "01.flac").write_bytes(b"HIRES")
    (old / bk._ORIGIN_SIDECAR).write_text(str(tmp_path / "gone"), encoding="utf-8")
    (old / bk._REAP_AFTER_RETENTION_SENTINEL).write_text("undo", encoding="utf-8")
    fresh = base / (_time.strftime("%Y%m%d_%H%M%S") + "_000000_downsample_Album2")
    fresh.mkdir()
    (fresh / "01.flac").write_bytes(b"HIRES2")
    (fresh / bk._ORIGIN_SIDECAR).write_text(str(tmp_path / "gone2"), encoding="utf-8")
    (fresh / bk._REAP_AFTER_RETENTION_SENTINEL).write_text("undo", encoding="utf-8")
    for backup, origin in ((old, tmp_path / "gone"),
                           (fresh, tmp_path / "gone2")):
        origin.mkdir()
        (origin / "01.flac").write_bytes(b"LOW")
        descriptor = bk._open_backup_directory(backup)
        try:
            assert bk._seal_backup_result(
                backup, descriptor, origin, kind="downsample",
                complete=True, requested=1, backed_up=1,
            ).complete
        finally:
            os.close(descriptor)
    n = bk.cleanup_old_upgrade_backups(retention_days=7, force=True)
    assert n == 1
    assert not old.exists() and fresh.exists()
    bk._only_copy_cache = None
    assert all(p != fresh for p, _ in bk.find_only_copy_backups())
    assert [p for p, _ in bk.list_undo_copies()] == [fresh]


def test_cross_fs_backup_refuses_when_flush_fails(tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bkmod

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio-bytes")
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR",
                        tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(bkmod, "_same_filesystem", lambda a, b: False)
    monkeypatch.setattr(bkmod, "_fsync_exact_tree", lambda *_a, **_k: False)

    assert backup_album_dir(album_dir) is None
    assert (album_dir / "01.flac").read_bytes() == b"audio-bytes"


def test_replacement_tree_flush_includes_nested_entries_and_parent(tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk

    album = tmp_path / "music" / "Artist" / "Album"
    disc = album / "Disc 2"
    disc.mkdir(parents=True)
    track = disc / "01 - Song.flac"
    booklet = album / "booklet.pdf"
    track.write_bytes(b"audio")
    booklet.write_bytes(b"booklet")
    flushed = []
    monkeypatch.setattr(bk, "_fsync", lambda path: flushed.append(path) or True)

    assert bk.replacement_tree_durable(album) is True
    assert {album, disc, track, booklet, album.parent}.issubset(set(flushed))

    monkeypatch.setattr(bk, "_fsync", lambda path: path != track)
    assert bk.replacement_tree_durable(album) is False


def test_stash_refuses_copies_that_cannot_be_flushed(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as bkmod

    album = tmp_path / "Album"
    album.mkdir()
    f = album / "01 - Track.flac"
    f.write_bytes(b"hi-res-master")
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(bkmod, "_fsync", lambda _p: False)

    bp, copied = bkmod.stash_downsample_originals([f], album)
    assert bp is None and copied == set()
    assert f.read_bytes() == b"hi-res-master"


def test_gap_fill_restore_keeps_backup_when_the_dir_flush_fails(monkeypatch, tmp_path):
    from pathlib import Path

    from qobuz_librarian.library import backup as bkmod
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", tmp_path)

    album = tmp_path / "Album"
    album.mkdir()
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "01 - Track.flac").write_bytes(b"the-only-copy")
    backup_result = _seal_test_backup(bkmod, bk, album)
    monkeypatch.setattr(bkmod, "_fsync",
                        lambda p: not Path(p).is_dir())

    n = bkmod.restore_gap_fill_backup(
        backup_result, album, keep_larger_dst=False)
    assert n == 0
    assert bk.exists()
    assert (bk / "01 - Track.flac").read_bytes() == b"the-only-copy"
    assert (bk / bkmod._PARTIAL_RESTORE_SENTINEL).is_file()


def test_file_restore_accepts_a_carried_downsample_backup(tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as bk

    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    backup = tmp_path / "backups" / "downsample"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"kept-original")
    carried = _seal_test_backup(bk, backup, album, kind="downsample")

    assert bk.restore_upgrade_backup(carried, album) is False
    assert backup.exists() and album.exists()
    assert bk.restore_gap_fill_backup(
        carried, album, keep_larger_dst=False) == 1
    assert (album / "01.flac").read_bytes() == b"kept-original"
    assert not backup.exists()


def test_keep_larger_dst_branch_flushes_before_dropping_the_backup_copy(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)

    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "01.flac").write_bytes(b"short-original")
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"the-much-larger-refill")
    monkeypatch.setattr(bk, "flac_audio_ok", lambda p: True)
    backup_result = _seal_test_backup(bk, backup, album)

    monkeypatch.setattr(bk, "_fsync", lambda p: False)
    assert bk.restore_gap_fill_backup(
        backup_result, album, keep_larger_dst=True) == 0
    assert (backup / "01.flac").read_bytes() == b"short-original"
    assert (backup / bk._PARTIAL_RESTORE_SENTINEL).is_file() or backup.exists()

    monkeypatch.setattr(bk, "_fsync", lambda p: True)
    assert bk.restore_gap_fill_backup(
        backup_result, album, keep_larger_dst=True) == 1
    assert not backup.exists()
    assert (album / "01.flac").read_bytes() == b"the-much-larger-refill"


def test_restore_refuses_a_silently_partial_backup_walk(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "01.flac").write_bytes(b"the-only-copy")
    album = tmp_path / "Album"
    backup_result = _seal_test_backup(bk, backup, album)

    monkeypatch.setattr(bk, "_exact_tree_snapshot", lambda *_a, **_k: None)
    assert bk.restore_gap_fill_backup(
        backup_result, album, keep_larger_dst=False) == 0
    assert (backup / "01.flac").read_bytes() == b"the-only-copy"


def test_pin_reports_an_unwritable_marker(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    bp = tmp_path / "bkp"
    bp.mkdir()
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    result = _seal_test_backup(
        bk, bp, tmp_path / "origin", kind="upgrade")
    assert bk.pin_unverified_upgrade_backup(result) is True
    assert (bp / bk._UNVERIFIED_UPGRADE_SENTINEL).is_file()

    (bp / bk._UNVERIFIED_UPGRADE_SENTINEL).unlink()
    ro = tmp_path / "gone"
    assert bk.pin_unverified_upgrade_backup(ro) is False


def test_reap_never_trusts_a_partial_backup_listing(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk

    bp = tmp_path / "bkp"
    bp.mkdir()
    (bp / "01.flac").write_bytes(b"x")
    monkeypatch.setattr(bk, "_list_tree", lambda root: None)
    assert bk._backup_safe_to_reap(bp) is False
