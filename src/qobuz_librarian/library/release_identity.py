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


def _open_album_directory(album_dir: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(os.fspath(album_dir), flags)
    except OSError as exc:
        raise ReleaseManifestError(f"cannot open album directory: {album_dir}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ReleaseManifestError("album path is not a directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


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
        return None
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
            or _identity(before) != _identity(after)
            or after.st_size != before.st_size
            or len(contents) != before.st_size
        ):
            _manifest_error("release manifest changed while it was read")
        if len(contents) > MAX_MANIFEST_BYTES:
            _manifest_error("release manifest is too large")
        return _manifest_identity_from_bytes(contents)
    finally:
        os.close(descriptor)


def read_release_identity(album_dir: Path) -> ReleaseIdentity | None:
    """Return a validated identity, ``None`` for no manifest, or raise on invalid data."""
    directory_descriptor = _open_album_directory(album_dir)
    try:
        return _read_release_identity_at(directory_descriptor)
    finally:
        os.close(directory_descriptor)


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
) -> bool:
    """Atomically publish *identity*, never replacing a pre-existing manifest."""
    identity = _validated_identity(identity)
    contents = _manifest_bytes(identity)
    directory_descriptor = _open_album_directory(album_dir)
    temporary_name = None
    temporary_descriptor = None
    try:
        _validate_expected_directory(directory_descriptor, expected_directory)
        existing = _read_release_identity_at(directory_descriptor)
        if existing is not None:
            if existing == identity:
                return False
            _manifest_error("release manifest identifies a different release")

        temporary_name, temporary_descriptor = _temporary_manifest(directory_descriptor, contents)
        if not _named_entry_matches(
            directory_descriptor, temporary_name, temporary_descriptor
        ):
            _manifest_error("temporary release manifest changed before publication")
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
        _remove_temporary_manifest(directory_descriptor, temporary_name)
        temporary_name = None
        os.fsync(directory_descriptor)
        return True
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            _remove_temporary_manifest(directory_descriptor, temporary_name)
        os.close(directory_descriptor)
