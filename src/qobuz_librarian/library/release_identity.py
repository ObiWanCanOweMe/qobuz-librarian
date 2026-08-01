"""Durable, descriptor-safe identity manifests for managed Qobuz releases."""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = ".qobuz-librarian-release.json"
MAX_MANIFEST_BYTES = 4096
_FIELDS = frozenset({"schema_version", "provider", "release_id"})
_TEMP_PREFIX = ".qobuz-librarian-release-"


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
    return isinstance(name, str) and name == MANIFEST_NAME


def is_ignored_library_artifact(name):
    return isinstance(name, str) and (name.startswith("._") or name == MANIFEST_NAME)


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


def _validate_expected_directory(descriptor: int, expected_directory: tuple[int, int] | None):
    if expected_directory is not None and _identity(os.fstat(descriptor)) != expected_directory:
        raise ReleaseManifestError("album directory changed before manifest publication")


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
        final = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final.st_mode)
            or _file_version(after) != _file_version(final)
            or not _named_entry_matches(
                directory_descriptor,
                MANIFEST_NAME,
                descriptor,
            )
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


def _temporary_manifest(directory_descriptor: int, contents: bytes) -> tuple[str, int]:
    for _ in range(10):
        name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            _manifest_error("cannot create temporary release manifest", exc)
        try:
            _write_all(descriptor, contents)
            os.fsync(descriptor)
            return name, descriptor
        except BaseException:
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=directory_descriptor)
            except OSError:
                pass
            raise
    _manifest_error("cannot allocate temporary release manifest")


def _named_entry_matches(directory_descriptor: int, name: str, descriptor: int) -> bool:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and _identity(held) == _identity(named)
    )


def _remove_temporary_manifest(directory_descriptor: int, name: str):
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return
    except OSError as exc:
        _manifest_error("cannot remove temporary release manifest", exc)


def publish_release_identity(
    album_dir: Path,
    identity: ReleaseIdentity,
    *,
    expected_directory: tuple[int, int] | None = None,
    expected_path_receipt: DirectoryPathReceipt | None = None,
) -> bool:
    """Atomically publish *identity*, never replacing a pre-existing manifest."""
    identity = _validated_identity(identity)
    contents = _manifest_bytes(identity)
    path_receipt, descriptors = _open_album_directory(
        album_dir,
        expected_path_receipt=expected_path_receipt,
    )
    directory_descriptor = descriptors[-1]
    temporary_name = None
    temporary_descriptor = None
    try:
        _validate_expected_directory(directory_descriptor, expected_directory)
        existing = _read_release_identity_at(directory_descriptor)
        if not _directory_chain_matches(path_receipt, descriptors):
            _manifest_error("album directory changed before manifest publication")
        if existing is not None:
            if existing == identity:
                return False
            _manifest_error("release manifest identifies a different release")

        temporary_name, temporary_descriptor = _temporary_manifest(directory_descriptor, contents)
        if not _named_entry_matches(
            directory_descriptor, temporary_name, temporary_descriptor
        ):
            _manifest_error("temporary release manifest changed before publication")
        if not _directory_chain_matches(path_receipt, descriptors):
            _manifest_error("album directory changed before manifest publication")
        try:
            os.link(
                temporary_name,
                MANIFEST_NAME,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_release_identity_at(directory_descriptor)
            if not _directory_chain_matches(path_receipt, descriptors):
                _manifest_error(
                    "album directory changed during manifest publication"
                )
            if existing == identity:
                return False
            if existing is not None:
                _manifest_error("release manifest identifies a different release")
            _manifest_error("release manifest appeared during publication")
        except OSError as exc:
            _manifest_error("cannot publish release manifest", exc)

        if not _named_entry_matches(
            directory_descriptor, MANIFEST_NAME, temporary_descriptor
        ):
            _manifest_error("release manifest changed during publication")
        if not _directory_chain_matches(path_receipt, descriptors):
            # The newly linked manifest is retained on the opened inode as
            # durable recovery evidence.  Commit that link before reporting
            # the public-path failure, then durably remove only our held temp.
            try:
                os.fsync(directory_descriptor)
            except OSError as exc:
                _manifest_error(
                    "cannot preserve release manifest recovery evidence",
                    exc,
                )
            _remove_temporary_manifest(directory_descriptor, temporary_name)
            temporary_name = None
            try:
                os.fsync(directory_descriptor)
            except OSError as exc:
                _manifest_error(
                    "cannot finish release manifest recovery evidence",
                    exc,
                )
            _manifest_error("album directory changed during manifest publication")
        _remove_temporary_manifest(directory_descriptor, temporary_name)
        temporary_name = None
        os.fsync(directory_descriptor)
        if not _directory_chain_matches(path_receipt, descriptors):
            _manifest_error("album directory changed during manifest publication")
        return True
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            _remove_temporary_manifest(directory_descriptor, temporary_name)
        _close_descriptors(descriptors)
