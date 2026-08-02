import errno
import gc
import hashlib
import os
import stat
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
    MANIFEST_NAME,
    ReleaseIdentity,
    ReleaseManifestError,
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


def _nonblocking_writer_errno(path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "\ntry: descriptor=os.open(sys.argv[1], os.O_WRONLY|os.O_NONBLOCK)"
                "\nexcept OSError as error: sys.exit(error.errno)"
                "\nelse: os.close(descriptor); sys.exit(0)"
            ),
            os.fspath(path),
        ],
        check=False,
    )
    return result.returncode


def _assert_descriptor_closed(descriptor):
    with pytest.raises(OSError):
        os.fstat(descriptor)


_CANONICAL_MANIFEST = (
    b'{"schema_version":1,"provider":"qobuz","release_id":"123"}\n'
)


def _transaction_artifacts(album):
    return sorted(
        path
        for path in album.iterdir()
        if path.name.startswith(".qobuz-librarian-release.txn-")
    )


def _assert_one_canonical_publication_evidence(album):
    final = album / MANIFEST_NAME
    final_is_canonical = final.is_file() and final.read_bytes() == _CANONICAL_MANIFEST
    recovery = [
        path for path in _transaction_artifacts(album)
        if path.is_file() and path.read_bytes() == _CANONICAL_MANIFEST
    ]
    assert int(final_is_canonical) + int(bool(recovery)) == 1


