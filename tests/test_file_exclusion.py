import errno
import fcntl
import gc
import io
import mmap
import os
import stat
import subprocess
import sys
import weakref

import pytest

from qobuz_librarian import file_exclusion


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


@pytest.mark.parametrize("busy_kind", ["writer", "writable-mapping"])
def test_inode_write_exclusion_refuses_existing_writers(tmp_path, busy_kind):
    target = tmp_path / "track.flac"
    target.write_bytes(b"audio")
    lease_fd = os.open(target, os.O_RDONLY)
    writer_fd = os.open(target, os.O_RDWR)
    mapping = None
    try:
        if busy_kind == "writable-mapping":
            mapping = mmap.mmap(writer_fd, 5, access=mmap.ACCESS_WRITE)
            os.close(writer_fd)
            writer_fd = None

        assert file_exclusion.acquire_inode_write_exclusion(lease_fd) is None
        assert target.read_bytes() == b"audio"
    finally:
        if mapping is not None:
            mapping.close()
        if writer_fd is not None:
            os.close(writer_fd)
        os.close(lease_fd)


def test_inode_write_exclusion_fails_closed_when_the_kernel_refuses_it(
    tmp_path, monkeypatch
):
    target = tmp_path / "track.flac"
    target.write_bytes(b"audio")
    lease_fd = os.open(target, os.O_RDONLY)
    real_fcntl = file_exclusion.fcntl.fcntl

    def refuse_lease(descriptor, command, *args):
        if command == fcntl.F_SETLEASE and args and args[0] == fcntl.F_RDLCK:
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
        return real_fcntl(descriptor, command, *args)

    monkeypatch.setattr(file_exclusion.fcntl, "fcntl", refuse_lease)
    try:
        assert file_exclusion.acquire_inode_write_exclusion(lease_fd) is None
    finally:
        os.close(lease_fd)

    assert target.read_bytes() == b"audio"


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_inode_write_exclusion_blocks_a_new_writer_until_close(tmp_path):
    target = tmp_path / "track.flac"
    target.write_bytes(b"audio")
    descriptor = os.open(target, os.O_RDONLY)
    exclusion = file_exclusion.acquire_inode_write_exclusion(descriptor)
    assert exclusion is not None
    writer = None
    try:
        writer = _start_waiting_writer(target)
    finally:
        exclusion.close()
        os.close(descriptor)

    assert writer is not None
    stdout, stderr = writer.communicate(timeout=3)
    assert writer.returncode == 0, (stdout, stderr)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_inode_write_lease_uses_original_read_write_descriptor(tmp_path):
    target = tmp_path / "transaction"
    descriptor = io.FileIO(target, "x+b")
    raw_descriptor = descriptor.fileno()
    exclusion = file_exclusion.acquire_inode_write_lease(descriptor)
    assert exclusion is not None
    try:
        assert exclusion.intact() is True
        assert fcntl.fcntl(raw_descriptor, fcntl.F_GETLEASE) == fcntl.F_WRLCK
        os.write(raw_descriptor, b"canonical")
        os.fsync(raw_descriptor)
    finally:
        exclusion.close()
        with pytest.raises(OSError):
            os.fstat(raw_descriptor)

    assert target.read_bytes() == b"canonical"


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_inode_write_lease_break_request_invalidates_authority(tmp_path):
    target = tmp_path / "transaction"
    target.write_bytes(b"canonical")
    descriptor = io.FileIO(target, "r+b")
    raw_descriptor = descriptor.fileno()
    exclusion = file_exclusion.acquire_inode_write_lease(descriptor)
    assert exclusion is not None
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys; "
                    "\ntry: fd=os.open(sys.argv[1], os.O_WRONLY|os.O_NONBLOCK)"
                    "\nexcept OSError as error: sys.exit(error.errno)"
                    "\nelse: os.close(fd); sys.exit(0)"
                ),
                os.fspath(target),
            ],
            check=False,
        )
        assert result.returncode == errno.EAGAIN
        assert exclusion.intact() is False
    finally:
        exclusion.close()
        with pytest.raises(OSError):
            os.fstat(raw_descriptor)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_inode_write_lease_finalizer_closes_creator_fd(tmp_path):
    target = tmp_path / "transaction"
    target.write_bytes(b"canonical")
    descriptor = io.FileIO(target, "r+b")
    raw_descriptor = descriptor.fileno()
    exclusion = file_exclusion.acquire_inode_write_lease(descriptor)
    assert exclusion is not None
    reference = weakref.ref(exclusion)

    del exclusion
    gc.collect()

    assert reference() is None
    with pytest.raises(OSError):
        os.fstat(raw_descriptor)


