"""Fail-closed Linux exclusion for writers to a held regular-file inode."""

from __future__ import annotations

import fcntl
import io
import os
import signal
import stat
import struct
import sys
import threading
import weakref
from contextlib import contextmanager
from typing import Iterator

# Python does not expose F_SETOWN_EX, but its Linux ABI value and f_owner_ex
# layout are stable. Leases share a signal-waiting thread without changing the
# process-wide signal handler.
_F_SETOWN_EX = 15
_F_OWNER_TID = 0


class _LeaseSignalTarget:
    _shared_lock = threading.RLock()
    _shared: _LeaseSignalTarget | None = None

    def __init__(self):
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._failed = False
        self._references = set()
        self.tid: int | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="ql-file-lease-signal",
            # Kernel teardown closes every process fd and therefore every
            # lease.  A forgotten main-module owner must never make this
            # process-local signal receiver hold interpreter exit hostage.
            daemon=True,
        )

    @classmethod
    def acquire(cls) -> _LeaseSignalReference | None:
        reference = None
        try:
            with cls._shared_lock:
                target = cls._shared
                if target is not None:
                    if not target.usable():
                        return None
                    reference = _LeaseSignalReference(target)
                    reference._arm_locked()
            if reference is not None:
                return reference
        except BaseException:
            if reference is not None:
                reference.close()
            raise

        # Starting and stopping a thread can join it.  Neither is allowed
        # while holding _shared_lock because the receiver also takes that lock
        # during self-reaping.  Concurrent starters are harmless: publication
        # below selects one winner and deterministically shuts down the rest.
        new_target = cls()
        reference = None
        winner_reference = None
        published = False
        try:
            reference = _LeaseSignalReference(new_target)
            with cls._shared_lock:
                reference._arm_locked()
            if not new_target._start():
                reference.close()
                reference = None
                return None

            with cls._shared_lock:
                target = cls._shared
                if target is None:
                    cls._shared = new_target
                    published = True
                elif target.usable():
                    winner_reference = _LeaseSignalReference(target)
                    winner_reference._arm_locked()

            if published:
                return reference
            reference.close()
            reference = None
            return winner_reference
        except BaseException:
            if winner_reference is not None:
                winner_reference.close()
            if reference is not None:
                reference.close()
            raise
        finally:
            if not published:
                new_target._shutdown()

    def _start(self) -> bool:
        try:
            self._thread.start()
        except RuntimeError:
            self._shutdown()
            return False
        except BaseException:
            self._shutdown()
            raise
        try:
            if not self._ready.wait(timeout=2) or not self.usable():
                self._shutdown()
                return False
            return True
        except BaseException:
            self._shutdown()
            raise

    def _run(self) -> None:
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGIO})
            self.tid = threading.get_native_id()
        except (AttributeError, OSError, RuntimeError, ValueError):
            self._failed = True
            self._ready.set()
            return
        self._ready.set()
        while not self._stop.is_set():
            try:
                signal.sigtimedwait({signal.SIGIO}, 0.05)
            except InterruptedError:
                continue
            except (AttributeError, OSError, RuntimeError, ValueError):
                self._failed = True
                return

            # A lost return value must not strand a live receiver.  The target
            # owns only weak owner references, so it can reap itself even when
            # finalisation was interrupted before explicit release.
            with self._shared_lock:
                self._prune_references_locked()
                if not self._references:
                    self._stop.set()
                    if type(self)._shared is self:
                        type(self)._shared = None
                    return

    def usable(self) -> bool:
        return (
            not self._failed
            and not self._stop.is_set()
            and self.tid is not None
            and self._thread.is_alive()
        )

    def _prune_references_locked(self) -> None:
        retained = set()
        for reference in self._references:
            owner = reference()
            if (
                owner is not None
                and not owner.closing
                and owner.finalizer_alive
            ):
                retained.add(reference)
        self._references = retained

    def _register_locked(self, reference) -> None:
        self._prune_references_locked()
        self._references.add(weakref.ref(reference))

    def release(self, token) -> None:
        thread = None
        with self._shared_lock:
            retained = set()
            for reference in self._references:
                owner = reference()
                if (
                    owner is not None
                    and owner._token is not token
                    and not owner.closing
                    and owner.finalizer_alive
                ):
                    retained.add(reference)
            self._references = retained
            if self._references:
                return
            self._stop.set()
            if type(self)._shared is self:
                type(self)._shared = None
            thread = self._thread
        if (
            thread.ident is not None
            and threading.current_thread() is not thread
        ):
            thread.join()

    def _shutdown(self) -> None:
        self._stop.set()
        if (
            self._thread.ident is not None
            and threading.current_thread() is not self._thread
        ):
            self._thread.join()
        with self._shared_lock:
            if type(self)._shared is self:
                type(self)._shared = None


