import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from qobuz_librarian import run_lock
from qobuz_librarian.library.release_authority import (
    AlbumAuthorityUnavailable,
    FileVersion,
    open_album_authority,
)
from qobuz_librarian.library.release_identity import (
    capture_directory_path_receipt,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)


@pytest.fixture
def live_run_lock(tmp_path, monkeypatch):
    lock_file = tmp_path / "data" / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)
    lease = run_lock.acquire()
    assert lease is not None
    try:
        yield lease, lock_file
    finally:
        lease.close()


def _album(tmp_path):
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    return album


def _start_waiting_writer(path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; print('ready', flush=True); "
                "descriptor=os.open(sys.argv[1], os.O_WRONLY); "
                "os.close(descriptor)"
            ),
            os.fspath(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.2)
    return process


def _finish_writer(process):
    try:
        stdout, stderr = process.communicate(timeout=3)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    assert process.returncode == 0, (stdout, stderr)


def _assert_descriptor_closed(descriptor):
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_album_authority_holds_write_exclusion_until_normal_context_exit(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    writer = None
    with open_album_authority(album, lease) as authority:
        held = authority.open_file(Path("01.flac"))
        writer = _start_waiting_writer(track)
        assert held.relative == Path("01.flac")
        assert os.fstat(held.descriptor).st_ino == track.stat().st_ino
        with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
            authority.validate_namespace()

    assert writer is not None
    _finish_writer(writer)
    _assert_descriptor_closed(held.descriptor)


def test_album_authority_releases_write_exclusion_after_body_exception(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")
    writer = None

    with pytest.raises(RuntimeError, match="stop after acquisition"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            writer = _start_waiting_writer(track)
            raise RuntimeError("stop after acquisition")

    assert writer is not None
    _finish_writer(writer)
    _assert_descriptor_closed(held.descriptor)


def test_album_authority_opens_files_in_required_bytewise_order_and_cleans_reverse(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_authority

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    discovered = [Path("02.flac"), Path("01.flac")]
    (album / discovered[0]).write_bytes(b"two")
    (album / discovered[1]).write_bytes(b"one")
    real_acquire = release_authority.acquire_inode_write_exclusion
    real_close = release_authority.os.close
    acquired = []
    events = []
    file_descriptors = set()

    class TrackedExclusion:
        def __init__(self, relative, exclusion):
            self.relative = relative
            self.exclusion = exclusion

        def intact(self):
            return self.exclusion.intact()

        def close(self):
            events.append(("exclusion", self.relative))
            self.exclusion.close()

    def acquire(descriptor):
        relative = Path(os.readlink(f"/proc/self/fd/{descriptor}")).relative_to(album)
        acquired.append(relative)
        file_descriptors.add(descriptor)
        exclusion = real_acquire(descriptor)
        assert exclusion is not None
        return TrackedExclusion(relative, exclusion)

    def close(descriptor):
        if descriptor in file_descriptors:
            relative = Path(os.readlink(f"/proc/self/fd/{descriptor}")).relative_to(album)
            events.append(("descriptor", relative))
        real_close(descriptor)

    monkeypatch.setattr(release_authority, "acquire_inode_write_exclusion", acquire)
    monkeypatch.setattr(release_authority.os, "close", close)

    with open_album_authority(album, lease) as authority:
        held = [
            authority.open_file(relative)
            for relative in sorted(discovered, key=lambda value: os.fsencode(value))
        ]

    assert acquired == [Path("01.flac"), Path("02.flac")]
    assert [item.relative for item in held] == acquired
    assert events[:4] == [
        ("exclusion", Path("02.flac")),
        ("descriptor", Path("02.flac")),
        ("exclusion", Path("01.flac")),
        ("descriptor", Path("01.flac")),
    ]


def test_album_authority_refuses_out_of_order_acquisition(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    (album / "02.flac").write_bytes(b"two")
    (album / "01.flac").write_bytes(b"one")

    with open_album_authority(album, lease) as authority:
        held = authority.open_file(Path("02.flac"))
        with pytest.raises(AlbumAuthorityUnavailable, match="bytewise order"):
            authority.open_file(Path("01.flac"))

    _assert_descriptor_closed(held.descriptor)


def test_album_authority_serializes_concurrent_file_acquisition(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_authority

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    (album / "01.flac").write_bytes(b"one")
    (album / "02.flac").write_bytes(b"two")
    real_hash = release_authority._sha256_fd
    first_hash_started = threading.Event()
    release_first_hash = threading.Event()
    second_hash_started = threading.Event()
    results = {}

    def controlled_hash(descriptor):
        name = Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
        if name == "01.flac":
            first_hash_started.set()
            assert release_first_hash.wait(timeout=3)
        else:
            second_hash_started.set()
        return real_hash(descriptor)

    monkeypatch.setattr(release_authority, "_sha256_fd", controlled_hash)
    with open_album_authority(album, lease) as authority:
        first = threading.Thread(
            target=lambda: results.setdefault(
                Path("01.flac"), authority.open_file(Path("01.flac"))
            )
        )
        second = threading.Thread(
            target=lambda: results.setdefault(
                Path("02.flac"), authority.open_file(Path("02.flac"))
            )
        )
        first.start()
        assert first_hash_started.wait(timeout=3)
        second.start()
        try:
            assert second_hash_started.wait(timeout=0.2) is False
        finally:
            release_first_hash.set()
            first.join(timeout=3)
            second.join(timeout=3)
        assert first.is_alive() is False
        assert second.is_alive() is False

    assert {relative: item.relative for relative, item in results.items()} == {
        Path("01.flac"): Path("01.flac"),
        Path("02.flac"): Path("02.flac"),
    }


def test_album_authority_returns_exact_full_version_and_sha256(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "disc" / "01.flac"
    track.parent.mkdir()
    track.write_bytes(b"one")
    value = track.stat()
    expected_version = FileVersion(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )
    digest = hashlib.sha256(b"one").hexdigest()

    with open_album_authority(album, lease) as authority:
        held = authority.open_file(Path("disc/01.flac"), expected_digest=digest)
        assert held.version == expected_version
        assert held.digest == digest
        authority.validate_namespace()


def test_album_authority_fails_closed_on_digest_or_lease_acquisition_failure(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_authority

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    with pytest.raises(AlbumAuthorityUnavailable, match="digest"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"), expected_digest="0" * 64)

    monkeypatch.setattr(
        release_authority,
        "acquire_inode_write_exclusion",
        lambda _descriptor: None,
    )
    with pytest.raises(AlbumAuthorityUnavailable, match="protected from writers"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"))

    descriptor = os.open(track, os.O_WRONLY | os.O_NONBLOCK)
    os.close(descriptor)


def test_album_authority_cleans_first_file_after_partial_second_acquisition(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    competing_writer = os.open(second, os.O_RDWR)
    try:
        with pytest.raises(AlbumAuthorityUnavailable, match="protected from writers"):
            with open_album_authority(album, lease) as authority:
                held = authority.open_file(Path("01.flac"))
                authority.open_file(Path("02.flac"))
    finally:
        os.close(competing_writer)

    _assert_descriptor_closed(held.descriptor)
    descriptor = os.open(first, os.O_WRONLY | os.O_NONBLOCK)
    os.close(descriptor)


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "directory"])
def test_album_authority_rejects_links_and_non_regular_entries(
    tmp_path, live_run_lock, entry_kind
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")
    target = album / "01.flac"
    if entry_kind == "symlink":
        target.symlink_to(outside)
    elif entry_kind == "hardlink":
        os.link(outside, target)
    else:
        target.mkdir()

    with pytest.raises(AlbumAuthorityUnavailable, match="regular unlinked file"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"))


@pytest.mark.parametrize(
    "relative",
    [Path("."), Path("../outside.flac"), Path("/tmp/outside.flac")],
)
def test_album_authority_rejects_unsafe_relative_paths(
    tmp_path, live_run_lock, relative
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)

    with pytest.raises(AlbumAuthorityUnavailable, match="unsafe relative path"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(relative)


def test_album_authority_rejects_a_changed_expected_album_path(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    expected = capture_directory_path_receipt(album)
    moved = tmp_path / "moved-album"
    album.rename(moved)
    album.mkdir()

    with pytest.raises(AlbumAuthorityUnavailable, match="album directory changed"):
        with open_album_authority(album, lease, expected_path=expected):
            pass


def test_album_authority_validate_namespace_rejects_file_name_replacement(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    displaced = album / "displaced.flac"
    track.write_bytes(b"one")

    with open_album_authority(album, lease) as authority:
        authority.open_file(Path("01.flac"))
        track.rename(displaced)
        track.write_bytes(b"replacement")
        with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
            authority.validate_namespace()


def test_album_authority_validate_namespace_rejects_full_version_change(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    with open_album_authority(album, lease) as authority:
        held = authority.open_file(Path("01.flac"))
        os.fchmod(held.descriptor, 0o600)
        with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
            authority.validate_namespace()


def test_album_authority_requires_live_exact_run_lock_at_entry_and_exit(
    tmp_path, live_run_lock
):
    lease, lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    with pytest.raises(AlbumAuthorityUnavailable, match="live run-lock"):
        with open_album_authority(album, object()):
            pass

    with pytest.raises(AlbumAuthorityUnavailable, match="live run-lock"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            lock_file.unlink()

    _assert_descriptor_closed(held.descriptor)
