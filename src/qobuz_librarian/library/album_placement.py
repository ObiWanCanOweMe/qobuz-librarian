"""Resolve stable Qobuz release identities onto album-directory paths."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.migrate import _truncate_component
from qobuz_librarian.library.release_identity import (
    DirectoryPathReceipt,
    ReleaseIdentity,
    ReleaseManifestError,
    _close_descriptors,
    _directory_chain_matches,
    _open_album_directory,
    capture_directory_path_receipt,
    read_release_identity_with_receipt,
)


class PlacementDisposition(str, Enum):
    NEW = "new"
    SAME_RELEASE = "same_release"
    ADOPTED = "adopted"
    COLLISION = "collision"


@dataclass(frozen=True, slots=True)
class AlbumPlacement:
    identity: ReleaseIdentity
    friendly_path: Path
    destination: Path
    disposition: PlacementDisposition
    suffix: str
    friendly_receipt: DirectoryPathReceipt | None = None
    destination_receipt: DirectoryPathReceipt | None = None
    adoption_receipt: LegacyAdoptionReceipt | None = None


@dataclass(frozen=True, slots=True)
class LegacyAudioReceipt:
    relative: str
    version: tuple[int, int, int, int, int]
    sha256: str


@dataclass(frozen=True, slots=True)
class LegacyAdoptionReceipt:
    """Exact directory and audio proof reviewed by the legacy selector."""

    identity: ReleaseIdentity
    path_receipt: DirectoryPathReceipt
    audio: tuple[LegacyAudioReceipt, ...]


class AlbumPlacementAttention(OSError):
    """The selected path cannot be safely associated with a release identity."""


def qobuz_collision_suffix(identity: ReleaseIdentity) -> str:
    if identity.provider != "qobuz":
        raise ValueError("collision suffix requires a Qobuz identity")
    return f" [qobuz-{identity.release_id}]"


def _component_name_max(path: Path) -> int:
    """Return the byte limit for a component beneath *path*'s parent."""
    try:
        limit = os.pathconf(path.parent, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        return 255
    return limit if limit > 0 else 255


def collision_album_path(friendly_path: Path, identity: ReleaseIdentity) -> Path:
    """Return the deterministic collision sibling for *identity*."""
    suffix = qobuz_collision_suffix(identity)
    suffix_bytes = os.fsencode(suffix)
    limit = _component_name_max(friendly_path)
    if len(suffix_bytes) > limit:
        raise AlbumPlacementAttention(
            "collision suffix exceeds the filesystem component length limit"
        )
    stem_limit = limit - len(suffix_bytes)
    stem = "" if stem_limit == 0 else _truncate_component(
        friendly_path.name,
        limit=stem_limit,
    )
    return friendly_path.with_name(stem + suffix)


def _path_receipt(path: Path, *, role: str) -> DirectoryPathReceipt:
    try:
        return capture_directory_path_receipt(path)
    except (ReleaseManifestError, OSError) as exc:
        raise AlbumPlacementAttention(
            f"{role} path occupancy could not be examined"
        ) from exc


def _manifest_identity(
    path: Path, *, role: str
) -> tuple[ReleaseIdentity | None, DirectoryPathReceipt]:
    try:
        return read_release_identity_with_receipt(path)
    except (ReleaseManifestError, OSError) as exc:
        raise AlbumPlacementAttention(
            f"{role} path is occupied but cannot provide a valid release manifest"
        ) from exc


def _placement(
    identity: ReleaseIdentity,
    friendly_path: Path,
    destination: Path,
    disposition: PlacementDisposition,
    friendly_receipt: DirectoryPathReceipt,
    destination_receipt: DirectoryPathReceipt,
    adoption_receipt: LegacyAdoptionReceipt | None = None,
) -> AlbumPlacement:
    suffix = "" if destination == friendly_path else qobuz_collision_suffix(identity)
    return AlbumPlacement(
        identity,
        friendly_path,
        destination,
        disposition,
        suffix,
        friendly_receipt,
        destination_receipt,
        adoption_receipt,
    )


def _legacy_file_version(value) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _legacy_directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
    )