class _LeaseReferenceToken:
    pass


class _LeaseSignalReference:
    """One finalisable ownership token for the shared signal target."""

    def __init__(self, target: _LeaseSignalTarget):
        self._target = target
        self._token = _LeaseReferenceToken()
        self._closing = False
        self._closed = False
        self._finalizer = weakref.finalize(
            self,
            target.release,
            self._token,
        )

    def _arm_locked(self) -> None:
        # The finaliser already owns the token before registration.  An
        # asynchronous exception immediately after registration therefore
        # unwinds through an owner that can release it.
        self._target._register_locked(self)

    @property
    def closing(self) -> bool:
        return self._closing or self._closed

    @property
    def finalizer_alive(self) -> bool:
        return self._finalizer.alive

    @property
    def tid(self) -> int | None:
        return self._target.tid

    def usable(self) -> bool:
        return (
            not self.closing
            and self._finalizer.alive
            and self._target.usable()
        )

    def close(self) -> None:
        if self._closed:
            return
        # Publish the closing state before the potentially interruptible join.
        # The target's own thread prunes closing references, so a retained
        # traceback cannot keep the signal target alive after an interrupted
        # explicit close.
        self._closing = True
        self._target.release(self._token)
        self._closed = True
        self._finalizer.detach()


class _InodeLeaseLifetime:
    """Resources behind one lease; held only by its one-shot finaliser."""

    def __init__(self, descriptor: io.FileIO,
                 signal_reference: _LeaseSignalReference):
        self.descriptor = descriptor
        self.signal_reference = signal_reference
        self.lock = threading.RLock()

    def intact(self) -> bool:
        with self.lock:
            if (
                self.descriptor.closed
                or not self.signal_reference.usable()
            ):
                return False
            try:
                return (
                    fcntl.fcntl(
                        self.descriptor.fileno(), fcntl.F_GETLEASE)
                    == fcntl.F_RDLCK
                )
            except (OSError, TypeError, ValueError):
                return False

    def release(self) -> None:
        # FileIO and the signal reference each have their own finalisation.
        # If this callback itself is interrupted, unwinding this lifetime still
        # closes the stable descriptor and drops the target token.
        with self.lock:
            try:
                if not self.descriptor.closed:
                    try:
                        fcntl.fcntl(
                            self.descriptor.fileno(),
                            fcntl.F_SETLEASE,
                            fcntl.F_UNLCK,
                        )
                    except (OSError, TypeError, ValueError):
                        pass
            finally:
                try:
                    self.descriptor.close()
                finally:
                    self.signal_reference.close()


class InodeWriteExclusion:
    """A read lease that excludes existing and newly opened writers."""

    def __init__(self, descriptor: io.FileIO,
                 signal_reference: _LeaseSignalReference):
        lifetime = _InodeLeaseLifetime(descriptor, signal_reference)
        self._lifetime = weakref.ref(lifetime)
        self._finalizer = weakref.finalize(self, lifetime.release)

    def intact(self) -> bool:
        if not self._finalizer.alive:
            return False
        lifetime = self._lifetime()
        return lifetime is not None and lifetime.intact()

    def close(self) -> None:
        if not self._finalizer.alive:
            return
        lifetime = self._lifetime()
        if lifetime is None:
            return
        # Do not consume the one-shot finalizer before cleanup.  If an
        # asynchronous exception interrupts release, this method remains
        # retryable and the lifetime's FileIO descriptor cannot be confused
        # with a subsequently reused raw fd number.
        lifetime.release()
        self._finalizer.detach()

    def __enter__(self) -> InodeWriteExclusion:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