def test_album_authority_holds_write_exclusion_until_normal_context_exit(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    writer = None
    with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
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


def test_album_authority_clean_exit_revalidates_replaced_file(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    displaced = album / "displaced.flac"
    track.write_bytes(b"one")

    with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            track.rename(displaced)
            track.write_bytes(b"replacement")

    _assert_descriptor_closed(held.descriptor)


def test_album_authority_clean_exit_revalidates_broken_file_lease(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            assert _nonblocking_writer_errno(track) == errno.EAGAIN

    _assert_descriptor_closed(held.descriptor)
    writer = os.open(track, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer)


def test_album_authority_body_exception_is_not_masked_by_exit_validation(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    displaced = album / "displaced.flac"
    track.write_bytes(b"one")

    with pytest.raises(RuntimeError, match="original body failure"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            track.rename(displaced)
            track.write_bytes(b"replacement")
            raise RuntimeError("original body failure")

    _assert_descriptor_closed(held.descriptor)


def test_album_authority_retries_interrupted_exclusion_cleanup_without_masking_body(
    tmp_path, live_run_lock, monkeypatch
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")
    close_calls = 0

    with pytest.raises(RuntimeError, match="original body failure"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            real_close = held.exclusion.close

            def interrupt_once():
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise KeyboardInterrupt
                real_close()

            monkeypatch.setattr(held.exclusion, "close", interrupt_once)
            raise RuntimeError("original body failure")

    assert close_calls == 2
    _assert_descriptor_closed(held.descriptor)
    writer = os.open(track, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer)


def test_album_authority_bounds_cleanup_retry_and_retains_failed_ownership(
    tmp_path, live_run_lock, monkeypatch
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")
    close_calls = 0

    with pytest.raises(RuntimeError, match="original body failure"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            real_close = held.exclusion.close

            def always_interrupt():
                nonlocal close_calls
                close_calls += 1
                raise KeyboardInterrupt

            monkeypatch.setattr(held.exclusion, "close", always_interrupt)
            raise RuntimeError("original body failure")

    assert close_calls == 2
    assert [binding.held for binding in authority._files] == [held]
    assert _nonblocking_writer_errno(track) == errno.EAGAIN
    _assert_descriptor_closed(held.descriptor)

    outsider = tmp_path / "outsider"
    outsider.write_bytes(b"outsider")
    outsider_descriptor = os.open(outsider, os.O_RDONLY)
    if outsider_descriptor != held.descriptor:
        os.dup2(outsider_descriptor, held.descriptor)
        os.close(outsider_descriptor)
        outsider_descriptor = held.descriptor
    try:
        monkeypatch.setattr(held.exclusion, "close", real_close)
        assert authority._close_resources() is None
        assert authority._files == []
        assert os.pread(outsider_descriptor, 8, 0) == b"outsider"
    finally:
        try:
            os.close(outsider_descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise

    writer = os.open(track, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer)


@pytest.mark.parametrize("descriptor_kind", ["file", "root-directory"])
def test_album_authority_consumes_interrupted_descriptor_cleanup_ownership(
    tmp_path, live_run_lock, monkeypatch, descriptor_kind
):
    from qobuz_librarian.library import release_authority

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")
    real_close = release_authority.os.close
    target = -1
    close_calls = 0

    def interrupt_target_once(descriptor):
        nonlocal close_calls
        if descriptor == target:
            close_calls += 1
            if close_calls == 1:
                raise KeyboardInterrupt
        real_close(descriptor)

    with pytest.raises(RuntimeError, match="original body failure"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            target = (
                held.descriptor
                if descriptor_kind == "file"
                else authority._directories[0].descriptor
            )
            other_descriptor = (
                authority._directories[0].descriptor
                if descriptor_kind == "file"
                else held.descriptor
            )
            monkeypatch.setattr(
                release_authority.os,
                "close",
                interrupt_target_once,
            )
            raise RuntimeError("original body failure")

    try:
        assert close_calls == 1
        assert authority._closed is True
        os.fstat(target)
        _assert_descriptor_closed(other_descriptor)
    finally:
        try:
            real_close(target)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


def test_album_authority_never_retries_reused_same_inode_descriptor(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_authority

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")
    real_close = release_authority.os.close
    target = -1
    close_calls = 0
    outsider_descriptor = -1

    def close_then_reuse_same_inode(descriptor):
        nonlocal close_calls, outsider_descriptor
        if descriptor == target:
            close_calls += 1
            if close_calls == 1:
                real_close(descriptor)
                opened = os.open(track, os.O_RDONLY)
                if opened != descriptor:
                    os.dup2(opened, descriptor)
                    real_close(opened)
                outsider_descriptor = descriptor
                raise KeyboardInterrupt
        real_close(descriptor)

    try:
        with pytest.raises(RuntimeError, match="original body failure"):
            with open_album_authority(album, lease) as authority:
                held = authority.open_file(Path("01.flac"))
                target = held.descriptor
                root_descriptor = authority._directories[0].descriptor
                monkeypatch.setattr(
                    release_authority.os,
                    "close",
                    close_then_reuse_same_inode,
                )
                raise RuntimeError("original body failure")

        assert close_calls == 1
        assert authority._closed is True
        assert outsider_descriptor == target
        assert os.pread(outsider_descriptor, 3, 0) == b"one"
        _assert_descriptor_closed(root_descriptor)
    finally:
        if outsider_descriptor >= 0:
            monkeypatch.setattr(
                release_authority.os,
                "close",
                real_close,
            )
            try:
                real_close(outsider_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


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


def test_album_authority_revalidates_cached_file_name_before_return(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    displaced = album / "displaced.flac"
    track.write_bytes(b"one")

    with pytest.raises(RuntimeError, match="cached replacement observed"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"))
            track.rename(displaced)
            track.write_bytes(b"replacement")
            with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
                authority.open_file(Path("01.flac"))
            raise RuntimeError("cached replacement observed")


def test_album_authority_revalidates_cached_file_lease_before_return(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    with pytest.raises(RuntimeError, match="cached lease break observed"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"))
            assert _nonblocking_writer_errno(track) == errno.EAGAIN
            with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
                authority.open_file(Path("01.flac"))
            raise RuntimeError("cached lease break observed")


def test_album_authority_revalidates_run_lock_after_file_hashing(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_authority

    lease, lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")
    real_hash = release_authority._sha256_fd

    def hash_then_replace_run_lock(descriptor):
        digest = real_hash(descriptor)
        lock_file.unlink()
        return digest

    monkeypatch.setattr(
        release_authority,
        "_sha256_fd",
        hash_then_replace_run_lock,
    )
    with pytest.raises(RuntimeError, match="run-lock regression observed"):
        with open_album_authority(album, lease) as authority:
            with pytest.raises(AlbumAuthorityUnavailable, match="live run-lock"):
                authority.open_file(Path("01.flac"))
            writer = os.open(track, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            raise RuntimeError("run-lock regression observed")


def test_album_authority_revalidates_prior_files_after_new_file_hashing(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_authority

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    real_hash = release_authority._sha256_fd

    with pytest.raises(RuntimeError, match="prior lease break observed"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"))

            def hash_then_break_prior_lease(descriptor):
                digest = real_hash(descriptor)
                assert _nonblocking_writer_errno(first) == errno.EAGAIN
                return digest

            monkeypatch.setattr(
                release_authority,
                "_sha256_fd",
                hash_then_break_prior_lease,
            )
            with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
                authority.open_file(Path("02.flac"))
            writer = os.open(second, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            raise RuntimeError("prior lease break observed")


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

    with pytest.raises(RuntimeError, match="replacement validation observed"):
        with open_album_authority(album, lease) as authority:
            authority.open_file(Path("01.flac"))
            track.rename(displaced)
            track.write_bytes(b"replacement")
            with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
                authority.validate_namespace()
            raise RuntimeError("replacement validation observed")


def test_album_authority_validate_namespace_rejects_full_version_change(
    tmp_path, live_run_lock
):
    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    track = album / "01.flac"
    track.write_bytes(b"one")

    with pytest.raises(RuntimeError, match="version validation observed"):
        with open_album_authority(album, lease) as authority:
            held = authority.open_file(Path("01.flac"))
            os.fchmod(held.descriptor, 0o600)
            with pytest.raises(AlbumAuthorityUnavailable, match="namespace changed"):
                authority.validate_namespace()
            raise RuntimeError("version validation observed")


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


def test_exclusive_publication_uses_authority_and_keeps_it_live(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    (album / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(
        release_identity.os,
        "link",
        lambda *_args, **_kwargs: pytest.fail("manifest publication used a hard link"),
    )

    with open_album_authority(album, lease) as authority:
        authority.open_file(Path("01.flac"))
        assert release_identity.publish_release_identity_authorized(
            authority,
            ReleaseIdentity("qobuz", "123"),
        ) is None
        authority.validate_namespace()

    assert (album / MANIFEST_NAME).read_bytes() == _CANONICAL_MANIFEST
    assert _transaction_artifacts(album) == []


def test_terminal_writer_is_refused_while_manifest_authority_is_held(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace
    writer_errnos = []

    def writer_then_rename(source_fd, source, destination_fd, destination):
        writer_errnos.append(_nonblocking_writer_errno(album / source))
        return real_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(release_identity, "_rename_noreplace", writer_then_rename)

    with pytest.raises(ReleaseManifestError, match="changed|writer"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert writer_errnos == [errno.EAGAIN]
    _assert_one_canonical_publication_evidence(album)


def test_terminal_writer_invalidates_idempotent_manifest_publication(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    with open_album_authority(album, lease) as authority:
        release_identity.publish_release_identity_authorized(
            authority,
            ReleaseIdentity("qobuz", "123"),
        )

    real_open_held = release_identity._open_held_manifest
    writer_errnos = []

    def hold_then_request_writer(directory_descriptor, name, **kwargs):
        held = real_open_held(directory_descriptor, name, **kwargs)
        if name == MANIFEST_NAME:
            writer_errnos.append(
                _nonblocking_writer_errno(album / MANIFEST_NAME)
            )
        return held

    monkeypatch.setattr(
        release_identity,
        "_open_held_manifest",
        hold_then_request_writer,
    )
    with pytest.raises(ReleaseManifestError, match="changed|writer"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert writer_errnos == [errno.EAGAIN]
    assert (album / MANIFEST_NAME).read_bytes() == _CANONICAL_MANIFEST


def test_recovery_artifact_survives_interrupt_before_exclusive_rename(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)

    def interrupt_before_rename(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(release_identity, "_rename_noreplace", interrupt_before_rename)

    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert not (album / MANIFEST_NAME).exists()
    _assert_one_canonical_publication_evidence(album)


def test_transaction_creation_handoff_preserves_exact_recovery_evidence(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_write_all = release_identity._write_all
    replaced = False

    def replace_named_transaction_after_write(descriptor, contents):
        nonlocal replaced
        real_write_all(descriptor, contents)
        if not replaced:
            transaction = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            transaction.unlink()
            transaction.write_bytes(b"replacement")
            replaced = True

    monkeypatch.setattr(
        release_identity,
        "_write_all",
        replace_named_transaction_after_write,
    )

    with pytest.raises(ReleaseManifestError, match="transaction|changed"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert replaced is True
    assert not (album / MANIFEST_NAME).exists()
    _assert_one_canonical_publication_evidence(album)


def test_transaction_writer_is_excluded_before_first_durable_fsync(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_fsync = os.fsync
    writer_errnos = []

    def request_writer_before_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and not writer_errnos:
            transaction = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            writer_errnos.append(_nonblocking_writer_errno(transaction))
        return real_fsync(descriptor)

    monkeypatch.setattr(release_identity.os, "fsync", request_writer_before_fsync)

    with pytest.raises(ReleaseManifestError, match="changed|writer|protected"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert writer_errnos == [errno.EAGAIN]
    _assert_one_canonical_publication_evidence(album)


def test_transaction_directory_is_fsynced_before_exclusive_rename(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace
    real_fsync_directory = release_identity._fsync_directory
    events = []

    def record_directory_fsync(descriptor):
        events.append("fsync-directory")
        return real_fsync_directory(descriptor)

    def require_durable_transaction(*args):
        assert events == ["fsync-directory"]
        events.append("rename")
        return real_rename(*args)

    monkeypatch.setattr(release_identity, "_fsync_directory", record_directory_fsync)
    monkeypatch.setattr(release_identity, "_rename_noreplace", require_durable_transaction)

    with open_album_authority(album, lease) as authority:
        release_identity.publish_release_identity_authorized(
            authority,
            ReleaseIdentity("qobuz", "123"),
        )

    assert events == ["fsync-directory", "rename", "fsync-directory"]


def test_exclusive_publication_derives_final_after_rename_interrupt(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace

    def rename_then_interrupt(*args):
        real_rename(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(release_identity, "_rename_noreplace", rename_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    _assert_one_canonical_publication_evidence(album)


def test_exclusive_publication_interrupt_before_directory_fsync_keeps_final_evidence(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    fsync_calls = 0

    def interrupt_after_rename(descriptor):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            os.fsync(descriptor)
            return
        assert (album / MANIFEST_NAME).is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(release_identity, "_fsync_directory", interrupt_after_rename)

    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    _assert_one_canonical_publication_evidence(album)


@pytest.mark.parametrize("replacement", [None, b"replacement"])
def test_exclusive_publication_recovers_final_unlinked_or_replaced_before_fsync(
    tmp_path, live_run_lock, monkeypatch, replacement
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    fsync_calls = 0

    def unlink_final_then_interrupt(descriptor):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            os.fsync(descriptor)
            return
        if fsync_calls > 2:
            os.fsync(descriptor)
            return
        (album / MANIFEST_NAME).unlink()
        if replacement is not None:
            (album / MANIFEST_NAME).write_bytes(replacement)
        raise KeyboardInterrupt

    monkeypatch.setattr(release_identity, "_fsync_directory", unlink_final_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    if replacement is None:
        assert not (album / MANIFEST_NAME).exists()
    else:
        assert (album / MANIFEST_NAME).read_bytes() == replacement
    _assert_one_canonical_publication_evidence(album)


def test_recovery_artifact_reconciliation_completes_under_authority(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )
    monkeypatch.setattr(release_identity, "_rename_noreplace", real_rename)

    with open_album_authority(album, lease) as authority:
        assert release_identity.reconcile_release_manifest_transaction(
            authority
        ) == ReleaseIdentity("qobuz", "123")
        authority.validate_namespace()

    assert (album / MANIFEST_NAME).read_bytes() == _CANONICAL_MANIFEST
    assert _transaction_artifacts(album) == []


def test_reconciliation_fsyncs_transaction_inode_before_rename(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )
    monkeypatch.setattr(release_identity, "_rename_noreplace", real_rename)

    events = []
    real_fsync = os.fsync

    def record_fsync(descriptor):
        events.append(
            "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        )
        real_fsync(descriptor)

    def require_file_fsync_before_rename(*args):
        assert events == ["file"]
        events.append("rename")
        return real_rename(*args)

    monkeypatch.setattr(release_identity.os, "fsync", record_fsync)
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        require_file_fsync_before_rename,
    )
    with open_album_authority(album, lease) as authority:
        assert release_identity.reconcile_release_manifest_transaction(
            authority
        ) == ReleaseIdentity("qobuz", "123")

    assert events == ["file", "rename", "directory"]


def test_reconciliation_revalidates_transaction_after_file_fsync(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )
    monkeypatch.setattr(release_identity, "_rename_noreplace", real_rename)
    transaction = _transaction_artifacts(album)[0]
    writer_errnos = []
    real_fsync = os.fsync

    def request_writer_after_file_fsync(descriptor):
        real_fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and not writer_errnos:
            writer_errnos.append(_nonblocking_writer_errno(transaction))

    monkeypatch.setattr(release_identity.os, "fsync", request_writer_after_file_fsync)
    with pytest.raises(ReleaseManifestError, match="changed|writer"):
        with open_album_authority(album, lease) as authority:
            release_identity.reconcile_release_manifest_transaction(authority)

    assert writer_errnos == [errno.EAGAIN]
    assert not (album / MANIFEST_NAME).exists()
    assert transaction.read_bytes() == _CANONICAL_MANIFEST


def test_final_only_reconciliation_flushes_directory_before_return(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    fsync_calls = 0

    def interrupt_post_rename_fsync(descriptor):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            os.fsync(descriptor)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(
        release_identity,
        "_fsync_directory",
        interrupt_post_rename_fsync,
    )
    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert (album / MANIFEST_NAME).read_bytes() == _CANONICAL_MANIFEST
    assert _transaction_artifacts(album) == []

    fsynced = []
    real_fsync = os.fsync

    def record_directory_fsync(descriptor):
        fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(release_identity, "_fsync_directory", record_directory_fsync)
    with open_album_authority(album, lease) as authority:
        expected_descriptor = authority.directory_descriptor
        assert release_identity.reconcile_release_manifest_transaction(
            authority
        ) == ReleaseIdentity("qobuz", "123")

    assert fsynced == [expected_descriptor]


def test_idempotent_publication_fsyncs_and_terminally_revalidates_final(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    with open_album_authority(album, lease) as authority:
        release_identity.publish_release_identity_authorized(
            authority,
            ReleaseIdentity("qobuz", "123"),
        )

    replacement = b"replacement"
    fsynced = False

    def replace_final_during_fsync(descriptor):
        nonlocal fsynced
        (album / MANIFEST_NAME).unlink()
        (album / MANIFEST_NAME).write_bytes(replacement)
        os.fsync(descriptor)
        fsynced = True

    monkeypatch.setattr(
        release_identity,
        "_fsync_directory",
        replace_final_during_fsync,
    )
    with pytest.raises(
        (ReleaseManifestError, AlbumAuthorityUnavailable),
        match="changed",
    ):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert fsynced is True
    assert (album / MANIFEST_NAME).read_bytes() == replacement


def test_equal_final_and_transaction_reconciliation_terminally_revalidates(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    with open_album_authority(album, lease) as authority:
        release_identity.publish_release_identity_authorized(
            authority,
            ReleaseIdentity("qobuz", "123"),
        )
    transaction = album / ".qobuz-librarian-release.txn-equal"
    transaction.write_bytes(_CANONICAL_MANIFEST)
    replacement = b"replacement"
    fsynced = False

    def replace_transaction_during_fsync(descriptor):
        nonlocal fsynced
        transaction.unlink()
        transaction.write_bytes(replacement)
        os.fsync(descriptor)
        fsynced = True

    monkeypatch.setattr(
        release_identity,
        "_fsync_directory",
        replace_transaction_during_fsync,
    )
    with pytest.raises(
        (ReleaseManifestError, AlbumAuthorityUnavailable),
        match="changed",
    ):
        with open_album_authority(album, lease) as authority:
            release_identity.reconcile_release_manifest_transaction(authority)

    assert fsynced is True
    assert transaction.read_bytes() == replacement


def test_recovery_artifact_replacement_is_never_published_or_unlinked(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_rename = release_identity._rename_noreplace
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )
    artifact = _transaction_artifacts(album)[0]

    def replace_then_rename(source_fd, source, destination_fd, destination):
        artifact.unlink()
        artifact.write_bytes(b"replacement")
        return real_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(release_identity, "_rename_noreplace", replace_then_rename)
    with pytest.raises(ReleaseManifestError, match="transaction|changed"):
        with open_album_authority(album, lease) as authority:
            release_identity.reconcile_release_manifest_transaction(authority)

    assert (album / MANIFEST_NAME).read_bytes() == b"replacement"
    _assert_one_canonical_publication_evidence(album)


def test_recovery_artifact_exception_cleanup_releases_descriptor_and_lease(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_acquire = release_identity.acquire_inode_write_lease
    exclusions = []
    close_calls = 0

    def capture_exclusion(descriptor):
        nonlocal close_calls
        exclusion = real_acquire(descriptor)
        exclusions.append(exclusion)
        if len(exclusions) == 1:
            real_close = exclusion.close

            def interrupt_close_once():
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise KeyboardInterrupt
                real_close()

            monkeypatch.setattr(exclusion, "close", interrupt_close_once)
        return exclusion

    monkeypatch.setattr(
        release_identity,
        "acquire_inode_write_lease",
        capture_exclusion,
    )
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stop publication")),
    )

    with pytest.raises(RuntimeError, match="stop publication"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    artifact = _transaction_artifacts(album)[0]
    assert close_calls == 2
    assert exclusions and all(exclusion is not None for exclusion in exclusions)
    assert all(exclusion.intact() is False for exclusion in exclusions)
    assert _nonblocking_writer_errno(artifact) == 0


def test_double_interrupted_write_lease_cleanup_retains_creator_descriptor(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_acquire = release_identity.acquire_inode_write_lease
    real_create = release_identity._create_manifest_transaction
    captured = []
    close_calls = 0

    def interrupt_two_close_attempts(descriptor):
        nonlocal close_calls
        exclusion = real_acquire(descriptor)
        assert exclusion is not None
        real_close = exclusion.close

        def close():
            nonlocal close_calls
            close_calls += 1
            if close_calls <= 2:
                raise KeyboardInterrupt
            real_close()

        monkeypatch.setattr(exclusion, "close", close)
        return exclusion

    def capture_held(*args, **kwargs):
        held = real_create(*args, **kwargs)
        captured.append((held, held.descriptor))
        return held

    monkeypatch.setattr(
        release_identity,
        "acquire_inode_write_lease",
        interrupt_two_close_attempts,
    )
    monkeypatch.setattr(
        release_identity,
        "_create_manifest_transaction",
        capture_held,
    )
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stop publication")),
    )

    with pytest.raises(RuntimeError, match="stop publication"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert len(captured) == 1
    held, creator_descriptor = captured[0]
    assert close_calls == 2
    assert held.descriptor == creator_descriptor
    assert os.fstat(creator_descriptor).st_ino == (album / held.name).stat().st_ino
    probe = os.open(album / "probe", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        assert probe != creator_descriptor
    finally:
        os.close(probe)

    held.close()
    assert close_calls == 3
    _assert_descriptor_closed(creator_descriptor)


def test_dropped_interrupted_write_lease_finalizer_closes_creator_descriptor(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_acquire = release_identity.acquire_inode_write_lease
    creator_descriptors = []

    def interrupt_every_explicit_close(descriptor):
        creator_descriptors.append(descriptor.fileno())
        exclusion = real_acquire(descriptor)
        assert exclusion is not None

        def close():
            raise KeyboardInterrupt

        exclusion.close = close
        return exclusion

    monkeypatch.setattr(
        release_identity,
        "acquire_inode_write_lease",
        interrupt_every_explicit_close,
    )
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stop publication")),
    )

    with pytest.raises(RuntimeError, match="stop publication"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert len(creator_descriptors) == 1
    creator_descriptor = creator_descriptors[0]
    gc.collect()
    _assert_descriptor_closed(creator_descriptor)
    artifact = _transaction_artifacts(album)[0]
    assert _nonblocking_writer_errno(artifact) == 0


def test_lost_write_lease_return_cannot_close_reused_descriptor(
    tmp_path, live_run_lock, monkeypatch
):
    from qobuz_librarian.library import release_identity

    lease, _lock_file = live_run_lock
    album = _album(tmp_path)
    real_acquire = release_identity.acquire_inode_write_lease
    replacement_descriptors = []

    def finalize_before_return(descriptor):
        creator_descriptor = (
            descriptor.fileno()
            if hasattr(descriptor, "fileno")
            else descriptor
        )
        exclusion = real_acquire(descriptor)
        assert exclusion is not None
        del exclusion
        gc.collect()
        replacement = os.open(
            tmp_path / "replacement-fd",
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        assert replacement == creator_descriptor
        replacement_descriptors.append(replacement)
        return None

    monkeypatch.setattr(
        release_identity,
        "acquire_inode_write_lease",
        finalize_before_return,
    )

    with pytest.raises(ReleaseManifestError, match="protected from writers"):
        with open_album_authority(album, lease) as authority:
            release_identity.publish_release_identity_authorized(
                authority,
                ReleaseIdentity("qobuz", "123"),
            )

    assert len(replacement_descriptors) == 1
    replacement = replacement_descriptors[0]
    try:
        assert stat.S_ISREG(os.fstat(replacement).st_mode)
    finally:
        try:
            os.close(replacement)
        except OSError as exc:
            assert exc.errno == errno.EBADF
