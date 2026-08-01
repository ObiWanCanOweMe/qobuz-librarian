"""Resolve stable Qobuz release identities onto album-directory paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian.library.migrate import _truncate_component
from qobuz_librarian.library.release_identity import (
    DirectoryPathReceipt,
    ReleaseIdentity,
    ReleaseManifestError,
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
    )


def resolve_album_placement(
    friendly_path: Path,
    identity: ReleaseIdentity,
    *,
    adopted_identity: ReleaseIdentity | None = None,
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
        if adopted_identity == identity:
            return _placement(
                identity,
                friendly_path,
                friendly_path,
                PlacementDisposition.ADOPTED,
                friendly_receipt,
                friendly_receipt,
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
    try:
        current = resolve_album_placement(
            placement.friendly_path,
            placement.identity,
            adopted_identity=adopted,
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
