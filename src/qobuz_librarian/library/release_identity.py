"""Durable, descriptor-safe identity manifests for managed Qobuz releases."""

from __future__ import annotations

import ctypes
import errno
import io
import json
import os
import secrets
import stat
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path

from qobuz_librarian.file_exclusion import (
    InodeWriteExclusion,
    acquire_inode_write_exclusion,
    acquire_inode_write_lease,
)

MANIFEST_NAME = ".qobuz-librarian-release.json"
MAX_MANIFEST_BYTES = 4096
_FIELDS = frozenset({"schema_version", "provider", "release_id"})
_TRANSACTION_PREFIX = ".qobuz-librarian-release.txn-"
_RENAME_NOREPLACE = 1


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    provider: str
    release_id: str


@dataclass(frozen=True, slots=True)
class DirectoryPathReceipt:
    """Exact no-follow binding for one absolute directory pathname.

    ``identities`` starts with the filesystem root and continues through every
    existing directory component.  ``missing`` is the uncreated tail, if any.
    """

    path: str
    identities: tuple[tuple[int, int], ...]
    missing: tuple[str, ...] = ()

    @property
    def exists(self) -> bool:
        return not self.missing

    @property
    def directory_identity(self) -> tuple[int, int] | None:
        return self.identities[-1] if self.exists else None


class ReleaseManifestError(OSError):
    pass


def normalise_release_id(value):
    if isinstance(value, bool) or value is None:
        return None
    value = str(value).strip()
    return value if value and "\x00" not in value and "/" not in value else None


def identity_from_album(album):
    release_id = normalise_release_id((album or {}).get("id"))
    return ReleaseIdentity("qobuz", release_id) if release_id else None


def is_release_manifest_name(name):
    return isinstance(name, str) and (
        name == MANIFEST_NAME or name.startswith(_TRANSACTION_PREFIX)
    )


def is_ignored_library_artifact(name):
    return isinstance(name, str) and (
        name.startswith("._") or is_release_manifest_name(name)
    )


def _identity(value):
    return int(value.st_dev), int(value.st_ino)