def _legacy_regular_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _capture_legacy_audio_at(directory_descriptor, prefix, captured) -> bool:
    before = os.fstat(directory_descriptor)
    if not stat.S_ISDIR(before.st_mode):
        return False
    for name in sorted(os.listdir(directory_descriptor)):
        if name in {"", ".", ".."}:
            return False
        named = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        relative_parts = (*prefix, name)
        if stat.S_ISDIR(named.st_mode):
            child = None
            try:
                child = os.open(
                    name,
                    _legacy_directory_flags(),
                    dir_fd=directory_descriptor,
                )
                held_before = os.fstat(child)
                if (
                    _legacy_file_version(named)
                    != _legacy_file_version(held_before)
                    or not _capture_legacy_audio_at(
                        child, relative_parts, captured
                    )
                ):
                    return False
                named_after = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                held_final = os.fstat(child)
                if len({
                    _legacy_file_version(named),
                    _legacy_file_version(held_before),
                    _legacy_file_version(named_after),
                    _legacy_file_version(held_final),
                }) != 1:
                    return False
            finally:
                if child is not None:
                    os.close(child)
            continue
        if Path(name).suffix.lower() not in cfg.AUDIO_EXTS:
            continue
        if not stat.S_ISREG(named.st_mode):
            return False
        descriptor = None
        try:
            descriptor = os.open(
                name,
                _legacy_regular_flags(),
                dir_fd=directory_descriptor,
            )
            held_before = os.fstat(descriptor)
            named_before = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            held_after = os.fstat(descriptor)
            named_final = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            held_final = os.fstat(descriptor)
            versions = {
                _legacy_file_version(value)
                for value in (
                    named,
                    held_before,
                    named_before,
                    held_after,
                    named_final,
                    held_final,
                )
            }
            if len(versions) != 1:
                return False
            captured.append(LegacyAudioReceipt(
                "/".join(relative_parts),
                _legacy_file_version(held_final),
                digest.hexdigest(),
            ))
        finally:
            if descriptor is not None:
                os.close(descriptor)
    final = os.fstat(directory_descriptor)
    return (
        stat.S_ISDIR(final.st_mode)
        and _legacy_file_version(before) == _legacy_file_version(final)
    )


def capture_legacy_adoption_receipt(
    path: Path,
    identity: ReleaseIdentity,
) -> LegacyAdoptionReceipt:
    """Seal the exact unmarked directory and all audio reviewed for adoption."""
    descriptors = ()
    try:
        path_receipt, descriptors = _open_album_directory(Path(path))
        captured = []
        if (
            not _directory_chain_matches(path_receipt, descriptors)
            or not _capture_legacy_audio_at(descriptors[-1], (), captured)
            or not _directory_chain_matches(path_receipt, descriptors)
        ):
            raise AlbumPlacementAttention(
                "legacy adoption proof changed while it was captured"
            )
        return LegacyAdoptionReceipt(
            identity,
            path_receipt,
            tuple(sorted(captured, key=lambda value: value.relative)),
        )
    except (ReleaseManifestError, OSError) as exc:
        raise AlbumPlacementAttention(
            "legacy adoption proof could not be captured"
        ) from exc
    finally:
        _close_descriptors(descriptors)


def legacy_adoption_receipt_matches(
    receipt: LegacyAdoptionReceipt,
    path: Path,
    identity: ReleaseIdentity,
) -> bool:
    if (
        not isinstance(receipt, LegacyAdoptionReceipt)
        or receipt.identity != identity
        or receipt.path_receipt.path != os.path.abspath(os.fspath(path))
    ):
        return False
    try:
        return capture_legacy_adoption_receipt(path, identity) == receipt
    except AlbumPlacementAttention:
        return False


