import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    CompletionExpectation,
    CompletionInput,
    CompletionOrigin,
    CompletionOriginKind,
    CompletionScope,
    DownloadCounts,
    QualityTarget,
    RecoveryOwner,
    SourceLineage,
    StagedReceipt,
)
from qobuz_librarian.library import backup as backup_module
from qobuz_librarian.library import catalog
from qobuz_librarian.library.release_identity import (
    ReleaseIdentity,
    publish_release_identity,
    read_release_identity,
)
from qobuz_librarian.quality import decision
from qobuz_librarian.queue import journal, library_backup_recovery
from qobuz_librarian.queue.builder import _build_queue_item


def _old_entry(scope=None):
    entry = {
        "ts": (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    }
    if scope:
        entry["scope"] = scope
    return entry


def _durable_upgrade_carrier(tmp_path, monkeypatch):
    music = tmp_path / "music"
    album_dir = music / "Artist" / "Capped Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"old")
    (album_dir / "booklet.pdf").write_bytes(b"booklet")
    publish_release_identity(album_dir, ReleaseIdentity("qobuz", "100"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets" / "library.db")

    track = {"id": "101", "media_number": 1, "track_number": 1}
    album = {
        "id": "100",
        "title": "Capped Album",
        "maximum_bit_depth": 16,
        "maximum_sampling_rate": 44.1,
        "tracks": {"items": [track]},
    }
    queued = _build_queue_item(
        album=album,
        album_dir=album_dir,
        label="Capped Album",
        missing=[track],
        present=[],
        upgrade_only=False,
        auto_upgrade=True,
        quality=4,
    )
    current = journal.save_queue_journal(
        journal.create_queue_journal([queued], mode="test:capped"))
    item_id = current.items[0].item_id
    owner = RecoveryOwner(current.operation_id, item_id)
    owner_record = {
        "operation_id": owner.operation_id,
        "item_id": owner.item_id,
    }
    slots = ("qobuz:101",)
    completion = CompletionInput(
        owner=owner,
        origin=CompletionOrigin(
            CompletionOriginKind.CLI, "test-capped-durability"),
        expectation=CompletionExpectation(
            album_id="100",
            scope=CompletionScope.ALBUM,
            catalogue_slots=slots,
            requested_slots=slots,
            quality_targets=(QualityTarget(slots[0], 16, 44_100),),
        ),
        effective_tier=4,
        release_identity=ReleaseIdentity("qobuz", "100"),
        placement_destination=str(album_dir),
        lineages=(SourceLineage(
            slot=slots[0],
            origin=StagedReceipt(
                path=str(tmp_path / "staging" / "01.flac"),
                identity=(1, 2, 3, 4, 5, 6),
            ),
        ),),
        counts=DownloadCounts(),
    )
    current = journal.transition_journal_item(
        current,
        item_id,
        journal.QueuePhase.ACTIVE,
        completion_input=completion,
    )
    nonce = "d" * 64
    managed_path = str(
        cfg.BEETS_DB_PATH.parent / f".qobuz-managed-beets-{nonce}.jsonl")
    reservation = journal.RecoveryReference(
        "managed-import",
        "managed-beets-reservation",
        {
            "version": 1,
            "path": managed_path,
            "parent_device": 3,
            "parent_inode": 4,
            "nonce": nonce,
            "owner": owner_record,
        },
    )
    current = journal.reserve_managed_carrier(
        current, item_id, reservation)

    def save_intent(record):
        nonlocal current
        current = journal.append_library_backup_intent(
            current, item_id, record)

    backup = backup_module.backup_album_dir(
        album_dir,
        owner=owner_record,
        on_intent=save_intent,
    )
    assert backup is not None and backup.complete
    intent = current.items[0].recovery_references[0]
    carrier = backup_module.library_backup_record(
        backup, expected_owner=owner_record)
    assert carrier is not None
    current = journal.promote_library_backup_carrier(
        current, item_id, intent, carrier)
    managed_carrier = journal.RecoveryReference(
        "managed-import",
        "managed-beets",
        {
            "version": 2,
            "path": managed_path,
            "device": 1,
            "inode": 2,
            "parent_device": 3,
            "parent_inode": 4,
            "nonce": nonce,
            "owner": owner_record,
        },
    )
    current = journal.promote_managed_carrier(
        current,
        item_id,
        reservation,
        managed_carrier,
        completion,
    )

    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")
    monkeypatch.setattr(
        "qobuz_librarian.modes.process._upgrade_trees_verified",
        lambda *_paths: True,
    )
    return current, item_id, owner, owner_record, album, album_dir, backup


def _legacy_backup_result(backup, owner_record):
    receipt = dict(backup.receipt)
    receipt.pop("release_identity")
    receipt.pop("release_identity_receipt")
    (backup.path / backup_module._RECEIPT_SIDECAR).write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    legacy = backup_module.load_backup_result(
        backup.path, expected_owner=owner_record)
    assert legacy is not None
    return legacy


def _interrupt_companion_carry_after_first_rename(monkeypatch):
    real_rename = backup_module._rename_exact_noreplace_at

    def interrupt_after_first_companion_rename(
            source_fd, source_name, destination_fd, destination_name,
            expected_fd):
        deferred = real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            expected_fd,
        )
        if source_name == "f0000":
            assert deferred is None
            raise KeyboardInterrupt
        return deferred

    monkeypatch.setattr(
        backup_module,
        "_rename_exact_noreplace_at",
        interrupt_after_first_companion_rename,
    )
    return real_rename


def _library_backup_reference(snapshot):
    return next(
        reference
        for reference in snapshot.items[0].recovery_references
        if reference.name == "library-backup"
    )


def _persisted_companion_intent(backup):
    opened = backup_module._validated_backup_result(
        backup, require_complete=True)
    assert opened is not None
    try:
        present, intent = backup_module._companion_marker_value(
            opened[-1][-1],
            backup_module._COMPANION_CARRY_INTENT_SENTINEL,
            backup.receipt,
        )
        assert present and intent is not None
        return intent
    finally:
        backup_module._close_descriptors(opened[-1])


def test_old_local_downsample_marker_still_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(decision.cfg, "CAPPED_FILE", tmp_path / "capped.json")
    monkeypatch.setattr(decision.cfg, "MUSIC_ROOT", tmp_path)
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    key = decision._local_album_cap_key(album_dir)
    capped = {
        key: {**_old_entry(scope="local_album"), "album_dir": str(album_dir)}
    }
    assert decision.is_local_album_capped(album_dir, capped) is True


def test_old_qobuz_partial_marker_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(decision.cfg, "CAPPED_FILE", tmp_path / "capped.json")
    capped = {"12345": _old_entry()}
    assert decision.is_album_capped("12345", capped) is False


def test_imported_album_can_be_found_from_staged_track_signatures(tmp_path, monkeypatch):
    music = tmp_path / "music"
    album_dir = music / "Bill Evans" / "Waltz For Debby (2023)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - My Foolish Heart.flac"
    track.write_bytes(b"fake flac")
    signature = ("bill evans", "waltz for debby", 1, 1, "my foolish heart")

    monkeypatch.setattr(catalog.config, "MUSIC_ROOT", music)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.rip._flac_signature",
        lambda p: signature if p == track else None,
    )

    assert catalog.find_album_dir_by_track_signatures([signature]) == album_dir


def test_capped_durable_upgrade_carries_identity_before_companions_and_resumes(
        tmp_path, monkeypatch):
    (
        current,
        item_id,
        owner,
        _owner_record,
        album,
        album_dir,
        backup,
    ) = _durable_upgrade_carrier(tmp_path, monkeypatch)
    real_carry = library_backup_recovery.carry_backup_companions
    interrupted = []

    def interrupt_after_identity(*_args, **_kwargs):
        interrupted.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        library_backup_recovery,
        "carry_backup_companions",
        interrupt_after_identity,
    )
    with pytest.raises(KeyboardInterrupt):
        library_backup_recovery.prepare_library_backup_settlement(
            current,
            item_id,
            owner,
            album,
            album_dir,
            authority_check=lambda: None,
        )

    assert interrupted == [True]
    assert read_release_identity(album_dir) == ReleaseIdentity("qobuz", "100")
    assert backup.path.is_dir()
    persisted = journal.load_queue_journal(current.operation_id).journal
    assert persisted is not None
    assert _library_backup_reference(persisted).kind == "library-backup"

    monkeypatch.setattr(
        library_backup_recovery, "carry_backup_companions", real_carry)
    resumed = library_backup_recovery.prepare_library_backup_settlement(
        persisted,
        item_id,
        owner,
        album,
        album_dir,
        authority_check=lambda: None,
    )

    assert resumed.status is (
        library_backup_recovery.LibraryBackupResolutionStatus.READY)
    assert (album_dir / "booklet.pdf").read_bytes() == b"booklet"
    assert backup.path.is_dir()
    reloaded = journal.load_queue_journal(current.operation_id).journal
    assert reloaded == resumed.journal
    again = library_backup_recovery.prepare_library_backup_settlement(
        reloaded,
        item_id,
        owner,
        album,
        album_dir,
        authority_check=lambda: None,
    )
    assert again.status is (
        library_backup_recovery.LibraryBackupResolutionStatus.READY)
    assert again.journal == resumed.journal