def _file_version(value):
    return (
        *_identity(value),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _absolute_directory_parts(album_dir: Path) -> tuple[str, tuple[str, ...]]:
    try:
        raw = os.fspath(album_dir)
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseManifestError("album directory path is invalid") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ReleaseManifestError("album directory path is invalid")
    absolute = os.path.abspath(raw)
    try:
        parts = Path(absolute).relative_to(Path(os.path.sep)).parts
    except ValueError as exc:
        raise ReleaseManifestError(
            "album directory path has no stable filesystem anchor"
        ) from exc
    return absolute, tuple(parts)


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise ReleaseManifestError("safe album directory access is unavailable")
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _close_descriptors(descriptors):
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _named_directory_matches(parent_descriptor: int, name: str, descriptor: int) -> bool:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(held.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and _identity(held) == _identity(named)
    )


def _directory_chain_matches(receipt: DirectoryPathReceipt, descriptors) -> bool:
    if (
        not isinstance(receipt, DirectoryPathReceipt)
        or len(descriptors) != len(receipt.identities)
        or not descriptors
    ):
        return False
    try:
        _absolute, parts = _absolute_directory_parts(Path(receipt.path))
        if tuple(_identity(os.fstat(fd)) for fd in descriptors) != receipt.identities:
            return False
        root_named = os.stat(os.path.sep, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_named.st_mode)
            or _identity(root_named) != receipt.identities[0]
        ):
            return False
        existing_names = parts[:len(receipt.identities) - 1]
        if not all(
            _named_directory_matches(descriptors[index], name, descriptors[index + 1])
            for index, name in enumerate(existing_names)
        ):
            return False
        if receipt.missing:
            if tuple(parts[len(existing_names):]) != receipt.missing:
                return False
            try:
                os.stat(
                    receipt.missing[0],
                    dir_fd=descriptors[-1],
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return False
        return len(existing_names) == len(parts)
    except (OSError, TypeError, ValueError):
        return False


def _open_directory_path(album_dir: Path, *, allow_missing: bool):
    absolute, parts = _absolute_directory_parts(album_dir)
    flags = _directory_flags()
    descriptors = [os.open(os.path.sep, flags)]
    try:
        for index, name in enumerate(parts):
            try:
                descriptor = os.open(name, flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                if not allow_missing:
                    raise ReleaseManifestError(
                        f"cannot open album directory: {absolute}"
                    ) from None
                receipt = DirectoryPathReceipt(
                    absolute,
                    tuple(_identity(os.fstat(fd)) for fd in descriptors),
                    tuple(parts[index:]),
                )
                if not _directory_chain_matches(receipt, descriptors):
                    raise ReleaseManifestError(
                        "album directory path changed while it was opened"
                    )
                return receipt, tuple(descriptors)
            except OSError as exc:
                raise ReleaseManifestError(
                    f"cannot open album directory: {absolute}"
                ) from exc
            if not _named_directory_matches(descriptors[-1], name, descriptor):
                os.close(descriptor)
                raise ReleaseManifestError(
                    "album directory path changed while it was opened"
                )
            descriptors.append(descriptor)
        receipt = DirectoryPathReceipt(
            absolute,
            tuple(_identity(os.fstat(fd)) for fd in descriptors),
        )
        if not _directory_chain_matches(receipt, descriptors):
            raise ReleaseManifestError(
                "album directory path changed while it was opened"
            )
        return receipt, tuple(descriptors)
    except BaseException:
        _close_descriptors(descriptors)
        raise


def capture_directory_path_receipt(album_dir: Path) -> DirectoryPathReceipt:
    """Capture an exact existing-or-missing no-follow pathname binding."""
    receipt, descriptors = _open_directory_path(album_dir, allow_missing=True)
    _close_descriptors(descriptors)
    return receipt


def directory_path_receipt_matches(receipt: DirectoryPathReceipt) -> bool:
    """Return whether *receipt* still describes the exact public pathname."""
    if not isinstance(receipt, DirectoryPathReceipt):
        return False
    try:
        current, descriptors = _open_directory_path(
            Path(receipt.path), allow_missing=True)
    except ReleaseManifestError:
        return False
    try:
        return current == receipt and _directory_chain_matches(receipt, descriptors)
    finally:
        _close_descriptors(descriptors)


def _open_album_directory(
    album_dir: Path,
    *,
    expected_path_receipt: DirectoryPathReceipt | None = None,
):
    receipt, descriptors = _open_directory_path(album_dir, allow_missing=False)
    if not receipt.exists:
        _close_descriptors(descriptors)
        raise ReleaseManifestError("album path is not a directory")
    if expected_path_receipt is not None and receipt != expected_path_receipt:
        _close_descriptors(descriptors)
        raise ReleaseManifestError("album directory changed before manifest publication")
    return receipt, descriptors


def _manifest_error(message: str, exc: BaseException | None = None):
    if exc is None:
        raise ReleaseManifestError(message)
    raise ReleaseManifestError(message) from exc


def _manifest_identity_from_bytes(contents: bytes) -> ReleaseIdentity:
    try:
        payload = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _manifest_error("release manifest contains invalid JSON", exc)

    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        _manifest_error("release manifest has an invalid schema")
    if payload.get("schema_version") != 1 or type(payload["schema_version"]) is not int:
        _manifest_error("release manifest has an invalid schema")
    if payload.get("provider") != "qobuz" or type(payload["provider"]) is not str:
        _manifest_error("release manifest has an invalid schema")

    release_id = payload.get("release_id")
    normalised = normalise_release_id(release_id)
    if type(release_id) is not str or normalised != release_id:
        _manifest_error("release manifest has an invalid schema")
    return ReleaseIdentity("qobuz", release_id)


def _read_release_identity_at(directory_descriptor: int) -> ReleaseIdentity | None:
    try:
        descriptor = os.open(
            MANIFEST_NAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        try:
            os.stat(
                MANIFEST_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            _manifest_error("cannot examine release manifest", exc)
        _manifest_error("release manifest changed while it was read")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _manifest_error("release manifest is not a regular file", exc)
        _manifest_error("cannot open release manifest", exc)

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _manifest_error("release manifest is not a regular file")
        if before.st_size > MAX_MANIFEST_BYTES:
            _manifest_error("release manifest is too large")
        contents = os.read(descriptor, MAX_MANIFEST_BYTES + 1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or _file_version(before) != _file_version(after)
            or len(contents) != before.st_size
        ):
            _manifest_error("release manifest changed while it was read")
        if len(contents) > MAX_MANIFEST_BYTES:
            _manifest_error("release manifest is too large")
        identity = _manifest_identity_from_bytes(contents)
        named_version = _stable_named_regular_file_version(
            directory_descriptor,
            MANIFEST_NAME,
            descriptor,
            expected_version=_file_version(after),
        )
        final = os.fstat(descriptor)
        if (
            named_version is None
            or not stat.S_ISREG(final.st_mode)
            or _file_version(after) != _file_version(final)
        ):
            _manifest_error("release manifest changed while it was read")
        return identity
    finally:
        os.close(descriptor)


def read_release_identity_with_receipt(
    album_dir: Path,
) -> tuple[ReleaseIdentity | None, DirectoryPathReceipt]:
    """Read an identity and return the exact pathname binding that authorised it."""
    receipt, descriptors = _open_album_directory(album_dir)
    try:
        identity = _read_release_identity_at(descriptors[-1])
        if not _directory_chain_matches(receipt, descriptors):
            raise ReleaseManifestError(
                "album directory changed while release manifest was read"
            )
        return identity, receipt
    finally:
        _close_descriptors(descriptors)


def read_release_identity(album_dir: Path) -> ReleaseIdentity | None:
    """Return a validated identity, ``None`` for no manifest, or raise on invalid data."""
    return read_release_identity_with_receipt(album_dir)[0]


def _validated_identity(identity: ReleaseIdentity) -> ReleaseIdentity:
    if not isinstance(identity, ReleaseIdentity):
        _manifest_error("release identity has an invalid schema")
    release_id = normalise_release_id(identity.release_id)
    if (
        identity.provider != "qobuz"
        or type(identity.provider) is not str
        or type(identity.release_id) is not str
        or release_id != identity.release_id
    ):
        _manifest_error("release identity has an invalid schema")
    return identity


def _manifest_bytes(identity: ReleaseIdentity) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "provider": identity.provider,
                "release_id": identity.release_id,
            },
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )


def _write_all(descriptor: int, contents: bytes):
    offset = 0
    while offset < len(contents):
        written = os.write(descriptor, contents[offset:])
        if written <= 0:
            raise OSError("unable to write release manifest")
        offset += written


def _stable_named_regular_file_version(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    *,
    expected_version=None,
):
    """Return one full version only while the name and held fd stay identical."""
    try:
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        after = os.fstat(descriptor)
    except OSError:
        return None
    versions = (_file_version(before), _file_version(named), _file_version(after))
    if (
        stat.S_ISREG(before.st_mode)
        and stat.S_ISREG(named.st_mode)
        and stat.S_ISREG(after.st_mode)
        and versions[0] == versions[1] == versions[2]
        and (expected_version is None or versions[0] == expected_version)
    ):
        return versions[0]
    return None


def _descriptor_has_exact_contents(
    descriptor: int,
    contents: bytes,
    *,
    expected_version=None,
) -> bool:
    try:
        before = os.fstat(descriptor)
        raw = os.pread(descriptor, len(contents) + 1, 0)
        after = os.fstat(descriptor)
    except OSError:
        return False
    version = _file_version(before)
    return bool(
        stat.S_ISREG(before.st_mode)
        and stat.S_ISREG(after.st_mode)
        and version == _file_version(after)
        and (expected_version is None or version == expected_version)
        and raw == contents
    )


def _publication_entry_matches(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    expected_version,
    contents: bytes,
) -> bool:
    if not _descriptor_has_exact_contents(
        descriptor,
        contents,
        expected_version=expected_version,
    ):
        return False
    if _stable_named_regular_file_version(
        directory_descriptor,
        name,
        descriptor,
        expected_version=expected_version,
    ) is None:
        return False
    final = os.fstat(descriptor)
    return (
        stat.S_ISREG(final.st_mode)
        and _file_version(final) == expected_version
    )


@dataclass(slots=True)
class _HeldManifest:
    name: str
    descriptor: int
    version: tuple[int, int, int, int, int]
    contents: bytes
    exclusion: InodeWriteExclusion

    def close(self) -> None:
        if self.descriptor < 0:
            return
        owns_descriptor = self.exclusion.owns_source_descriptor
        error = None
        for _attempt in range(2):
            try:
                self.exclusion.close()
                error = None
                break
            except BaseException as exc:
                error = exc
        if error is not None:
            # A creator F_WRLCK borrows this exact raw descriptor. Retain the
            # fd number until the lease lifetime has definitely unlocked, so
            # a delayed finalizer can never target a reused unrelated fd.
            if sys.exc_info()[0] is None:
                raise error
            return
        descriptor = self.descriptor
        self.descriptor = -1
        if owns_descriptor:
            return
        try:
            os.close(descriptor)
        except BaseException as exc:
            if error is None:
                error = exc
        if error is not None and sys.exc_info()[0] is None:
            raise error


class _RetainedReleaseManifestLifetime:
    def __init__(self, held: _HeldManifest):
        self.held: _HeldManifest | None = held

    def close(self) -> None:
        held = self.held
        if held is None:
            return
        held.close()
        self.held = None


class RetainedReleaseManifestAuthority:
    """The publisher's exact manifest descriptor and exclusion owner."""

    def __init__(self, album, identity: ReleaseIdentity, held: _HeldManifest):
        self._album = album
        self._identity = identity
        self._lifetime = _RetainedReleaseManifestLifetime(held)
        self._finalizer = weakref.finalize(
            self,
            self._lifetime.close,
        )

    @property
    def descriptor(self) -> int:
        held = self._lifetime.held
        if held is None:
            _manifest_error("release manifest authority is closed")
        return held.descriptor

    def validate_namespace(self) -> None:
        held = self._lifetime.held
        if held is None:
            _manifest_error("release manifest authority is closed")
        self._album.validate_namespace()
        if (
            held.name != MANIFEST_NAME
            or _manifest_identity_from_bytes(held.contents) != self._identity
            or not _held_manifest_is_authoritative(
                self._album.directory_descriptor,
                MANIFEST_NAME,
                held,
            )
        ):
            _manifest_error("release manifest changed under retained authority")
        self._album.validate_namespace()

    def close(self) -> None:
        if not self._finalizer.alive:
            return
        self._lifetime.close()
        self._finalizer.detach()


def _rename_noreplace(
    source_directory: int,
    source: str,
    destination_directory: int,
    destination: str,
) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "exclusive manifest rename is unavailable")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(
            errno.ENOTSUP,
            "exclusive manifest rename is unavailable",
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        source_directory,
        os.fsencode(source),
        destination_directory,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _transaction_names(entries) -> tuple[str, ...]:
    return tuple(
        name for name in entries
        if isinstance(name, str) and name.startswith(_TRANSACTION_PREFIX)
    )


def _hold_manifest_descriptor(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    *,
    expected_contents: bytes | None = None,
    expected_version=None,
) -> _HeldManifest:
    exclusion = None
    try:
        before = os.fstat(descriptor)
        version = _file_version(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (expected_version is not None and version != expected_version)
        ):
            _manifest_error("release manifest transaction is not a regular file")
        exclusion = acquire_inode_write_exclusion(descriptor)
        if exclusion is None:
            _manifest_error("release manifest could not be protected from writers")
        raw = os.pread(descriptor, MAX_MANIFEST_BYTES + 1, 0)
        if len(raw) > MAX_MANIFEST_BYTES:
            _manifest_error("release manifest is too large")
        identity = _manifest_identity_from_bytes(raw)
        canonical = _manifest_bytes(identity)
        if raw != canonical or (
            expected_contents is not None and raw != expected_contents
        ):
            _manifest_error("release manifest transaction is not canonical")
        if (
            not exclusion.intact()
            or not _publication_entry_matches(
                directory_descriptor,
                name,
                descriptor,
                version,
                raw,
            )
        ):
            _manifest_error("release manifest transaction changed while held")
        held = _HeldManifest(name, descriptor, version, raw, exclusion)
        exclusion = None
        return held
    except ReleaseManifestError:
        raise
    except OSError as exc:
        _manifest_error("cannot hold release manifest transaction", exc)
    finally:
        if exclusion is not None:
            exclusion.close()


def _open_held_manifest(
    directory_descriptor: int,
    name: str,
    *,
    expected_contents: bytes | None = None,
    expected_version=None,
) -> _HeldManifest:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_descriptor,
        )
        held = _hold_manifest_descriptor(
            directory_descriptor,
            name,
            descriptor,
            expected_contents=expected_contents,
            expected_version=expected_version,
        )
        descriptor = -1
        return held
    except FileNotFoundError as exc:
        _manifest_error("release manifest transaction changed while opened", exc)
    except ReleaseManifestError:
        raise
    except OSError as exc:
        _manifest_error("cannot hold release manifest transaction", exc)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _preserve_created_transaction(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    contents: bytes,
) -> None:
    if (
        descriptor >= 0
        and _descriptor_has_exact_contents(descriptor, contents)
        and _stable_named_regular_file_version(
            directory_descriptor,
            name,
            descriptor,
        ) is not None
    ):
        os.fsync(directory_descriptor)
        return
    recovery = _create_manifest_transaction(
        directory_descriptor,
        contents,
        preserve_on_failure=False,
        directory_fsync=os.fsync,
    )
    try:
        if not _held_manifest_is_exact(
            directory_descriptor,
            recovery.name,
            recovery,
        ):
            _manifest_error("release manifest recovery artifact changed")
        os.fsync(directory_descriptor)
    finally:
        recovery.close()


def _create_manifest_transaction(
    directory_descriptor: int,
    contents: bytes,
    *,
    preserve_on_failure: bool = True,
    directory_fsync=None,
) -> _HeldManifest:
    for _attempt in range(16):
        name = f"{_TRANSACTION_PREFIX}{secrets.token_hex(16)}"
        writer = None

        def open_relative(path, flags):
            return os.open(
                path,
                flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_descriptor,
            )

        try:
            writer = io.FileIO(
                name,
                "x+b",
                opener=open_relative,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            _manifest_error("cannot create release manifest transaction", exc)
        exclusion = None
        durable = False
        try:
            exclusion = acquire_inode_write_lease(writer)
            if exclusion is None:
                _manifest_error(
                    "release manifest transaction could not be protected from writers"
                )
            writer_descriptor = writer.fileno()
            _write_all(writer_descriptor, contents)
            os.fsync(writer_descriptor)
            version = _file_version(os.fstat(writer_descriptor))
            durable = True
            if (
                not exclusion.intact()
                or not _publication_entry_matches(
                    directory_descriptor,
                    name,
                    writer_descriptor,
                    version,
                    contents,
                )
            ):
                _manifest_error("release manifest transaction changed while written")
            if directory_fsync is None:
                _fsync_directory(directory_descriptor)
            else:
                directory_fsync(directory_descriptor)
            if (
                not exclusion.intact()
                or not _publication_entry_matches(
                    directory_descriptor,
                    name,
                    writer_descriptor,
                    version,
                    contents,
                )
            ):
                _manifest_error("release manifest transaction changed before rename")
            held = _HeldManifest(
                name,
                writer_descriptor,
                version,
                contents,
                exclusion,
            )
            writer = None
            exclusion = None
            return held
        except BaseException:
            if durable and preserve_on_failure:
                _preserve_created_transaction(
                    directory_descriptor,
                    name,
                    writer.fileno(),
                    contents,
                )
            raise
        finally:
            cleanup_error = None
            if exclusion is not None:
                for _cleanup_attempt in range(2):
                    try:
                        exclusion.close()
                        cleanup_error = None
                        break
                    except BaseException as exc:
                        cleanup_error = exc
            if cleanup_error is None and writer is not None:
                try:
                    writer.close()
                except BaseException as exc:
                    cleanup_error = exc
            if cleanup_error is not None and sys.exc_info()[0] is None:
                raise cleanup_error
    _manifest_error("cannot allocate release manifest transaction")


def _held_manifest_is_exact(
    directory_descriptor: int,
    name: str,
    held: _HeldManifest,
) -> bool:
    return (
        _descriptor_has_exact_contents(
            held.descriptor,
            held.contents,
            expected_version=held.version,
        )
        and _stable_named_regular_file_version(
            directory_descriptor,
            name,
            held.descriptor,
            expected_version=held.version,
        ) is not None
    )


def _held_manifest_is_authoritative(
    directory_descriptor: int,
    name: str,
    held: _HeldManifest,
) -> bool:
    return held.exclusion.intact() and _held_manifest_is_exact(
        directory_descriptor,
        name,
        held,
    )


def _refresh_renamed_manifest(
    directory_descriptor: int,
    held: _HeldManifest,
) -> bool:
    try:
        current = os.fstat(held.descriptor)
        version = _file_version(current)
    except OSError:
        return False
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not _descriptor_has_exact_contents(
            held.descriptor,
            held.contents,
            expected_version=version,
        )
        or _stable_named_regular_file_version(
            directory_descriptor,
            MANIFEST_NAME,
            held.descriptor,
            expected_version=version,
        ) is None
    ):
        return False
    held.name = MANIFEST_NAME
    held.version = version
    return held.exclusion.intact()


def _exact_named_canonical_manifest(
    directory_descriptor: int,
    name: str,
    contents: bytes,
) -> bool:
    held = None
    try:
        held = _open_held_manifest(
            directory_descriptor,
            name,
            expected_contents=contents,
        )
        return True
    except (ReleaseManifestError, OSError):
        return False
    finally:
        if held is not None:
            held.close()


def _preserve_publication_evidence(
    directory_descriptor: int,
    held: _HeldManifest,
) -> None:
    if held.name != MANIFEST_NAME:
        _refresh_renamed_manifest(directory_descriptor, held)
    if _held_manifest_is_exact(directory_descriptor, MANIFEST_NAME, held):
        os.fsync(directory_descriptor)
        return
    if (
        held.name != MANIFEST_NAME
        and _held_manifest_is_exact(directory_descriptor, held.name, held)
    ):
        os.fsync(directory_descriptor)
        return
    try:
        entries = tuple(os.listdir(directory_descriptor))
    except OSError as exc:
        _manifest_error("cannot examine release manifest recovery evidence", exc)
    for name in _transaction_names(entries):
        if _exact_named_canonical_manifest(
            directory_descriptor,
            name,
            held.contents,
        ):
            os.fsync(directory_descriptor)
            return
    if not _descriptor_has_exact_contents(
        held.descriptor,
        held.contents,
    ):
        _manifest_error("release manifest publication lost canonical evidence")
    recovery = _create_manifest_transaction(
        directory_descriptor,
        held.contents,
        directory_fsync=os.fsync,
    )
    try:
        if not _held_manifest_is_exact(
            directory_descriptor,
            recovery.name,
            recovery,
        ):
            _manifest_error("release manifest recovery artifact changed")
        os.fsync(directory_descriptor)
    finally:
        recovery.close()


def _expected_renamed_entries(snapshot, source: str) -> tuple[str, ...]:
    entries = list(snapshot.entries)
    if source in entries:
        entries.remove(source)
    if MANIFEST_NAME in entries:
        _manifest_error("release manifest appeared before exclusive rename")
    entries.append(MANIFEST_NAME)
    return tuple(sorted(entries, key=os.fsencode))


def _install_manifest_transaction(album, snapshot, held: _HeldManifest) -> None:
    directory_descriptor = album.directory_descriptor
    try:
        if not held.exclusion.intact() or not _held_manifest_is_exact(
            directory_descriptor,
            held.name,
            held,
        ):
            _manifest_error("release manifest transaction changed before rename")
        expected_entries = _expected_renamed_entries(snapshot, held.name)
        try:
            _rename_noreplace(
                directory_descriptor,
                held.name,
                directory_descriptor,
                MANIFEST_NAME,
            )
        except FileExistsError as exc:
            _preserve_publication_evidence(directory_descriptor, held)
            _manifest_error("release manifest appeared during exclusive rename", exc)
        except BaseException:
            _preserve_publication_evidence(directory_descriptor, held)
            raise
        if not _refresh_renamed_manifest(directory_descriptor, held):
            _preserve_publication_evidence(directory_descriptor, held)
            _manifest_error("release manifest transaction changed during rename")
        try:
            _fsync_directory(directory_descriptor)
        except BaseException:
            _preserve_publication_evidence(directory_descriptor, held)
            raise
        if not _held_manifest_is_exact(
            directory_descriptor,
            MANIFEST_NAME,
            held,
        ) or not held.exclusion.intact():
            _preserve_publication_evidence(directory_descriptor, held)
            _manifest_error("release manifest changed before publication commit")
        album.commit_directory_mutation(snapshot, expected_entries)
    except ReleaseManifestError:
        raise
    except OSError as exc:
        try:
            _preserve_publication_evidence(directory_descriptor, held)
        except ReleaseManifestError:
            raise
        _manifest_error("cannot publish release manifest transaction", exc)


def _publish_release_identity_authorized_retained(
    album,
    identity: ReleaseIdentity,
) -> tuple[bool, RetainedReleaseManifestAuthority]:
    from qobuz_librarian.library.release_authority import AlbumAuthority

    if type(album) is not AlbumAuthority:
        _manifest_error("release manifest publication requires live album authority")
    identity = _validated_identity(identity)
    album.validate_namespace()
    snapshot = album.snapshot_directory()
    if _transaction_names(snapshot.entries):
        _manifest_error("release manifest transaction requires reconciliation")
    directory_descriptor = album.directory_descriptor
    if MANIFEST_NAME in snapshot.entries:
        held = _open_held_manifest(directory_descriptor, MANIFEST_NAME)
        try:
            existing = _manifest_identity_from_bytes(held.contents)
            album.validate_namespace()
            if not _held_manifest_is_authoritative(
                directory_descriptor,
                MANIFEST_NAME,
                held,
            ):
                _manifest_error("release manifest changed under authority")
            if existing == identity:
                _fsync_directory(directory_descriptor)
                album.validate_namespace()
                if not _held_manifest_is_authoritative(
                    directory_descriptor,
                    MANIFEST_NAME,
                    held,
                ):
                    _manifest_error(
                        "release manifest changed during idempotent publication"
                    )
                retained = RetainedReleaseManifestAuthority(
                    album,
                    identity,
                    held,
                )
                retained.validate_namespace()
                held = None
                return False, retained
            _manifest_error("release manifest identifies a different release")
        finally:
            if held is not None:
                held.close()
    contents = _manifest_bytes(identity)
    held = _create_manifest_transaction(directory_descriptor, contents)
    try:
        _install_manifest_transaction(album, snapshot, held)
        retained = RetainedReleaseManifestAuthority(album, identity, held)
        retained.validate_namespace()
        held = None
        return True, retained
    finally:
        if held is not None:
            held.close()


def _publish_release_identity_authorized(album, identity: ReleaseIdentity) -> bool:
    changed, retained = _publish_release_identity_authorized_retained(
        album,
        identity,
    )
    retained.close()
    return changed


def publish_release_identity_authorized(album, identity: ReleaseIdentity) -> None:
    """Publish canonical identity bytes while *album* authority remains live."""
    _publish_release_identity_authorized(album, identity)


def publish_release_identity_authorized_retained(
    album,
    identity: ReleaseIdentity,
) -> RetainedReleaseManifestAuthority:
    """Publish and transfer the exact live manifest authority to the caller."""
    _changed, retained = _publish_release_identity_authorized_retained(
        album,
        identity,
    )
    return retained


def retain_release_identity_authorized(
    album,
    identity: ReleaseIdentity,
) -> RetainedReleaseManifestAuthority:
    """Retain an existing exact manifest without reopening the album path."""
    from qobuz_librarian.library.release_authority import AlbumAuthority

    if type(album) is not AlbumAuthority:
        _manifest_error("release manifest retention requires live album authority")
    identity = _validated_identity(identity)
    album.validate_namespace()
    held = _open_held_manifest(album.directory_descriptor, MANIFEST_NAME)
    try:
        if _manifest_identity_from_bytes(held.contents) != identity:
            _manifest_error("release manifest identifies a different release")
        retained = RetainedReleaseManifestAuthority(album, identity, held)
        retained.validate_namespace()
        held = None
        return retained
    finally:
        if held is not None:
            held.close()


def reconcile_release_manifest_transaction(album) -> ReleaseIdentity | None:
    """Complete one exact reserved manifest transaction under live authority."""
    from qobuz_librarian.library.release_authority import AlbumAuthority

    if type(album) is not AlbumAuthority:
        _manifest_error("release manifest reconciliation requires live album authority")
    album.validate_namespace()
    snapshot = album.snapshot_directory()
    transactions = _transaction_names(snapshot.entries)
    directory_descriptor = album.directory_descriptor
    if not transactions:
        if MANIFEST_NAME not in snapshot.entries:
            return None
        final = _open_held_manifest(directory_descriptor, MANIFEST_NAME)
        try:
            identity = _manifest_identity_from_bytes(final.contents)
            album.validate_namespace()
            if not _held_manifest_is_authoritative(
                directory_descriptor,
                MANIFEST_NAME,
                final,
            ):
                _manifest_error("release manifest changed under authority")
            _fsync_directory(directory_descriptor)
            album.validate_namespace()
            if not _held_manifest_is_authoritative(
                directory_descriptor,
                MANIFEST_NAME,
                final,
            ):
                _manifest_error("release manifest changed during reconciliation")
            return identity
        finally:
            final.close()
    if len(transactions) != 1:
        _manifest_error("multiple release manifest transactions require attention")
    held = _open_held_manifest(directory_descriptor, transactions[0])
    try:
        identity = _manifest_identity_from_bytes(held.contents)
        os.fsync(held.descriptor)
        album.validate_namespace()
        if not _held_manifest_is_authoritative(
            directory_descriptor,
            transactions[0],
            held,
        ):
            _manifest_error(
                "release manifest transaction changed during file fsync"
            )
        if MANIFEST_NAME in snapshot.entries:
            final = _open_held_manifest(directory_descriptor, MANIFEST_NAME)
            try:
                existing = _manifest_identity_from_bytes(final.contents)
                album.validate_namespace()
                if (
                    not _held_manifest_is_authoritative(
                        directory_descriptor,
                        transactions[0],
                        held,
                    )
                    or not _held_manifest_is_authoritative(
                        directory_descriptor,
                        MANIFEST_NAME,
                        final,
                    )
                ):
                    _manifest_error("release manifest transaction changed under authority")
                if existing != identity:
                    _manifest_error(
                        "release manifest transaction conflicts with final manifest"
                    )
                _fsync_directory(directory_descriptor)
                album.validate_namespace()
                if (
                    not _held_manifest_is_authoritative(
                        directory_descriptor,
                        transactions[0],
                        held,
                    )
                    or not _held_manifest_is_authoritative(
                        directory_descriptor,
                        MANIFEST_NAME,
                        final,
                    )
                ):
                    _manifest_error(
                        "release manifest transaction changed during reconciliation"
                    )
                return existing
            finally:
                final.close()
        _install_manifest_transaction(album, snapshot, held)
        return identity
    finally:
        held.close()


def publish_release_identity(
    album_dir: Path,
    identity: ReleaseIdentity,
    *,
    expected_directory: tuple[int, int] | None = None,
    expected_path_receipt: DirectoryPathReceipt | None = None,
) -> bool:
    """Compatibility wrapper that acquires the same live album authority."""
    from qobuz_librarian import run_lock
    from qobuz_librarian.library.release_authority import (
        AlbumAuthorityUnavailable,
        open_album_authority,
    )

    lease = run_lock.current_lease()
    owned_lease = False
    try:
        if lease is None:
            lease = run_lock.acquire()
            owned_lease = True
        if lease is None:
            _manifest_error("release manifest publication requires the live run lock")
        with open_album_authority(
            Path(album_dir),
            lease,
            expected_path=expected_path_receipt,
        ) as album:
            if (
                expected_directory is not None
                and album.path_receipt.directory_identity != expected_directory
            ):
                _manifest_error(
                    "album directory changed before manifest publication"
                )
            return _publish_release_identity_authorized(album, identity)
    except run_lock.LockBusy as exc:
        _manifest_error("release manifest publication requires the live run lock", exc)
    except AlbumAuthorityUnavailable as exc:
        _manifest_error("release manifest publication authority is unavailable", exc)
    finally:
        if owned_lease and lease is not None:
            lease.close()
