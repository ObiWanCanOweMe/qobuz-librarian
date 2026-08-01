"""Resolve stable Qobuz release identities onto album-directory paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian.library.migrate import _truncate_component
from qobuz_librarian.library.release_identity import (
    ReleaseIdentity,
    ReleaseManifestError,
    read_release_identity,
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


def _path_is_occupied(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _manifest_identity(path: Path, *, role: str) -> ReleaseIdentity | None:
    try:
        return read_release_identity(path)
    except (ReleaseManifestError, OSError) as exc:
        raise AlbumPlacementAttention(
            f"{role} path is occupied but cannot provide a valid release manifest"
        ) from exc


def _placement(
    identity: ReleaseIdentity,
    friendly_path: Path,
    destination: Path,
    disposition: PlacementDisposition,
) -> AlbumPlacement:
    suffix = "" if destination == friendly_path else qobuz_collision_suffix(identity)
    return AlbumPlacement(identity, friendly_path, destination, disposition, suffix)


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
    if not _path_is_occupied(friendly_path):
        return _placement(
            identity,
            friendly_path,
            friendly_path,
            PlacementDisposition.NEW,
        )

    friendly_identity = _manifest_identity(friendly_path, role="friendly")
    if friendly_identity is None:
        if adopted_identity == identity:
            return _placement(
                identity,
                friendly_path,
                friendly_path,
                PlacementDisposition.ADOPTED,
            )
        raise AlbumPlacementAttention("friendly path is occupied and unmarked")
    if friendly_identity == identity:
        return _placement(
            identity,
            friendly_path,
            friendly_path,
            PlacementDisposition.SAME_RELEASE,
        )

    destination = collision_album_path(friendly_path, identity)
    if not _path_is_occupied(destination):
        return _placement(
            identity,
            friendly_path,
            destination,
            PlacementDisposition.COLLISION,
        )

    destination_identity = _manifest_identity(destination, role="collision")
    if destination_identity == identity:
        return _placement(
            identity,
            friendly_path,
            destination,
            PlacementDisposition.COLLISION,
        )
    raise AlbumPlacementAttention("collision path is occupied by another release")