@contextmanager
def inode_write_exclusion_scope() -> Iterator[bool]:
    """Keep the shared signal receiver alive across sequential leases.

    A library scan opens and closes one inode at a time.  Without a retained
    reference, every close would synchronously stop the signal-wait thread and
    every next file would start another one.  The scope owns no inode lease;
    it only keeps the process-local signal target reusable until the bounded
    operation finishes, when the ordinary reference-counted shutdown still
    joins it deterministically.
    """
    signal_reference = _LeaseSignalTarget.acquire()
    if signal_reference is None:
        yield False
        return
    try:
        yield True
    finally:
        signal_reference.close()


def acquire_inode_write_exclusion(descriptor: int) -> InodeWriteExclusion | None:
    """Acquire a nonblocking writer exclusion, or refuse without mutation.

    A Linux read lease fails with EAGAIN when another writable descriptor or
    shared writable mapping already exists. Once acquired, later opens for
    writing and truncates cannot proceed until the lease is released. A break
    request makes ``intact()`` false before the kernel admits the writer.
    """
    required = (
        "F_GETLEASE",
        "F_SETLEASE",
        "F_RDLCK",
        "F_UNLCK",
        "F_SETSIG",
    )
    if sys.platform != "linux" or any(not hasattr(fcntl, name) for name in required):
        return None
    if not all(
        hasattr(signal, name) for name in ("SIGIO", "SIG_BLOCK", "pthread_sigmask", "sigtimedwait")
    ):
        return None
    owned_descriptor = None
    signal_reference = None
    lease_set = False
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        path_flag = getattr(os, "O_PATH", 0)
        if (
            (flags & os.O_ACCMODE) != os.O_RDONLY
            or (path_flag and flags & path_flag)
            or not stat.S_ISREG(os.fstat(descriptor).st_mode)
        ):
            return None
        original = os.fstat(descriptor)
        # FileIO adopts its descriptor in one C-level construction and closes
        # it if the Python return value is lost.  A raw os.dup() integer would
        # introduce the same CALL-to-STORE leak this owner is meant to close.
        owned_descriptor = io.FileIO(
            f"/proc/self/fd/{descriptor}", "rb", closefd=True)
        if (
            not stat.S_ISREG(os.fstat(owned_descriptor.fileno()).st_mode)
            or (
                int(original.st_dev),
                int(original.st_ino),
                stat.S_IFMT(original.st_mode),
            )
            != (
                int(os.fstat(owned_descriptor.fileno()).st_dev),
                int(os.fstat(owned_descriptor.fileno()).st_ino),
                stat.S_IFMT(os.fstat(owned_descriptor.fileno()).st_mode),
            )
        ):
            return None
        signal_reference = _LeaseSignalTarget.acquire()
        if signal_reference is None:
            return None
        owned_fd = owned_descriptor.fileno()
        fcntl.fcntl(
            owned_fd,
            _F_SETOWN_EX,
            struct.pack("=ii", _F_OWNER_TID, signal_reference.tid),
        )
        fcntl.fcntl(owned_fd, fcntl.F_SETSIG, signal.SIGIO)
        fcntl.fcntl(owned_fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        lease_set = True
        exclusion = InodeWriteExclusion(
            owned_descriptor, signal_reference)
        if exclusion.intact():
            owned_descriptor = None
            signal_reference = None
            return exclusion
        exclusion.close()
        return None
    except (OSError, TypeError, ValueError, struct.error):
        if lease_set:
            try:
                fcntl.fcntl(
                    owned_descriptor.fileno(),
                    fcntl.F_SETLEASE,
                    fcntl.F_UNLCK,
                )
            except (OSError, TypeError, ValueError):
                pass
        return None
    except BaseException:
        if lease_set:
            try:
                fcntl.fcntl(
                    owned_descriptor.fileno(),
                    fcntl.F_SETLEASE,
                    fcntl.F_UNLCK,
                )
            except (OSError, TypeError, ValueError):
                pass
        raise
    finally:
        if owned_descriptor is not None:
            owned_descriptor.close()
        if signal_reference is not None:
            signal_reference.close()
