"""Finish durable queue-owned post-import folder actions."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import sys
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    CompletionOriginKind,
    RecoveryOwner,
    completion_acknowledgement_hash,
    completion_input_ready,
    parse_completion_input_record,
    parse_completion_record,
)
from qobuz_librarian.library.catalog import (
    _is_split_album_merge,
    multi_artist_migration_destination,
)
from qobuz_librarian.library.post_import_relocation import (
    PostImportRelocationUnavailable,
    RelocationKind,
    acknowledge_post_import_relocation,
    capture_post_import_relocation_expectation,
    release_post_import_relocation,
    relocate_post_import_album,
    seal_post_import_relocation_handoff,
)
from qobuz_librarian.library.release_authority import (
    AlbumAuthority,
    AlbumAuthorityUnavailable,
    FileVersion,
    file_version,
    open_album_authority,
)
from qobuz_librarian.library.release_identity import (
    ReleaseManifestError,
    _close_descriptors,
    _directory_chain_matches,
    _open_album_directory,
    capture_directory_path_receipt,
    publish_release_identity_authorized_retained,
    reconcile_release_manifest_transaction,
    retain_release_identity_authorized,
)
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.run_lock import RunLockLease


class PostImportFinalizationUnavailable(OSError):
    """A durable queue completion cannot yet be finalised safely."""


@dataclass(frozen=True, slots=True)
class _ExpectedAudio:
    relative: Path
    identity: tuple[int, int, int, int, int] | None
    digest: str | None


@dataclass(slots=True)
class _HeldInventoryDirectory:
    descriptor: int
    parent_descriptor: int
    name: str
    version: FileVersion
    entries: tuple[str, ...]


def _inventory_directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
    )


def _normalise_expected_audio(expected_audio) -> tuple[_ExpectedAudio, ...]:
    if type(expected_audio) is not dict:
        raise AlbumAuthorityUnavailable("verified audio inventory is invalid")
    values = []
    for relative, evidence in expected_audio.items():
        if not isinstance(relative, Path) or relative.is_absolute():
            raise AlbumAuthorityUnavailable("verified audio inventory is invalid")
        try:
            identity, digest = evidence
        except (TypeError, ValueError) as exc:
            raise AlbumAuthorityUnavailable(
                "verified audio inventory is invalid"
            ) from exc
        if identity is not None:
            if (
                type(identity) not in {tuple, list}
                or len(identity) != 5
                or any(type(value) is not int for value in identity)
            ):
                raise AlbumAuthorityUnavailable("verified audio inventory is invalid")
            identity = tuple(identity)
        if digest is not None and (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise AlbumAuthorityUnavailable("verified audio inventory is invalid")
        values.append(_ExpectedAudio(relative, identity, digest))
    values.sort(key=lambda value: os.fsencode(value.relative.as_posix()))
    if len({value.relative for value in values}) != len(values):
        raise AlbumAuthorityUnavailable("verified audio inventory is invalid")
    return tuple(values)


class VerifiedAlbumInventory(AbstractContextManager):
    """One live exact audio proof retained through final retirement."""

    def __init__(self, path, authority, expected_receipt, expected_audio):
        self._path = Path(path)
        self._run_lock = authority
        self._expected_receipt = expected_receipt
        self._expected_audio = _normalise_expected_audio(expected_audio)
        self._album_context = None
        self._album: AlbumAuthority | None = None
        self._manifest = None
        self._directories: list[_HeldInventoryDirectory] = []
        self._entered = False

    @staticmethod
    def _entry_names(descriptor: int) -> tuple[str, ...]:
        return tuple(sorted(os.listdir(descriptor), key=os.fsencode))

    def _scan_audio_tree(
        self,
        descriptor: int,
        prefix: tuple[str, ...] = (),
    ) -> set[Path]:
        audio = set()
        for name in self._entry_names(descriptor):
            named_before = os.stat(
                name, dir_fd=descriptor, follow_symlinks=False
            )
            relative = Path(*prefix, name)
            if stat.S_ISDIR(named_before.st_mode):
                child = os.open(
                    name,
                    _inventory_directory_flags(),
                    dir_fd=descriptor,
                )
                try:
                    held_before = os.fstat(child)
                    entries = self._entry_names(child)
                    version = file_version(held_before)
                    if (
                        not stat.S_ISDIR(held_before.st_mode)
                        or file_version(named_before) != version
                    ):
                        raise AlbumAuthorityUnavailable(
                            "album directory changed during inventory proof"
                        )
                    nested_audio = self._scan_audio_tree(
                        child, (*prefix, name)
                    )
                    named_after = os.stat(
                        name, dir_fd=descriptor, follow_symlinks=False
                    )
                    held_after = os.fstat(child)
                    if (
                        not stat.S_ISDIR(named_after.st_mode)
                        or not stat.S_ISDIR(held_after.st_mode)
                        or file_version(named_after) != version
                        or file_version(held_after) != version
                        or self._entry_names(child) != entries
                    ):
                        raise AlbumAuthorityUnavailable(
                            "album directory changed during inventory proof"
                        )
                    audio.update(nested_audio)
                finally:
                    if child >= 0:
                        os.close(child)
                continue
            if relative.suffix.lower() in cfg.AUDIO_EXTS:
                if not stat.S_ISREG(named_before.st_mode):
                    raise AlbumAuthorityUnavailable(
                        "verified audio inventory contains an unsafe entry"
                    )
                audio.add(relative)
        return audio

    def _hold_directories(self, descriptor: int) -> None:
        for name in self._entry_names(descriptor):
            named_before = os.stat(
                name, dir_fd=descriptor, follow_symlinks=False
            )
            if not stat.S_ISDIR(named_before.st_mode):
                continue
            child = os.open(
                name,
                _inventory_directory_flags(),
                dir_fd=descriptor,
            )
            try:
                held_before = os.fstat(child)
                entries = self._entry_names(child)
                version = file_version(held_before)
                if (
                    not stat.S_ISDIR(held_before.st_mode)
                    or file_version(named_before) != version
                ):
                    raise AlbumAuthorityUnavailable(
                        "album directory changed during inventory proof"
                    )
                binding = _HeldInventoryDirectory(
                    child, descriptor, name, version, entries
                )
                self._directories.append(binding)
                child = -1
                self._hold_directories(binding.descriptor)
                named_after = os.stat(
                    name, dir_fd=descriptor, follow_symlinks=False
                )
                if (
                    not stat.S_ISDIR(named_after.st_mode)
                    or file_version(named_after) != version
                    or file_version(os.fstat(binding.descriptor)) != version
                    or self._entry_names(binding.descriptor) != entries
                ):
                    raise AlbumAuthorityUnavailable(
                        "album directory changed during inventory proof"
                    )
            finally:
                if child >= 0:
                    os.close(child)

    def _validate_directories(self) -> None:
        for binding in self._directories:
            try:
                held = os.fstat(binding.descriptor)
                named = os.stat(
                    binding.name,
                    dir_fd=binding.parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise AlbumAuthorityUnavailable(
                    "album directory changed under verified inventory"
                ) from exc
            if (
                not stat.S_ISDIR(held.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or file_version(held) != binding.version
                or file_version(named) != binding.version
                or self._entry_names(binding.descriptor) != binding.entries
            ):
                raise AlbumAuthorityUnavailable(
                    "album directory changed under verified inventory"
                )

    def __enter__(self):
        if self._entered:
            raise AlbumAuthorityUnavailable(
                "verified album inventory context is one-shot"
            )
        self._entered = True
        self._album_context = open_album_authority(
            self._path,
            self._run_lock,
            expected_path=self._expected_receipt,
        )
        try:
            self._album = self._album_context.__enter__()
            found = self._scan_audio_tree(self._album.directory_descriptor)
            expected = {value.relative for value in self._expected_audio}
            if found != expected:
                raise AlbumAuthorityUnavailable(
                    "release destination audio inventory changed"
                )
            for item in self._expected_audio:
                held = self._album.open_file(
                    item.relative,
                    expected_digest=item.digest,
                )
                version = (
                    held.version.device,
                    held.version.inode,
                    held.version.size,
                    held.version.mtime_ns,
                    held.version.ctime_ns,
                )
                if item.identity is not None and version != item.identity:
                    raise AlbumAuthorityUnavailable(
                        "release destination audio identity changed"
                    )
            self._hold_directories(self._album.directory_descriptor)
            self.validate_namespace()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def validate_namespace(self) -> None:
        if self._album is None:
            raise AlbumAuthorityUnavailable("verified album inventory is not live")
        self._album.validate_namespace()
        self._validate_directories()
        found = self._current_audio_paths(
            self._album.directory_descriptor,
        )
        if found != {value.relative for value in self._expected_audio}:
            raise AlbumAuthorityUnavailable(
                "release destination audio inventory changed"
            )
        self._validate_directories()
        self._album.validate_namespace()
        if self._manifest is not None:
            self._manifest.validate_namespace()

    def retain_manifest(self, identity) -> None:
        if self._album is None:
            raise AlbumAuthorityUnavailable("verified album inventory is not live")
        if self._manifest is not None:
            self._manifest.validate_namespace()
            return
        self._manifest = retain_release_identity_authorized(
            self._album,
            identity,
        )
        self.validate_namespace()

    def _current_audio_paths(
        self,
        descriptor: int,
        prefix: tuple[str, ...] = (),
    ) -> set[Path]:
        audio = set()
        before = os.fstat(descriptor)
        for name in self._entry_names(descriptor):
            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            relative = Path(*prefix, name)
            if stat.S_ISDIR(named.st_mode):
                child = os.open(
                    name,
                    _inventory_directory_flags(),
                    dir_fd=descriptor,
                )
                try:
                    child_version = file_version(os.fstat(child))
                    if child_version != file_version(named):
                        raise AlbumAuthorityUnavailable(
                            "album directory changed under verified inventory"
                        )
                    audio.update(self._current_audio_paths(
                        child, (*prefix, name)
                    ))
                    if child_version != file_version(os.fstat(child)):
                        raise AlbumAuthorityUnavailable(
                            "album directory changed under verified inventory"
                        )
                finally:
                    os.close(child)
            elif relative.suffix.lower() in cfg.AUDIO_EXTS:
                if not stat.S_ISREG(named.st_mode):
                    raise AlbumAuthorityUnavailable(
                        "verified audio inventory contains an unsafe entry"
                    )
                audio.add(relative)
        if file_version(os.fstat(descriptor)) != file_version(before):
            raise AlbumAuthorityUnavailable(
                "album directory changed under verified inventory"
            )
        return audio

    def publish(self, identity) -> None:
        self.validate_namespace()
        assert self._album is not None
        if self._manifest is not None:
            raise AlbumAuthorityUnavailable(
                "release manifest authority is already retained"
            )
        self._manifest = publish_release_identity_authorized_retained(
            self._album,
            identity,
        )
        self.validate_namespace()

    def read_identity(self):
        self.validate_namespace()
        assert self._album is not None
        identity = reconcile_release_manifest_transaction(self._album)
        self.validate_namespace()
        return identity

    def __exit__(self, exc_type, exc, traceback):
        cleanup_error = None
        if exc_type is None and self._album is not None:
            try:
                self.validate_namespace()
            except BaseException as caught:
                cleanup_error = caught
        manifest = self._manifest
        self._manifest = None
        if manifest is not None:
            try:
                manifest.close()
            except BaseException as caught:
                if cleanup_error is None:
                    cleanup_error = caught
        for binding in reversed(self._directories):
            try:
                os.close(binding.descriptor)
            except OSError as caught:
                if cleanup_error is None:
                    cleanup_error = caught
        self._directories = []
        album_context = self._album_context
        self._album = None
        self._album_context = None
        if album_context is not None:
            context_error_type = (
                exc_type
                if exc_type is not None
                else type(cleanup_error)
                if cleanup_error is not None
                else None
            )
            try:
                album_context.__exit__(
                    context_error_type,
                    exc if exc_type is not None else cleanup_error,
                    traceback,
                )
            except BaseException as caught:
                if cleanup_error is None:
                    cleanup_error = caught
        if exc_type is None and cleanup_error is not None:
            raise cleanup_error
        return False


def open_verified_album_inventory(
    path,
    authority,
    expected_receipt,
    expected_audio,
) -> AbstractContextManager[VerifiedAlbumInventory]:
    return VerifiedAlbumInventory(
        path,
        authority,
        expected_receipt,
        expected_audio,
    )


def _require_authority(authority: RunLockLease) -> None:
    if type(authority) is not RunLockLease or authority.intact() is not True:
        raise PostImportFinalizationUnavailable(
            errno.EBUSY, "the shared run lock was lost during queue finalisation"
        )


def _retirement(journal, item_id):
    retirement = next(
        (value for value in journal.retirements if value.item_id == item_id),
        None,
    )
    if retirement is None:
        raise KeyError(f"unknown carrier retirement: {item_id}")
    return retirement


def plan_post_import_action(
    journal,
    item_id,
    post_dir,
    *,
    authority: RunLockLease,
):
    """Capture at most one exact folder action while completion proof is live."""
    _require_authority(authority)
    item = next((value for value in journal.items if value.item_id == item_id), None)
    if item is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    source = None
    destination = None
    kind = None
    post_path = Path(post_dir)
    planned = item.planned
    album = planned["album"]
    album_dir = (
        Path(planned["album_dir"])
        if planned.get("album_dir") is not None
        else None
    )
    artist = (album.get("artist") or {}).get("name") or ""

    # Reuniting a split gap-fill already lands in the primary artist folder,
    # so it takes precedence over the optional whole-folder filing pass.
    if _is_split_album_merge(album_dir, post_path, artist):
        source = album_dir
        destination = post_path
        kind = RelocationKind.SPLIT_GAP_FILL
    elif item.multi_artist_filing:
        candidate = multi_artist_migration_destination(album, post_path)
        if candidate is not None:
            source = post_path
            destination = candidate
            kind = RelocationKind.WHOLE_ALBUM

    if source is None or destination is None or kind is None:
        return None
    expectation = capture_post_import_relocation_expectation(
        source,
        destination,
        authority=authority,
    )
    _require_authority(authority)
    return {
        "action_id": secrets.token_hex(32),
        "kind": kind.value,
        "source": str(source),
        "destination": str(destination),
        "expectation": expectation,
        "phase": queue_state.PostImportActionPhase.PLANNED.value,
        "relocation_operation_id": None,
        "handoff_hash": None,
    }


def _handoff_consumer(journal, retirement):
    action = retirement.action
    if action is None:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the post-import action is missing"
        )
    return {
        "kind": "queue-completion",
        "queue_operation_id": journal.operation_id,
        "item_id": retirement.item_id,
        "action_id": action.action_id,
    }


def _settle_action(
    journal,
    retirement,
    authority,
    *,
    validate_before_commit=None,
):
    action = retirement.action
    if action is None:
        return journal
    _require_authority(authority)
    if action.phase is queue_state.PostImportActionPhase.PLANNED:
        result = relocate_post_import_album(
            action.source,
            action.destination,
            kind=RelocationKind(action.kind),
            authority=authority,
            await_handoff=True,
            retain_completion=True,
            expected=action.expectation,
            require_no_conflicts=(
                action.kind == RelocationKind.SPLIT_GAP_FILL.value
            ),
        )
        if (
            result.changed is False
            and result.operation_id is None
            and result.ownership_receipt is None
            and result.published_files == 0
            and str(result.destination) == retirement.final_path
            and action.kind == RelocationKind.WHOLE_ALBUM.value
        ):
            return queue_state.clear_planned_noop_post_import_action(
                journal,
                item_id=retirement.item_id,
                action_id=action.action_id,
            )
        if (
            result.changed is not True
            or result.operation_id is None
            or result.ownership_receipt is None
            or (
                action.kind == RelocationKind.SPLIT_GAP_FILL.value
                and result.reason is not None
            )
        ):
            raise PostImportRelocationUnavailable(
                errno.EBUSY, "the recorded post-import action made no exact move"
            )
        handoff_hash = seal_post_import_relocation_handoff(
            result.operation_id,
            consumer=_handoff_consumer(journal, retirement),
            payload={
                "operation_id": result.operation_id,
                "move": result.ownership_receipt,
            },
            authority=authority,
        )
        _require_authority(authority)
        return queue_state.checkpoint_post_import_action_handoff(
            journal,
            item_id=retirement.item_id,
            action_id=action.action_id,
            operation_id=result.operation_id,
            handoff_hash=handoff_hash,
        )

    operation_id = action.relocation_operation_id
    handoff_hash = action.handoff_hash
    if operation_id is None or handoff_hash is None:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the post-import handoff is incomplete"
        )
    if action.phase is queue_state.PostImportActionPhase.HANDOFF:
        acknowledge_post_import_relocation(
            operation_id,
            handoff_hash,
            authority=authority,
        )
        _require_authority(authority)
        return queue_state.commit_post_import_action(
            journal,
            item_id=retirement.item_id,
            action_id=action.action_id,
            operation_id=operation_id,
            handoff_hash=handoff_hash,
            final_path=action.destination,
        )

    if action.phase is queue_state.PostImportActionPhase.COMMITTED:
        release_post_import_relocation(
            operation_id,
            handoff_hash,
            authority=authority,
        )
        _require_authority(authority)
        if validate_before_commit is not None:
            validate_before_commit()
        return queue_state.clear_committed_post_import_action(
            journal,
            item_id=retirement.item_id,
            action_id=action.action_id,
        )
    raise PostImportFinalizationUnavailable(
        errno.EINVAL, "the post-import action phase is invalid"
    )


def _acknowledge_completion(
    journal,
    retirement,
    acknowledge_completion,
    *,
    validate_before_commit=None,
    persist=True,
):
    if retirement.completion_acknowledged:
        if validate_before_commit is not None:
            validate_before_commit()
        return journal
    owner = RecoveryOwner(journal.operation_id, retirement.item_id)
    completion_input = parse_completion_input_record(
        retirement.completion_input,
        expected_owner=owner,
    )
    if completion_input is None or not completion_input_ready(completion_input):
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the saved completion input is invalid"
        )
    evidence = parse_completion_record(
        retirement.completion_evidence,
        expected_owner=owner,
        expected_expectation=completion_input.expectation,
        expected_download=completion_input.download_coverage(),
    )
    if evidence is None:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the saved completion evidence is invalid"
        )
    if (
        completion_input.origin.kind is CompletionOriginKind.CLI
        and not callable(acknowledge_completion)
    ):
        # The queue journal is the CLI queue's durable authority.  Completed
        # removal already deleted this exact item in the same CAS that created
        # the retirement, so no second store needs to acknowledge it.
        if validate_before_commit is not None:
            validate_before_commit()
        if persist:
            return queue_state.acknowledge_carrier_retirement_completion(
                journal,
                item_id=retirement.item_id,
            )
        return journal
    if not callable(acknowledge_completion):
        raise PostImportFinalizationUnavailable(
            errno.EAGAIN, "the completion owner is not available to acknowledge"
        )
    if completion_input.origin.kind not in {
        CompletionOriginKind.CLI,
        CompletionOriginKind.WEB_JOB,
    }:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the completion owner is invalid"
        )
    try:
        acknowledged = acknowledge_completion(
            completion_input.origin,
            owner,
            album_id=completion_input.expectation.album_id,
            completion_hash=completion_acknowledgement_hash(
                completion_input,
                evidence,
            ),
            planned=retirement.planned,
            post_dir=retirement.final_path,
        )
    except Exception as exc:
        raise PostImportFinalizationUnavailable(
            errno.EIO, "the completion owner could not be acknowledged"
        ) from exc
    if acknowledged is not True:
        raise PostImportFinalizationUnavailable(
            errno.EAGAIN, "the completion owner did not acknowledge"
        )
    if validate_before_commit is not None:
        validate_before_commit()
    if persist:
        return queue_state.acknowledge_carrier_retirement_completion(
            journal,
            item_id=retirement.item_id,
        )
    return journal


def _current_inventory_matches(
    evidence,
    final_path: str,
    *,
    relocated: bool,
    path_receipt=None,
) -> bool:
    """Recheck exact audio bytes at the settled path immediately before publish."""
    inventory = evidence.inventory
    if inventory is None or inventory.path != evidence.album_path:
        return False
    album_parts = PurePosixPath(evidence.album_path).parts
    expected = {}
    for receipt in inventory.audio:
        parts = PurePosixPath(receipt.path).parts
        if len(parts) <= len(album_parts) or parts[:len(album_parts)] != album_parts:
            return False
        relative = PurePosixPath(*parts[len(album_parts):]).as_posix()
        if relative in expected:
            return False
        expected[relative] = receipt
    if path_receipt is None:
        try:
            path_receipt = capture_directory_path_receipt(Path(final_path))
        except (ReleaseManifestError, OSError):
            return False
    descriptors = ()
    try:
        opened_receipt, descriptors = _open_album_directory(
            Path(final_path),
            expected_path_receipt=path_receipt,
        )
        if (
            opened_receipt != path_receipt
            or not _directory_chain_matches(opened_receipt, descriptors)
        ):
            return False
        seen = set()
        if not _audit_audio_tree_at(
            descriptors[-1],
            (),
            expected,
            seen,
            relocated=relocated,
        ):
            return False
        return bool(
            seen == set(expected)
            and _directory_chain_matches(opened_receipt, descriptors)
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        _close_descriptors(descriptors)


def _audit_file_version(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _audit_directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
    )


def _audit_regular_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _audit_audio_file_at(
    parent_descriptor,
    name,
    receipt,
    *,
    relocated,
) -> bool:
    descriptor = None
    try:
        descriptor = os.open(
            name,
            _audit_regular_flags(),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        digest_value = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest_value.update(chunk)
        after = os.fstat(descriptor)
        final_named = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        final = os.fstat(descriptor)
        versions = tuple(
            _audit_file_version(value)
            for value in (before, named, after, final_named, final)
        )
        return bool(
            all(stat.S_ISREG(value.st_mode) for value in (
                before, named, after, final_named, final
            ))
            and len(set(versions)) == 1
            and digest_value.hexdigest() == receipt.sha256
            and (relocated or versions[-1] == receipt.identity)
        )
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _audit_audio_tree_at(
    directory_descriptor,
    prefix,
    expected,
    seen,
    *,
    relocated,
) -> bool:
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
        relative = PurePosixPath(*relative_parts).as_posix()
        if stat.S_ISDIR(named.st_mode):
            child = None
            try:
                child = os.open(
                    name,
                    _audit_directory_flags(),
                    dir_fd=directory_descriptor,
                )
                held_before = os.fstat(child)
                if (
                    _audit_file_version(named)
                    != _audit_file_version(held_before)
                    or not _audit_audio_tree_at(
                        child,
                        relative_parts,
                        expected,
                        seen,
                        relocated=relocated,
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
                    _audit_file_version(named),
                    _audit_file_version(held_before),
                    _audit_file_version(named_after),
                    _audit_file_version(held_final),
                }) != 1:
                    return False
            finally:
                if child is not None:
                    os.close(child)
            continue
        if Path(name).suffix.lower() not in cfg.AUDIO_EXTS:
            continue
        if not stat.S_ISREG(named.st_mode) or relative not in expected:
            return False
        if relative in seen or not _audit_audio_file_at(
            directory_descriptor,
            name,
            expected[relative],
            relocated=relocated,
        ):
            return False
        seen.add(relative)
    final = os.fstat(directory_descriptor)
    return (
        stat.S_ISDIR(final.st_mode)
        and _audit_file_version(before) == _audit_file_version(final)
    )


@dataclass(frozen=True, slots=True)
class _RetirementPublication:
    completion_input: object
    evidence: object
    completion_path: str
    final_path: str
    path_receipt: object
    committed_whole_album: bool
    relocated_after_publication: bool
    expected_audio: dict


def _retirement_expected_audio(evidence, *, relocated: bool) -> dict:
    inventory = evidence.inventory
    if inventory is None or inventory.path != evidence.album_path:
        raise ReleaseManifestError("release destination audio inventory changed")
    album_parts = PurePosixPath(evidence.album_path).parts
    expected = {}
    for receipt in inventory.audio:
        parts = PurePosixPath(receipt.path).parts
        if len(parts) <= len(album_parts) or parts[:len(album_parts)] != album_parts:
            raise ReleaseManifestError(
                "release destination audio inventory changed"
            )
        relative = Path(*parts[len(album_parts):])
        if relative in expected:
            raise ReleaseManifestError(
                "release destination audio inventory changed"
            )
        expected[relative] = (
            None if relocated else receipt.identity,
            receipt.sha256,
        )
    return expected


def _retirement_publication(journal, retirement):
    if (
        retirement.planned is None
        and retirement.completion_input is None
        and retirement.completion_evidence is None
        and retirement.final_path is None
    ):
        # Legacy carrier-only retirements predate durable completion records.
        return None
    owner = RecoveryOwner(journal.operation_id, retirement.item_id)
    completion_input = parse_completion_input_record(
        retirement.completion_input,
        expected_owner=owner,
    )
    if (
        completion_input is None
        or not completion_input_ready(completion_input)
    ):
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the saved release identity plan is invalid"
        )
    if (
        completion_input.release_identity is None
        and completion_input.placement_destination is None
    ):
        # Pre-identity journals have no authority to invent a manifest. They
        # retain the previous retirement contract while all newly admitted
        # durable work carries the version-2 release plan below.
        return None
    evidence = parse_completion_record(
        retirement.completion_evidence,
        expected_owner=owner,
        expected_expectation=completion_input.expectation,
        expected_download=completion_input.download_coverage(),
    )
    if evidence is None:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the saved completion evidence is invalid"
        )
    completion_path = os.path.abspath(os.path.join(
        evidence.library_root,
        evidence.album_path,
    ))
    if completion_path != completion_input.placement_destination:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the completed album missed its planned destination"
        )
    final_path = retirement.final_path
    if (
        type(final_path) is not str
        or not os.path.isabs(final_path)
        or os.path.abspath(final_path) != final_path
    ):
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the settled release path is invalid"
        )
    action = retirement.action
    if (
        action is not None
        and action.phase is not queue_state.PostImportActionPhase.COMMITTED
    ):
        raise PostImportFinalizationUnavailable(
            errno.EBUSY, "the post-import relocation is not committed"
        )
    if action is not None and action.destination != final_path:
        raise PostImportFinalizationUnavailable(
            errno.EINVAL, "the settled release path does not match relocation"
        )
    try:
        path_receipt = capture_directory_path_receipt(Path(final_path))
    except (ReleaseManifestError, OSError) as exc:
        raise ReleaseManifestError("release destination is unavailable") from exc
    directory_identity = path_receipt.directory_identity
    if not path_receipt.exists or directory_identity is None:
        raise ReleaseManifestError(
            "release destination does not match completed album identity"
        )
    relocated_after_publication = action is None and final_path != completion_path
    node_matches_completion = (
        directory_identity == evidence.album_identity[:2]
    )
    committed_whole_album = (
        action is not None
        and action.kind == RelocationKind.WHOLE_ALBUM.value
    )
    if (
        not node_matches_completion
        and not committed_whole_album
        and not relocated_after_publication
    ):
        raise ReleaseManifestError(
            "release destination does not match completed album identity"
        )
    relocated = committed_whole_album or relocated_after_publication
    return _RetirementPublication(
        completion_input,
        evidence,
        completion_path,
        final_path,
        path_receipt,
        committed_whole_album,
        relocated_after_publication,
        _retirement_expected_audio(evidence, relocated=relocated),
    )


def _publish_retirement_identity(
    journal,
    retirement,
    *,
    authority,
    cancel_check=None,
    inventory=None,
    publication=None,
) -> bool:
    """Publish only while the exact completion inventory remains live."""
    if publication is None:
        publication = _retirement_publication(journal, retirement)
    if publication is None:
        return False
    if inventory is None:
        with open_verified_album_inventory(
            Path(publication.final_path),
            authority,
            publication.path_receipt,
            publication.expected_audio,
        ) as held:
            return _publish_retirement_identity(
                journal,
                retirement,
                authority=authority,
                cancel_check=cancel_check,
                inventory=held,
                publication=publication,
            )
    if cancel_check is not None and cancel_check():
        raise PostImportFinalizationUnavailable(
            errno.ECANCELED, "queue finalisation was cancelled"
        )
    _require_authority(authority)
    inventory.validate_namespace()
    if publication.relocated_after_publication:
        if inventory.read_identity() != publication.completion_input.release_identity:
            raise ReleaseManifestError(
                "relocated release identity is unavailable after publication"
            )
        inventory.retain_manifest(publication.completion_input.release_identity)
        return False
    changed = (
        inventory.read_identity()
        != publication.completion_input.release_identity
    )
    inventory.publish(publication.completion_input.release_identity)
    _require_authority(authority)
    inventory.validate_namespace()
    return changed


def finalize_carrier_retirement(
    journal,
    item_id,
    *,
    authority: RunLockLease,
    acknowledge_completion,
    cancel_check=None,
):
    """Settle action, external owner, then the exact managed carrier."""
    _require_authority(authority)
    if cancel_check is not None and cancel_check():
        raise PostImportFinalizationUnavailable(
            errno.ECANCELED, "queue finalisation was cancelled"
        )
    action_was_pending = _retirement(journal, item_id).action is not None
    for _unused in range(4):
        retirement = _retirement(journal, item_id)
        if (
            retirement.action is None
            or retirement.action.phase
            is queue_state.PostImportActionPhase.COMMITTED
        ):
            break
        journal = _settle_action(journal, retirement, authority)
        if cancel_check is not None and cancel_check():
            raise PostImportFinalizationUnavailable(
                errno.ECANCELED, "queue finalisation was cancelled"
            )
    retirement = _retirement(journal, item_id)
    if (
        retirement.action is not None
        and retirement.action.phase
        is not queue_state.PostImportActionPhase.COMMITTED
    ):
        raise PostImportFinalizationUnavailable(
            errno.EBUSY, "the post-import action did not settle"
        )
    if action_was_pending:
        from qobuz_librarian.library.scanner import clear_scan_caches

        clear_scan_caches()

    if cancel_check is not None and cancel_check():
        raise PostImportFinalizationUnavailable(
            errno.ECANCELED, "queue finalisation was cancelled"
        )
    publication = _retirement_publication(journal, retirement)
    inventory_context = (
        nullcontext(None)
        if publication is None
        else open_verified_album_inventory(
            Path(publication.final_path),
            authority,
            publication.path_receipt,
            publication.expected_audio,
        )
    )
    with inventory_context as inventory:
        _publish_retirement_identity(
            journal,
            retirement,
            authority=authority,
            cancel_check=cancel_check,
            inventory=inventory,
            publication=publication,
        )
        if inventory is not None:
            inventory.validate_namespace()
        if cancel_check is not None and cancel_check():
            raise PostImportFinalizationUnavailable(
                errno.ECANCELED,
                "queue finalisation was cancelled",
            )
        if retirement.action is not None:
            if inventory is None:
                journal = _settle_action(journal, retirement, authority)
            else:
                journal = _settle_action(
                    journal,
                    retirement,
                    authority,
                    validate_before_commit=inventory.validate_namespace,
                )
            retirement = _retirement(journal, item_id)
            if retirement.action is not None:
                raise PostImportFinalizationUnavailable(
                    errno.EBUSY,
                    "the post-import relocation could not be released",
                )
        if inventory is not None:
            inventory.validate_namespace()
        if inventory is None:
            journal = _acknowledge_completion(
                journal,
                retirement,
                acknowledge_completion,
            )
        else:
            journal = _acknowledge_completion(
                journal,
                retirement,
                acknowledge_completion,
                validate_before_commit=inventory.validate_namespace,
                persist=False,
            )
        retirement = _retirement(journal, item_id)
        final_path = Path(retirement.final_path) if retirement.final_path else None
        if inventory is not None:
            inventory.validate_namespace()
        _require_authority(authority)
        journal, carrier_result = queue_state.process_carrier_retirement(
            journal,
            item_id=item_id,
            pre_commit_validator=(
                inventory.validate_namespace
                if inventory is not None
                else None
            ),
        )
        _require_authority(authority)
    return journal, final_path, carrier_result
