"""Live descriptor and inode-lease authority for one album namespace."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from qobuz_librarian.file_exclusion import (
    InodeWriteExclusion,
    acquire_inode_write_exclusion,
)
from qobuz_librarian.library.release_identity import (
    DirectoryPathReceipt,
    ReleaseManifestError,
    capture_directory_path_receipt,
    directory_path_receipt_matches,
)
from qobuz_librarian.run_lock import RunLockLease


@dataclass(frozen=True, slots=True)
class FileVersion:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class HeldFileAuthority:
    relative: Path
    descriptor: int
    version: FileVersion
    digest: str
    exclusion: InodeWriteExclusion


class AlbumAuthorityUnavailable(OSError):
    """The requested album mutation cannot be authorised safely."""


@dataclass(slots=True)
class _DirectoryBinding:
    descriptor: int
    parent_descriptor: int | None
    name: str | None
    version: FileVersion


@dataclass(slots=True)
class _FileBinding:
    held: HeldFileAuthority
    parents: tuple[_DirectoryBinding, ...]
    parent_descriptor: int
    name: str


def file_version(value: os.stat_result) -> FileVersion:
    return FileVersion(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        ctime_ns=int(value.st_ctime_ns),
    )


def _identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise AlbumAuthorityUnavailable(
            "safe no-follow album directory access is unavailable"
        )
    return (
        os.O_RDONLY
        | nofollow
        | directory
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise AlbumAuthorityUnavailable(
            "safe no-follow album file access is unavailable"
        )
    return (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _safe_relative(relative: Path) -> tuple[Path, tuple[str, ...], bytes]:
    if not isinstance(relative, Path) or relative.is_absolute():
        raise AlbumAuthorityUnavailable("unsafe relative path for album authority")
    parts = tuple(relative.parts)
    if (
        not parts
        or any(
            not isinstance(part, str)
            or part in {"", ".", ".."}
            or "\x00" in part
            for part in parts
        )
    ):
        raise AlbumAuthorityUnavailable("unsafe relative path for album authority")
    normalised = Path(*parts)
    return normalised, parts, b"/".join(os.fsencode(part) for part in parts)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _close_descriptor(descriptor: int) -> BaseException | None:
    try:
        os.close(descriptor)
    except BaseException as exc:
        return exc
    return None


def _close_partial_file_resources(exclusion, descriptor, parents):
    error = None
    if exclusion is not None:
        try:
            exclusion.close()
        except BaseException as exc:
            error = exc
    if descriptor >= 0:
        caught = _close_descriptor(descriptor)
        if error is None and caught is not None:
            error = caught
    for binding in reversed(parents):
        caught = _close_descriptor(binding.descriptor)
        if error is None and caught is not None:
            error = caught
    return error


def _directory_binding_intact(binding: _DirectoryBinding) -> bool:
    try:
        held = os.fstat(binding.descriptor)
        if not stat.S_ISDIR(held.st_mode) or file_version(held) != binding.version:
            return False
        if binding.parent_descriptor is None:
            named = os.stat(os.path.sep, follow_symlinks=False)
        else:
            named = os.stat(
                binding.name,
                dir_fd=binding.parent_descriptor,
                follow_symlinks=False,
            )
        return stat.S_ISDIR(named.st_mode) and file_version(named) == binding.version
    except (OSError, TypeError, ValueError):
        return False


class AlbumAuthority:
    """Context-owned album path, file descriptors, and writer exclusions."""

    def __init__(
        self,
        path: Path,
        authority: RunLockLease,
        expected_path: DirectoryPathReceipt | None,
    ):
        self.path = Path(path)
        self._run_lock = authority
        self._expected_path = expected_path
        self._directories: list[_DirectoryBinding] = []
        self._files: list[_FileBinding] = []
        self._files_by_relative: dict[Path, _FileBinding] = {}
        self._last_file_key: bytes | None = None
        self._entered = False
        self._closed = False
        self._lock = threading.RLock()

    @property
    def directory_descriptor(self) -> int:
        with self._lock:
            self._require_open()
            return self._directories[-1].descriptor

    @property
    def path_receipt(self) -> DirectoryPathReceipt:
        with self._lock:
            self._require_open()
            assert self._expected_path is not None
            return self._expected_path

    def _require_run_lock(self) -> None:
        if type(self._run_lock) is not RunLockLease or not self._run_lock.intact():
            raise AlbumAuthorityUnavailable(
                "album authority requires an exact live run-lock lease"
            )

    def _require_open(self) -> None:
        if not self._entered or self._closed or not self._directories:
            raise AlbumAuthorityUnavailable("album authority is not live")

    def _capture_expected_path(self) -> DirectoryPathReceipt:
        try:
            absolute = os.path.abspath(os.fspath(self.path))
        except (OSError, TypeError, ValueError) as exc:
            raise AlbumAuthorityUnavailable("album directory path is invalid") from exc
        if not absolute or "\x00" in absolute:
            raise AlbumAuthorityUnavailable("album directory path is invalid")
        expected = self._expected_path
        if expected is None:
            try:
                expected = capture_directory_path_receipt(Path(absolute))
            except (ReleaseManifestError, OSError) as exc:
                raise AlbumAuthorityUnavailable(
                    "album directory could not be captured safely"
                ) from exc
        if (
            not isinstance(expected, DirectoryPathReceipt)
            or not expected.exists
            or expected.path != absolute
            or not directory_path_receipt_matches(expected)
        ):
            raise AlbumAuthorityUnavailable(
                "album directory changed before authority acquisition"
            )
        self._expected_path = expected
        return expected

    def _open_album_chain(self, expected: DirectoryPathReceipt) -> None:
        try:
            parts = Path(expected.path).relative_to(Path(os.path.sep)).parts
        except ValueError as exc:
            raise AlbumAuthorityUnavailable(
                "album directory has no stable filesystem anchor"
            ) from exc
        if len(expected.identities) != len(parts) + 1:
            raise AlbumAuthorityUnavailable("album directory receipt is malformed")
        flags = _directory_flags()
        bindings: list[_DirectoryBinding] = []
        descriptor = -1
        try:
            descriptor = os.open(os.path.sep, flags)
            root_before = os.fstat(descriptor)
            root_named = os.stat(os.path.sep, follow_symlinks=False)
            root_after = os.fstat(descriptor)
            root_version = file_version(root_before)
            if (
                not stat.S_ISDIR(root_before.st_mode)
                or not stat.S_ISDIR(root_named.st_mode)
                or not stat.S_ISDIR(root_after.st_mode)
                or not root_version
                == file_version(root_named)
                == file_version(root_after)
                or _identity(root_after) != expected.identities[0]
            ):
                raise AlbumAuthorityUnavailable(
                    "album directory changed while authority was opened"
                )
            bindings.append(_DirectoryBinding(descriptor, None, None, root_version))
            descriptor = -1

            for index, name in enumerate(parts, 1):
                parent = bindings[-1].descriptor
                named_before = os.stat(
                    name, dir_fd=parent, follow_symlinks=False
                )
                descriptor = os.open(name, flags, dir_fd=parent)
                held = os.fstat(descriptor)
                named_after = os.stat(
                    name, dir_fd=parent, follow_symlinks=False
                )
                version = file_version(held)
                if (
                    not stat.S_ISDIR(named_before.st_mode)
                    or not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(named_after.st_mode)
                    or file_version(named_before) != version
                    or file_version(named_after) != version
                    or _identity(held) != expected.identities[index]
                ):
                    raise AlbumAuthorityUnavailable(
                        "album directory changed while authority was opened"
                    )
                bindings.append(
                    _DirectoryBinding(descriptor, parent, name, version)
                )
                descriptor = -1
            self._directories = bindings
            bindings = []
            if (
                not directory_path_receipt_matches(expected)
                or not all(
                    _directory_binding_intact(binding)
                    for binding in self._directories
                )
            ):
                raise AlbumAuthorityUnavailable(
                    "album directory changed while authority was opened"
                )
        except AlbumAuthorityUnavailable:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise AlbumAuthorityUnavailable(
                "album directory could not be opened without following links"
            ) from exc
        finally:
            if descriptor >= 0:
                _close_descriptor(descriptor)
            for binding in reversed(bindings):
                _close_descriptor(binding.descriptor)

    def __enter__(self) -> AlbumAuthority:
        with self._lock:
            if self._entered or self._closed:
                raise AlbumAuthorityUnavailable("album authority context is one-shot")
            self._entered = True
            try:
                self._require_run_lock()
                expected = self._capture_expected_path()
                self._open_album_chain(expected)
                self._require_run_lock()
                return self
            except BaseException:
                self._close_resources()
                self._closed = True
                raise

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        with self._lock:
            validation_error = None
            try:
                self._require_run_lock()
            except BaseException as caught:
                validation_error = caught
            cleanup_error = self._close_resources()
            self._closed = True
            if exc_type is None:
                if validation_error is not None:
                    raise validation_error
                if cleanup_error is not None:
                    raise cleanup_error

    def _open_child_directories(
        self, parts: tuple[str, ...]
    ) -> tuple[list[_DirectoryBinding], int]:
        parent = self.directory_descriptor
        opened: list[_DirectoryBinding] = []
        descriptor = -1
        try:
            for name in parts:
                named_before = os.stat(
                    name, dir_fd=parent, follow_symlinks=False
                )
                descriptor = os.open(name, _directory_flags(), dir_fd=parent)
                held = os.fstat(descriptor)
                named_after = os.stat(
                    name, dir_fd=parent, follow_symlinks=False
                )
                version = file_version(held)
                if (
                    not stat.S_ISDIR(named_before.st_mode)
                    or not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(named_after.st_mode)
                    or file_version(named_before) != version
                    or file_version(named_after) != version
                ):
                    raise AlbumAuthorityUnavailable(
                        "album file parent namespace changed while it was opened"
                    )
                opened.append(_DirectoryBinding(descriptor, parent, name, version))
                parent = descriptor
                descriptor = -1
            return opened, parent
        except BaseException:
            if descriptor >= 0:
                _close_descriptor(descriptor)
            for binding in reversed(opened):
                _close_descriptor(binding.descriptor)
            raise

    def open_file(
        self,
        relative: Path,
        *,
        expected_digest: str | None = None,
    ) -> HeldFileAuthority:
        with self._lock:
            return self._open_file_locked(
                relative,
                expected_digest=expected_digest,
            )

    def _open_file_locked(
        self,
        relative: Path,
        *,
        expected_digest: str | None,
    ) -> HeldFileAuthority:
        self._require_open()
        self._require_run_lock()
        normalised, parts, key = _safe_relative(relative)
        existing = self._files_by_relative.get(normalised)
        if existing is not None:
            self._validate_namespace_locked()
            if expected_digest is not None and not secrets.compare_digest(
                existing.held.digest, expected_digest
            ):
                raise AlbumAuthorityUnavailable(
                    "album file digest differs from expected evidence"
                )
            return existing.held
        if self._last_file_key is not None and key <= self._last_file_key:
            raise AlbumAuthorityUnavailable(
                "album files must be acquired in stable bytewise order"
            )

        parents: list[_DirectoryBinding] = []
        descriptor = -1
        exclusion = None
        try:
            parents, parent = self._open_child_directories(parts[:-1])
            name = parts[-1]
            named_before = os.stat(
                name, dir_fd=parent, follow_symlinks=False
            )
            descriptor = os.open(name, _regular_flags(), dir_fd=parent)
            held_before = os.fstat(descriptor)
            version = file_version(held_before)
            if (
                not stat.S_ISREG(named_before.st_mode)
                or not stat.S_ISREG(held_before.st_mode)
                or named_before.st_nlink != 1
                or held_before.st_nlink != 1
                or file_version(named_before) != version
            ):
                raise AlbumAuthorityUnavailable(
                    "album authority requires a regular unlinked file"
                )

            exclusion = acquire_inode_write_exclusion(descriptor)
            if exclusion is None:
                raise AlbumAuthorityUnavailable(
                    "album file could not be protected from writers"
                )
            named_after_open = os.stat(
                name, dir_fd=parent, follow_symlinks=False
            )
            held_after_lease = os.fstat(descriptor)
            if (
                not exclusion.intact()
                or not stat.S_ISREG(named_after_open.st_mode)
                or not stat.S_ISREG(held_after_lease.st_mode)
                or named_after_open.st_nlink != 1
                or held_after_lease.st_nlink != 1
                or file_version(named_after_open) != version
                or file_version(held_after_lease) != version
            ):
                raise AlbumAuthorityUnavailable(
                    "album file changed while authority was acquired"
                )
            digest = _sha256_fd(descriptor)
            held_after = os.fstat(descriptor)
            named_final = os.stat(
                name, dir_fd=parent, follow_symlinks=False
            )
            if (
                not exclusion.intact()
                or not stat.S_ISREG(held_after.st_mode)
                or not stat.S_ISREG(named_final.st_mode)
                or held_after.st_nlink != 1
                or named_final.st_nlink != 1
                or file_version(held_after) != version
                or file_version(named_final) != version
                or not all(_directory_binding_intact(item) for item in parents)
                or not self._album_namespace_intact()
            ):
                raise AlbumAuthorityUnavailable(
                    "album file changed while authority was acquired"
                )
            if expected_digest is not None and not secrets.compare_digest(
                digest, expected_digest
            ):
                raise AlbumAuthorityUnavailable(
                    "album file digest differs from expected evidence"
                )
            held = HeldFileAuthority(
                normalised,
                descriptor,
                version,
                digest,
                exclusion,
            )
            binding = _FileBinding(
                held,
                tuple(parents),
                parent,
                name,
            )
            self._validate_namespace_locked((binding,))
            self._files.append(binding)
            self._files_by_relative[normalised] = binding
            self._last_file_key = key
            parents = []
            descriptor = -1
            exclusion = None
            return held
        except AlbumAuthorityUnavailable:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise AlbumAuthorityUnavailable(
                "album authority requires a regular unlinked file"
            ) from exc
        finally:
            cleanup_error = _close_partial_file_resources(
                exclusion, descriptor, parents
            )
            if cleanup_error is not None and sys.exc_info()[0] is None:
                raise cleanup_error

    def _album_namespace_intact(self) -> bool:
        expected = self._expected_path
        return bool(
            expected is not None
            and directory_path_receipt_matches(expected)
            and all(
                _directory_binding_intact(binding)
                for binding in self._directories
            )
        )

    def validate_namespace(self) -> None:
        with self._lock:
            self._validate_namespace_locked()

    def _validate_namespace_locked(
        self,
        additional: tuple[_FileBinding, ...] = (),
    ) -> None:
        self._require_open()
        self._require_run_lock()
        if not self._album_namespace_intact():
            raise AlbumAuthorityUnavailable("album namespace changed under authority")
        for binding in (*self._files, *additional):
            try:
                held_value = os.fstat(binding.held.descriptor)
                named_value = os.stat(
                    binding.name,
                    dir_fd=binding.parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise AlbumAuthorityUnavailable(
                    "album file namespace changed under authority"
                ) from exc
            if (
                not binding.held.exclusion.intact()
                or not stat.S_ISREG(held_value.st_mode)
                or not stat.S_ISREG(named_value.st_mode)
                or held_value.st_nlink != 1
                or named_value.st_nlink != 1
                or file_version(held_value) != binding.held.version
                or file_version(named_value) != binding.held.version
                or not all(
                    _directory_binding_intact(parent)
                    for parent in binding.parents
                )
            ):
                raise AlbumAuthorityUnavailable(
                    "album file namespace changed under authority"
                )
        self._require_run_lock()

    def _close_resources(self) -> BaseException | None:
        error = None
        for binding in reversed(self._files):
            try:
                binding.held.exclusion.close()
            except BaseException as exc:
                if error is None:
                    error = exc
            caught = _close_descriptor(binding.held.descriptor)
            if error is None and caught is not None:
                error = caught
            for parent in reversed(binding.parents):
                caught = _close_descriptor(parent.descriptor)
                if error is None and caught is not None:
                    error = caught
        self._files.clear()
        self._files_by_relative.clear()
        for binding in reversed(self._directories):
            caught = _close_descriptor(binding.descriptor)
            if error is None and caught is not None:
                error = caught
        self._directories.clear()
        return error


def open_album_authority(
    path: Path,
    authority: RunLockLease,
    *,
    expected_path: DirectoryPathReceipt | None = None,
) -> AbstractContextManager[AlbumAuthority]:
    """Return a one-shot context that owns live authority over *path*."""
    if sys.platform != "linux":
        raise AlbumAuthorityUnavailable(
            "album authority requires the supported Linux lease backend"
        )
    return AlbumAuthority(Path(path), authority, expected_path)
