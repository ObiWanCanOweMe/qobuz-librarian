"""Resolve stable Qobuz release identities onto album-directory paths."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.migrate import _truncate_component
from qobuz_librarian.library.release_authority import (
    AlbumAuthority,
    AlbumAuthorityUnavailable,
    HeldFileAuthority,
    open_album_authority,
)
from qobuz_librarian.library.release_identity import (
    DirectoryPathReceipt,
    ReleaseIdentity,
    ReleaseManifestError,
    _close_descriptors,
    _directory_chain_matches,
    _open_album_directory,
    capture_directory_path_receipt,
    is_ignored_library_artifact,
    is_release_manifest_name,
    read_release_identity_with_receipt,
)
from qobuz_librarian.run_lock import RunLockLease


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


@dataclass(frozen=True, slots=True)
class LegacyAdoptionProof:
    """Live descriptor-backed evidence for one legacy placement decision."""

    identity: ReleaseIdentity
    path_receipt: DirectoryPathReceipt
    audio_receipts: tuple[LegacyAudioReceipt, ...]
    authority_generation: object


@dataclass(frozen=True, slots=True)
class LegacyAdoptionCandidateState:
    """Descriptor-backed filesystem inputs for legacy candidate ranking."""

    path_receipt: DirectoryPathReceipt
    audio_count: int
    authority_generation: object

    def validate(self, path: Path) -> None:
        generation = self.authority_generation
        if type(generation) is not _LegacyAdoptionGeneration:
            raise AlbumPlacementAttention(
                "legacy adoption candidate state is detached"
            )
        generation.validate_candidate_state(self, path)


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


def _read_held_audio_meta(path: Path):
    """Read one held descriptor path without consulting the pathname cache."""
    from qobuz_librarian.library import scanner

    if not scanner.HAVE_MUTAGEN:
        return None
    try:
        audio = scanner.mutagen.File(os.fspath(path), easy=True)
    except OSError:
        raise
    except Exception:
        return None
    if audio is None:
        return None
    tags = audio.tags

    def first(key):
        value = tags.get(key) if tags else None
        return value[0] if value and isinstance(value, list) else ""

    title = first("title")
    if not title:
        return None
    info = audio.info
    return {
        "title": title,
        "isrc": first("isrc").strip().replace("-", "").upper(),
        "mb_trackid": first("musicbrainz_trackid").strip().lower(),
        "album": first("album"),
        "albumartist": first("albumartist") or first("artist"),
        "tracknumber": scanner.parse_track_num(first("tracknumber")),
        "discnumber": scanner.parse_track_num(first("discnumber")) or 1,
        "bits": getattr(info, "bits_per_sample", 0) if info else 0,
        "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
        "channels": getattr(info, "channels", 0) if info else 0,
        "length": getattr(info, "length", 0.0) if info else 0.0,
        "path": os.fspath(path),
    }


def _legacy_audio_relatives_at(directory_descriptor: int, prefix=()):
    before = os.fstat(directory_descriptor)
    if not stat.S_ISDIR(before.st_mode):
        raise AlbumAuthorityUnavailable("legacy adoption source is not a directory")
    relatives = []
    for name in sorted(os.listdir(directory_descriptor), key=os.fsencode):
        if name in {"", ".", ".."}:
            raise AlbumAuthorityUnavailable("legacy adoption source has an unsafe entry")
        named_before = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(named_before.st_mode):
            raise AlbumAuthorityUnavailable("legacy adoption does not accept links")
        if is_release_manifest_name(name):
            raise AlbumAuthorityUnavailable("legacy adoption source is already marked")
        parts = (*prefix, name)
        if stat.S_ISDIR(named_before.st_mode):
            child = os.open(
                name,
                _legacy_directory_flags(),
                dir_fd=directory_descriptor,
            )
            try:
                held_before = os.fstat(child)
                if _legacy_file_version(named_before) != _legacy_file_version(held_before):
                    raise AlbumAuthorityUnavailable(
                        "legacy adoption directory changed during enumeration"
                    )
                relatives.extend(_legacy_audio_relatives_at(child, parts))
                named_after = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                held_after = os.fstat(child)
                if len({
                    _legacy_file_version(named_before),
                    _legacy_file_version(held_before),
                    _legacy_file_version(named_after),
                    _legacy_file_version(held_after),
                }) != 1:
                    raise AlbumAuthorityUnavailable(
                        "legacy adoption directory changed during enumeration"
                    )
            finally:
                os.close(child)
            continue
        if is_ignored_library_artifact(name) or Path(name).suffix.lower() not in cfg.AUDIO_EXTS:
            continue
        if not stat.S_ISREG(named_before.st_mode):
            raise AlbumAuthorityUnavailable(
                "legacy adoption requires regular audio files"
            )
        relatives.append(Path(*parts))
    after = os.fstat(directory_descriptor)
    if _legacy_file_version(before) != _legacy_file_version(after):
        raise AlbumAuthorityUnavailable(
            "legacy adoption directory changed during enumeration"
        )
    return relatives


class _LegacyAdoptionGeneration:
    __slots__ = ("scan",)

    def __init__(self, scan):
        self.scan = scan

    def validate(self, proof, path, identity) -> None:
        scan = self.scan
        if (
            type(scan) is not LegacyAdoptionScan
            or not scan._entered
            or scan._closed
            or scan._generation is not self
            or proof.path_receipt != scan.path_receipt
            or proof.audio_receipts != scan.audio_receipts
            or proof.identity != identity
            or proof.path_receipt.path != os.path.abspath(os.fspath(path))
        ):
            raise AlbumPlacementAttention(
                "legacy adoption proof is detached or its authority is closed"
            )
        scan.validate_namespace()

    def validate_candidate_state(self, state, path) -> None:
        scan = self.scan
        if (
            type(scan) is not LegacyAdoptionScan
            or not scan._entered
            or scan._closed
            or scan._generation is not self
            or state.path_receipt != scan.path_receipt
            or state.audio_count != len(scan._held_audio)
            or state.path_receipt.path != os.path.abspath(os.fspath(path))
        ):
            raise AlbumPlacementAttention(
                "legacy adoption candidate state is detached"
            )
        scan.validate_namespace()


class LegacyAdoptionScan:
    """One live held album source for legacy tags, receipts, and placement."""

    def __init__(self, path: Path, authority: RunLockLease):
        self.path = Path(path)
        self._run_lock = authority
        self._authority_context = None
        self._album: AlbumAuthority | None = None
        self._held_audio: tuple[HeldFileAuthority, ...] = ()
        self._generation: _LegacyAdoptionGeneration | None = None
        self._entered = False
        self._closed = False

    @property
    def album_authority(self) -> AlbumAuthority:
        if not self._entered or self._closed or self._album is None:
            raise AlbumPlacementAttention("legacy adoption scan authority is not live")
        return self._album

    @property
    def path_receipt(self) -> DirectoryPathReceipt:
        return self.album_authority.path_receipt

    @property
    def audio_receipts(self) -> tuple[LegacyAudioReceipt, ...]:
        return tuple(
            LegacyAudioReceipt(
                held.relative.as_posix(),
                (
                    held.version.device,
                    held.version.inode,
                    held.version.size,
                    held.version.mtime_ns,
                    held.version.ctime_ns,
                ),
                held.digest,
            )
            for held in self._held_audio
        )

    def __enter__(self):
        if self._entered or self._closed:
            raise AlbumPlacementAttention("legacy adoption scan is one-shot")
        self._entered = True
        try:
            context = open_album_authority(self.path, self._run_lock)
            self._authority_context = context
            self._album = context.__enter__()
            relatives = _legacy_audio_relatives_at(
                self._album.directory_descriptor
            )
            relatives.sort(key=lambda value: os.fsencode(value.as_posix()))
            self._held_audio = tuple(
                self._album.open_file(relative) for relative in relatives
            )
            self._album.validate_namespace()
            self._generation = _LegacyAdoptionGeneration(self)
            return self
        except BaseException as exc:
            context = self._authority_context
            if context is not None:
                context.__exit__(*sys.exc_info())
            self._closed = True
            if isinstance(exc, AlbumPlacementAttention):
                raise
            if isinstance(exc, (AlbumAuthorityUnavailable, OSError)):
                raise AlbumPlacementAttention(
                    "legacy adoption authority is unavailable"
                ) from exc
            raise

    def __exit__(self, exc_type, exc, traceback):
        generation = self._generation
        if generation is not None:
            generation.scan = None
        self._closed = True
        context = self._authority_context
        if context is None:
            return None
        try:
            return context.__exit__(exc_type, exc, traceback)
        except (AlbumAuthorityUnavailable, OSError) as caught:
            if exc_type is None:
                raise AlbumPlacementAttention(
                    "legacy adoption authority changed before close"
                ) from caught
            return None

    def validate_namespace(self) -> None:
        try:
            self.album_authority.validate_namespace()
        except AlbumAuthorityUnavailable as exc:
            raise AlbumPlacementAttention(
                "legacy adoption authority changed"
            ) from exc

    def proof(self, identity: ReleaseIdentity) -> LegacyAdoptionProof:
        if type(identity) is not ReleaseIdentity or self._generation is None:
            raise AlbumPlacementAttention("legacy adoption identity is invalid")
        self.validate_namespace()
        return LegacyAdoptionProof(
            identity,
            self.path_receipt,
            self.audio_receipts,
            self._generation,
        )

    def candidate_state(self) -> LegacyAdoptionCandidateState:
        if self._generation is None:
            raise AlbumPlacementAttention(
                "legacy adoption candidate state is unavailable"
            )
        self.validate_namespace()
        return LegacyAdoptionCandidateState(
            self.path_receipt,
            len(self._held_audio),
            self._generation,
        )

    def read_tracks(self) -> list[dict]:
        from qobuz_librarian.library.tags import normalize

        self.validate_namespace()
        tracks = []
        for held in self._held_audio:
            proc_path = Path(f"/proc/self/fd/{held.descriptor}")
            try:
                tags = _read_held_audio_meta(proc_path)
            except OSError as exc:
                raise AlbumPlacementAttention(
                    "legacy adoption audio tags could not be read"
                ) from exc
            if tags is None:
                stem = held.relative.stem
                import re

                match = re.match(r"^(\d+)[\s.\-]+(.+)$", stem)
                disc_match = re.match(
                    r"(?:disc|cd)\s*0*(\d+)",
                    held.relative.parent.name,
                    re.IGNORECASE,
                )
                tags = {
                    "title": match.group(2) if match else stem,
                    "tracknumber": int(match.group(1)) if match else 0,
                    "isrc": "",
                    "mb_trackid": "",
                    "album": "",
                    "albumartist": "",
                    "discnumber": int(disc_match.group(1)) if disc_match else 1,
                    "bits": 0,
                    "sample_rate": 0,
                    "channels": 0,
                    "length": 0.0,
                    "path": os.fspath(proc_path),
                }
            else:
                tags = dict(tags)
            tags["normalized"] = normalize(tags["title"])
            tags["size"] = held.version.size
            tracks.append(tags)
        self.validate_namespace()
        return tracks


def _require_live_adoption_proof(
    proof: LegacyAdoptionProof,
    path: Path,
    identity: ReleaseIdentity,
) -> LegacyAdoptionReceipt:
    if (
        type(proof) is not LegacyAdoptionProof
        or type(proof.authority_generation) is not _LegacyAdoptionGeneration
    ):
        raise AlbumPlacementAttention(
            "legacy adoption proof is detached or its authority is closed"
        )
    proof.authority_generation.validate(proof, path, identity)
    return LegacyAdoptionReceipt(
        proof.identity,
        proof.path_receipt,
        proof.audio_receipts,
    )


def resolve_album_placement(
    friendly_path: Path,
    identity: ReleaseIdentity,
    *,
    adopted_identity: ReleaseIdentity | None = None,
    adoption_receipt: LegacyAdoptionReceipt | None = None,
    adoption_proof: LegacyAdoptionProof | None = None,
) -> AlbumPlacement:
    """Resolve *identity* to its friendly path or deterministic collision sibling.

    The manifest is authoritative.  An occupied friendly directory without one
    is reusable only with the exact adoption proof supplied by discovery.
    """
    friendly_path = Path(friendly_path)
    if adoption_proof is not None:
        live_receipt = _require_live_adoption_proof(
            adoption_proof,
            friendly_path,
            identity,
        )
        if adopted_identity != identity:
            raise AlbumPlacementAttention(
                "legacy adoption proof does not select the requested release"
            )
        return _placement(
            identity,
            friendly_path,
            friendly_path,
            PlacementDisposition.ADOPTED,
            live_receipt.path_receipt,
            live_receipt.path_receipt,
            live_receipt,
        )

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
        # Detached receipts remain only for compatibility with already-frozen
        # placement records. New adoption decisions must use a live proof.
        if (
            adoption_proof is None
            and adopted_identity == identity
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