def resolve_album_placement(
    friendly_path: Path,
    identity: ReleaseIdentity,
    *,
    adopted_identity: ReleaseIdentity | None = None,
    adoption_receipt: LegacyAdoptionReceipt | None = None,
) -> AlbumPlacement:
    """Resolve *identity* to its friendly path or deterministic collision sibling.

    The manifest is authoritative.  An occupied friendly directory without one
    is reusable only with the exact adoption proof supplied by discovery.
    """
    friendly_path = Path(friendly_path)
    friendly_receipt = _path_receipt(friendly_path, role="friendly")
    if not friendly_receipt.exists:
        return _placement(
            identity,
            friendly_path,
            friendly_path,
            PlacementDisposition.NEW,
            friendly_receipt,
            friendly_receipt,
        )

    friendly_identity, friendly_receipt = _manifest_identity(
        friendly_path, role="friendly")
    if friendly_identity is None:
        if (
            adopted_identity == identity
            and adoption_receipt is not None
            and adoption_receipt.path_receipt == friendly_receipt
            and legacy_adoption_receipt_matches(
                adoption_receipt, friendly_path, identity
            )
        ):
            return _placement(
                identity,
                friendly_path,
                friendly_path,
                PlacementDisposition.ADOPTED,
                friendly_receipt,
                friendly_receipt,
                adoption_receipt,
            )
        if adopted_identity == identity:
            raise AlbumPlacementAttention(
                "legacy adoption proof changed before placement"
            )
        raise AlbumPlacementAttention("friendly path is occupied and unmarked")
    if friendly_identity == identity:
        return _placement(
            identity,
            friendly_path,
            friendly_path,
            PlacementDisposition.SAME_RELEASE,
            friendly_receipt,
            friendly_receipt,
        )

    destination = collision_album_path(friendly_path, identity)
    destination_receipt = _path_receipt(destination, role="collision")
    if not destination_receipt.exists:
        return _placement(
            identity,
            friendly_path,
            destination,
            PlacementDisposition.COLLISION,
            friendly_receipt,
            destination_receipt,
        )

    destination_identity, destination_receipt = _manifest_identity(
        destination, role="collision")
    if destination_identity == identity:
        return _placement(
            identity,
            friendly_path,
            destination,
            PlacementDisposition.COLLISION,
            friendly_receipt,
            destination_receipt,
        )
    raise AlbumPlacementAttention("collision path is occupied by another release")


def album_placement_is_current(placement: AlbumPlacement) -> bool:
    """Re-resolve and compare the exact path bindings carried by *placement*."""
    if (
        not isinstance(placement, AlbumPlacement)
        or placement.friendly_receipt is None
        or placement.destination_receipt is None
    ):
        return False
    adopted = (
        placement.identity
        if placement.disposition is PlacementDisposition.ADOPTED
        else None
    )
    adoption_receipt = (
        placement.adoption_receipt
        if placement.disposition is PlacementDisposition.ADOPTED
        else None
    )
    try:
        current = resolve_album_placement(
            placement.friendly_path,
            placement.identity,
            adopted_identity=adopted,
            adoption_receipt=adoption_receipt,
        )
    except (AlbumPlacementAttention, OSError, ValueError):
        return False
    return (
        current.identity == placement.identity
        and current.friendly_path == placement.friendly_path
        and current.destination == placement.destination
        and current.suffix == placement.suffix
        and current.friendly_receipt == placement.friendly_receipt
        and current.destination_receipt == placement.destination_receipt
    )


def require_album_placement_current(placement: AlbumPlacement) -> None:
    """Fail closed unless the exact reviewed placement still names its paths."""
    if not album_placement_is_current(placement):
        raise AlbumPlacementAttention(
            "release placement path binding changed before import"
        )


def album_placement_requires_publication(placement: AlbumPlacement) -> bool:
    """Whether safe completion must publish the destination's first manifest."""
    if (
        not isinstance(placement, AlbumPlacement)
        or placement.destination_receipt is None
    ):
        return False
    return (
        not placement.destination_receipt.exists
        or placement.disposition is PlacementDisposition.ADOPTED
    )