def test_capped_durable_upgrade_resumes_after_one_companion_rename(
        tmp_path, monkeypatch):
    (
        current,
        item_id,
        owner,
        _owner_record,
        album,
        album_dir,
        backup,
    ) = _durable_upgrade_carrier(tmp_path, monkeypatch)
    real_rename = _interrupt_companion_carry_after_first_rename(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        library_backup_recovery.prepare_library_backup_settlement(
            current,
            item_id,
            owner,
            album,
            album_dir,
            authority_check=lambda: None,
        )

    assert read_release_identity(album_dir) == ReleaseIdentity("qobuz", "100")
    assert (album_dir / "booklet.pdf").read_bytes() == b"booklet"
    assert backup.path.is_dir()
    persisted = journal.load_queue_journal(current.operation_id).journal
    assert persisted is not None
    persisted_reference = _library_backup_reference(persisted)
    assert persisted_reference.kind == "library-backup"
    assert "disposal" not in persisted_reference.data
    pre_receipt = _persisted_companion_intent(backup)["pre_receipt"]
    monkeypatch.setattr(
        backup_module, "_rename_exact_noreplace_at", real_rename)

    resumed = library_backup_recovery.prepare_library_backup_settlement(
        persisted,
        item_id,
        owner,
        album,
        album_dir,
        authority_check=lambda: None,
    )

    assert resumed.status is (
        library_backup_recovery.LibraryBackupResolutionStatus.READY)
    assert (album_dir / "booklet.pdf").read_bytes() == b"booklet"
    assert backup.path.is_dir()
    assert _persisted_companion_intent(backup)["pre_receipt"] == pre_receipt
    reloaded = journal.load_queue_journal(current.operation_id).journal
    assert reloaded == resumed.journal
    settlement = _library_backup_reference(reloaded)
    assert settlement.kind == "library-backup-settlement"
    assert settlement.data["disposal"] is not None
    assert backup_module._COMPANION_CARRY_COMMITTED_SENTINEL in (
        settlement.data["disposal"]["snapshot"]["files"])

    again = library_backup_recovery.prepare_library_backup_settlement(
        reloaded,
        item_id,
        owner,
        album,
        album_dir,
        authority_check=lambda: None,
    )
    assert again.status is (
        library_backup_recovery.LibraryBackupResolutionStatus.READY)
    assert again.journal == resumed.journal


@pytest.mark.parametrize("change", ("changed", "extra"))
def test_capped_durable_upgrade_rejects_unplanned_partial_tree_change(
        tmp_path, monkeypatch, change):
    (
        current,
        item_id,
        owner,
        _owner_record,
        album,
        album_dir,
        backup,
    ) = _durable_upgrade_carrier(tmp_path, monkeypatch)
    real_rename = _interrupt_companion_carry_after_first_rename(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        library_backup_recovery.prepare_library_backup_settlement(
            current,
            item_id,
            owner,
            album,
            album_dir,
            authority_check=lambda: None,
        )

    if change == "changed":
        (album_dir / "booklet.pdf").write_bytes(b"changed")
    else:
        (album_dir / "extra.txt").write_bytes(b"extra")
    persisted = journal.load_queue_journal(current.operation_id).journal
    assert persisted is not None
    monkeypatch.setattr(
        backup_module, "_rename_exact_noreplace_at", real_rename)

    result = library_backup_recovery.prepare_library_backup_settlement(
        persisted,
        item_id,
        owner,
        album,
        album_dir,
        authority_check=lambda: None,
    )

    assert result.status is (
        library_backup_recovery.LibraryBackupResolutionStatus.ATTENTION)
    assert result.reason == "library-backup-companions-unsettled"
    assert _library_backup_reference(result.journal).kind == "library-backup"
    if change == "changed":
        assert (album_dir / "booklet.pdf").read_bytes() == b"changed"
    else:
        assert (album_dir / "extra.txt").read_bytes() == b"extra"
    assert backup.path.is_dir()


def test_startup_never_upgrades_legacy_settlement_to_disposal_authority(
        tmp_path, monkeypatch):
    (
        current,
        item_id,
        owner,
        owner_record,
        album,
        album_dir,
        backup,
    ) = _durable_upgrade_carrier(tmp_path, monkeypatch)
    legacy = _legacy_backup_result(backup, owner_record)
    carrier = backup_module.library_backup_record(
        legacy, expected_owner=owner_record)
    assert carrier is not None
    replacement_receipt = backup_module.capture_album_source_receipt(album_dir)
    assert replacement_receipt is not None
    settlement = journal.RecoveryReference(
        "library-backup",
        "library-backup-settlement",
        {
            "version": 1,
            "owner": owner_record,
            "carrier": carrier,
            "replacement_path": str(album_dir),
            "replacement_receipt": replacement_receipt,
        },
    )
    legacy_journal = replace(
        current,
        items=(replace(
            current.items[0],
            recovery_references=(settlement,),
        ),),
    )
    upgrades = []
    monkeypatch.setattr(
        journal,
        "upgrade_library_backup_settlement",
        lambda *_args, **_kwargs: upgrades.append(True) or legacy_journal,
    )

    result = library_backup_recovery.prepare_library_backup_settlement(
        legacy_journal,
        item_id,
        owner,
        album,
        album_dir,
        authority_check=lambda: None,
    )

    assert result.status is (
        library_backup_recovery.LibraryBackupResolutionStatus.ATTENTION)
    assert result.reason == "library-backup-disposal-unsettled"
    assert upgrades == []
    assert legacy.path.is_dir()
