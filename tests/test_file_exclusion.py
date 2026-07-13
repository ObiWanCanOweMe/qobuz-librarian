import errno
import fcntl
import mmap
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

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


def test_inode_write_exclusion_detects_a_later_writer_and_stops_its_waiter(tmp_path):
    target = tmp_path / "track.flac"
    target.write_bytes(b"audio")
    lease_fd = os.open(target, os.O_RDONLY)
    exclusion = file_exclusion.acquire_inode_write_exclusion(lease_fd)
    assert exclusion is not None
    try:
        with pytest.raises(BlockingIOError) as refused:
            os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        assert refused.value.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
        assert exclusion.intact() is False
    finally:
        exclusion.close()
        os.close(lease_fd)

    writer_fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer_fd)


def test_exclusion_owns_a_stable_fd_after_the_callers_fd_is_reused(tmp_path):
    target = tmp_path / "track.flac"
    decoy = tmp_path / "decoy.flac"
    target.write_bytes(b"audio")
    decoy.write_bytes(b"decoy")
    caller_fd = os.open(target, os.O_RDONLY)
    original_number = caller_fd
    exclusion = file_exclusion.acquire_inode_write_exclusion(caller_fd)
    assert exclusion is not None
    os.close(caller_fd)
    reused_fd = os.open(decoy, os.O_RDONLY)
    assert reused_fd == original_number
    try:
        with pytest.raises(BlockingIOError):
            os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        exclusion.close()
        assert os.fstat(reused_fd).st_size == len(b"decoy")
    finally:
        exclusion.close()
        os.close(reused_fd)

    writer_fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer_fd)


def test_lost_signal_reference_self_reaps_after_interrupted_finalizer(
        monkeypatch):
    real_release = file_exclusion._LeaseSignalTarget.release
    interrupted = False

    def interrupt_once(target, token):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_release(target, token)

    monkeypatch.setattr(
        file_exclusion._LeaseSignalTarget, "release", interrupt_once)
    reference = file_exclusion._LeaseSignalTarget.acquire()
    assert reference is not None
    receiver = reference._target._thread
    with pytest.raises(KeyboardInterrupt):
        reference._finalizer()

    deadline = time.monotonic() + 2
    while receiver.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not receiver.is_alive()
    assert file_exclusion._LeaseSignalTarget._shared is None


@pytest.mark.parametrize("scenario", ["retained-owner", "startup-interrupt"])
def test_signal_receiver_never_holds_interpreter_exit(scenario):
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    setup = """
        import os
        import tempfile
        import time
        from qobuz_librarian import file_exclusion as fe
    """
    if scenario == "retained-owner":
        body = """
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "track.flac")
                with open(path, "wb") as stream:
                    stream.write(b"audio")
                descriptor = os.open(path, os.O_RDONLY)
                retained = fe.acquire_inode_write_exclusion(descriptor)
                assert retained is not None
                os.close(descriptor)
        """
    else:
        body = """
            real_init = fe._LeaseSignalTarget.__init__

            class InterruptingReady:
                def __init__(self, event):
                    self.event = event

                def set(self):
                    self.event.set()

                def wait(self, timeout=None):
                    assert self.event.wait(timeout)
                    time.sleep(0.1)
                    raise KeyboardInterrupt

            def install_interrupting_ready(self):
                real_init(self)
                self._ready = InterruptingReady(self._ready)

            fe._LeaseSignalTarget.__init__ = install_interrupting_ready
            try:
                fe._LeaseSignalTarget.acquire()
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("startup interruption was not injected")
        """
    code = textwrap.dedent(setup) + textwrap.dedent(body)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": source_root},
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_interrupted_explicit_close_is_retryable(tmp_path):
    target = tmp_path / "track.flac"
    target.write_bytes(b"audio")
    descriptor = os.open(target, os.O_RDONLY)
    exclusion = file_exclusion.acquire_inode_write_exclusion(descriptor)
    assert exclusion is not None
    lifetime = exclusion._lifetime()
    assert lifetime is not None
    real_lock = lifetime.lock

    class InterruptOnce:
        def __init__(self):
            self.interrupted = False

        def __enter__(self):
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            real_lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            real_lock.release()

    lifetime.lock = InterruptOnce()
    try:
        with pytest.raises(KeyboardInterrupt):
            exclusion.close()
        with pytest.raises(BlockingIOError):
            os.open(target, os.O_WRONLY | os.O_NONBLOCK)

        lifetime.lock = real_lock
        exclusion.close()
    finally:
        lifetime.lock = real_lock
        exclusion.close()
        os.close(descriptor)

    writer_fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer_fd)


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
