"""Finish durable queue-owned post-import folder actions."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
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
from qobuz_librarian.library.release_identity import (
    ReleaseManifestError,
    publish_release_identity,
    read_release_identity,
)
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.run_lock import RunLockLease


class PostImportFinalizationUnavailable(OSError):
    """A durable queue completion cannot yet be finalised safely."""


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


def _settle_action(journal, retirement, authority):
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
        )
        if (
            result.changed is False
            and result.operation_id is None
            and result.ownership_receipt is None
            and result.published_files == 0
            and str(result.destination) == retirement.final_path
            and action.kind == RelocationKind.WHOLE_ALBUM.value
            and result.reason == "there is nothing to relocate"
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
        return queue_state.clear_committed_post_import_action(
            journal,
            item_id=retirement.item_id,
            action_id=action.action_id,
        )
    raise PostImportFinalizationUnavailable(
        errno.EINVAL, "the post-import action phase is invalid"
    )


def _acknowledge_completion(journal, retirement, acknowledge_completion):
    if retirement.completion_acknowledged:
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
        return queue_state.acknowledge_carrier_retirement_completion(
            journal,
            item_id=retirement.item_id,
        )
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
    return queue_state.acknowledge_carrier_retirement_completion(
        journal,
        item_id=retirement.item_id,
    )


def _current_inventory_matches(evidence, final_path: str, *, relocated: bool) -> bool:
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
    try:
        actual = {
            path.relative_to(final_path).as_posix(): path
            for path in Path(final_path).rglob("*")
            if path.suffix.lower() in cfg.AUDIO_EXTS and path.is_file()
        }
    except OSError:
        return False
    if set(actual) != set(expected):
        return False
    for relative, path in actual.items():
        receipt = expected[relative]
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                return False
            digest_value = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest_value.update(chunk)
            digest = digest_value.hexdigest()
            after = os.fstat(descriptor)
            named = os.lstat(path)
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
        identity = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if (
            (before.st_dev, before.st_ino, before.st_size,
             before.st_mtime_ns, before.st_ctime_ns) != identity
            or (named.st_dev, named.st_ino) != identity[:2]
            or digest != receipt.sha256
            or (not relocated and identity != receipt.identity)
        ):
            return False
    return True


def _publish_retirement_identity(
    journal, retirement, *, authority, cancel_check=None
) -> bool:
    """Publish only from the retained exact completion and destination proof."""
    if (
        retirement.planned is None
        and retirement.completion_input is None
        and retirement.completion_evidence is None
        and retirement.final_path is None
    ):
        # Legacy carrier-only retirements predate durable completion records.
        return False
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
        return False
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
        directory = os.lstat(final_path)
    except OSError as exc:
        raise ReleaseManifestError("release destination is unavailable") from exc
    if not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode):
        raise ReleaseManifestError(
            "release destination does not match completed album identity"
        )
    relocated_after_publication = action is None and final_path != completion_path
    if relocated_after_publication:
        if not _current_inventory_matches(evidence, final_path, relocated=True):
            raise ReleaseManifestError("release destination audio inventory changed")
        _require_authority(authority)
        existing = read_release_identity(Path(final_path))
        after_read = os.lstat(final_path)
        if (
            existing != completion_input.release_identity
            or not stat.S_ISDIR(after_read.st_mode)
            or stat.S_ISLNK(after_read.st_mode)
            or (after_read.st_dev, after_read.st_ino)
            != (directory.st_dev, directory.st_ino)
        ):
            # A committed whole-album relocation can clear its retained receipt
            # only after publication. Once cleared, the held same-directory
            # manifest is the sole authority for an acknowledgement retry;
            # never recreate one at a path no longer bound to live evidence.
            raise ReleaseManifestError(
                "relocated release identity is unavailable after publication"
            )
        _require_authority(authority)
        return False
    node_matches_completion = (
        (directory.st_dev, directory.st_ino)
        == evidence.album_identity[:2]
    )
    committed_whole_album = (
        action is not None
        and action.kind == RelocationKind.WHOLE_ALBUM.value
    )
    if (
        not node_matches_completion
        and not committed_whole_album
    ):
        raise ReleaseManifestError(
            "release destination does not match completed album identity"
        )
    if not _current_inventory_matches(
        evidence, final_path, relocated=committed_whole_album
    ):
        raise ReleaseManifestError("release destination audio inventory changed")
    if cancel_check is not None and cancel_check():
        raise PostImportFinalizationUnavailable(
            errno.ECANCELED, "queue finalisation was cancelled"
        )
    _require_authority(authority)
    changed = publish_release_identity(
        Path(final_path),
        completion_input.release_identity,
        expected_directory=(directory.st_dev, directory.st_ino),
    )
    _require_authority(authority)
    if not _current_inventory_matches(
        evidence, final_path, relocated=committed_whole_album
    ):
        raise ReleaseManifestError("release destination audio inventory changed")
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
    _publish_retirement_identity(
        journal,
        retirement,
        authority=authority,
        cancel_check=cancel_check,
    )
    if retirement.action is not None:
        journal = _settle_action(journal, retirement, authority)
        retirement = _retirement(journal, item_id)
        if retirement.action is not None:
            raise PostImportFinalizationUnavailable(
                errno.EBUSY, "the post-import relocation could not be released"
            )
    journal = _acknowledge_completion(
        journal,
        retirement,
        acknowledge_completion,
    )
    retirement = _retirement(journal, item_id)
    final_path = Path(retirement.final_path) if retirement.final_path else None
    _require_authority(authority)
    journal, carrier_result = queue_state.process_carrier_retirement(
        journal,
        item_id=item_id,
    )
    _require_authority(authority)
    return journal, final_path, carrier_result
