import errno
import fcntl
import mmap
import os

import pytest

from qobuz_librarian import file_exclusion


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