def test_inode_write_lease_refuses_wrong_access_and_unsupported_platform(
    tmp_path, monkeypatch
):
    target = tmp_path / "transaction"
    target.write_bytes(b"canonical")
    read_descriptor = io.FileIO(target, "rb")
    try:
        assert file_exclusion.acquire_inode_write_lease(read_descriptor) is None
    finally:
        read_descriptor.close()

    descriptor = io.FileIO(target, "r+b")
    try:
        monkeypatch.setattr(file_exclusion.sys, "platform", "unsupported")
        assert file_exclusion.acquire_inode_write_lease(descriptor) is None
    finally:
        descriptor.close()


def test_inode_write_lease_fails_closed_when_kernel_refuses_it(
    tmp_path, monkeypatch
):
    target = tmp_path / "transaction"
    target.write_bytes(b"canonical")
    descriptor = io.FileIO(target, "r+b")
    real_fcntl = file_exclusion.fcntl.fcntl

    def refuse_lease(held, command, *args):
        if command == fcntl.F_SETLEASE and args and args[0] == fcntl.F_WRLCK:
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
        return real_fcntl(held, command, *args)

    monkeypatch.setattr(file_exclusion.fcntl, "fcntl", refuse_lease)
    try:
        assert file_exclusion.acquire_inode_write_lease(descriptor) is None
        assert os.pread(descriptor.fileno(), 9, 0) == b"canonical"
    finally:
        descriptor.close()


def test_inode_write_lease_constructor_loss_never_unlocks_reused_fd(
    tmp_path, monkeypatch
):
    target = tmp_path / "transaction"
    target.write_bytes(b"canonical")
    replacement_path = tmp_path / "replacement"
    descriptor = io.FileIO(target, "r+b")
    creator_descriptor = descriptor.fileno()
    real_exclusion = file_exclusion.InodeWriteExclusion
    real_fcntl = file_exclusion.fcntl.fcntl
    replacement_descriptors = []
    unsafe_unlocks = []

    def lose_constructor_return(*args, **kwargs):
        exclusion = real_exclusion(*args, **kwargs)
        del exclusion
        gc.collect()
        replacement = os.open(
            replacement_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        assert replacement == creator_descriptor
        replacement_descriptors.append(replacement)
        raise KeyboardInterrupt

    def track_reused_unlock(held, command, *args):
        if (
            replacement_descriptors
            and command == fcntl.F_SETLEASE
            and args
            and args[0] == fcntl.F_UNLCK
        ):
            unsafe_unlocks.append(held)
        return real_fcntl(held, command, *args)

    monkeypatch.setattr(
        file_exclusion,
        "InodeWriteExclusion",
        lose_constructor_return,
    )
    monkeypatch.setattr(file_exclusion.fcntl, "fcntl", track_reused_unlock)

    with pytest.raises(KeyboardInterrupt):
        file_exclusion.acquire_inode_write_lease(descriptor)

    assert len(replacement_descriptors) == 1
    replacement = replacement_descriptors[0]
    try:
        assert unsafe_unlocks == []
        assert stat.S_ISREG(os.fstat(replacement).st_mode)
    finally:
        os.close(replacement)
