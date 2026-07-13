import json
from dataclasses import replace
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    AlbumInventory,
    CompletionExpectation,
    CompletionInput,
    CompletionOrigin,
    CompletionOriginKind,
    CompletionScope,
    DownloadCounts,
    LandingReceipt,
    ManagedImportEvidence,
    ManagedMapping,
    QualityTarget,
    RecoveryOwner,
    SourceLineage,
    StagedReceipt,
    TrackQuality,
    assess_completion,
)
from qobuz_librarian.queue import journal as journal_module
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.persistence import (
    QueueJournalBlocked,
    QueueLoadStatus,
    QueuePhase,
    RecoveryReference,
    clear_queue_journal,
    commit_completed_item_removal,
    commit_recovered_completed_item_removal,
    create_queue_journal,
    list_queue_journals,
    load_pending_queue,
    load_queue_journal,
    process_carrier_retirement,
    save_queue_journal,
    transition_journal_item,
)


def _queue_item(title="Test"):
    return _build_queue_item(
        album={"id": "1", "title": title},
        album_dir=Path(f"/music/{title.lower()}"),
        label=title,
        missing=[],
        present=[],
        upgrade_only=False,
        auto_upgrade=False,
    )


def _completion_input(journal, item_id, *, complete=False):
    slots = ("qobuz:track-1",)
    lineages = ()
    counts = None
    if complete:
        lineages = (SourceLineage(
            slot=slots[0],
            origin=StagedReceipt(
                path=f"/staging/{item_id}.flac",
                identity=(1, 2, 3, 4, 5, 6),
            ),
        ),)
        counts = DownloadCounts()
    return CompletionInput(
        owner=RecoveryOwner(journal.operation_id, item_id),
        origin=CompletionOrigin(CompletionOriginKind.CLI, "test-queue"),
        expectation=CompletionExpectation(
            album_id="1",
            scope=CompletionScope.ALBUM,
            catalogue_slots=slots,
            requested_slots=slots,
            quality_targets=(QualityTarget(slots[0], 16, 44_100),),
        ),
        effective_tier=2,
        lineages=lineages,
        counts=counts,
    )


def _completion_evidence(journal, item_id):
    completion_input = _completion_input(journal, item_id, complete=True)
    lineage = completion_input.lineages[0]
    source = lineage.current
    destination = (2, 111, 100, 1_000, 3_000)
    album_identity = (2, 99, 0, 900, 2_900)
    destination_path = "Artist/Album/01 Test.flac"
    landing = LandingReceipt(
        lineage.slot,
        destination_path,
        destination,
        "d" * 64,
    )
    assessment = assess_completion(
        owner=completion_input.owner,
        expectation=completion_input.expectation,
        download=completion_input.download_coverage(),
        managed=ManagedImportEvidence(
            owner=completion_input.owner,
            library_root="/music",
            library_root_identity=(2, 10, 0, 1_000, 2_000),
            album_path="Artist/Album",
            album_identity=album_identity,
            manifest_hash="c" * 64,
            mappings=(ManagedMapping(
                lineage.slot,
                source.path,
                source.identity,
                destination_path,
                destination,
            ),),
        ),
        landings=(landing,),
        inventory=AlbumInventory(
            path="Artist/Album",
            identity=album_identity,
            audio=(landing,),
        ),
        quality=(TrackQuality(
            slot=lineage.slot,
            path=destination_path,
            identity=destination,
            sha256=landing.sha256,
            served_bits=16,
            served_rate=44_100,
        ),),
    )
    assert assessment.evidence is not None
    return assessment.evidence


def _managed_reference(journal, item_id):
    return RecoveryReference(
        name="managed-import",
        kind="managed-beets",
        data={
            "version": 1,
            "path": "/private/managed-carrier.jsonl",
            "device": 1,
            "inode": 2,
            "parent_device": 3,
            "parent_inode": 4,
            "nonce": "d" * 64,
            "owner": {
                "operation_id": journal.operation_id,
                "item_id": item_id,
            },
        },
    )


def _staging_run_record(journal, item_id, path=None):
    if path is None:
        path = str(Path(cfg.STAGING_DIR) / f".qobuz-run-{'a' * 24}")
    return {
        "version": 2,
        "path": path,
        "root_identity": [1, 2, 0o40700, 3, 4, 5],
        "owner": {
            "operation_id": journal.operation_id,
            "item_id": item_id,
        },
    }


def _staging_group_record(journal, item_id, path=None):
    if path is None:
        path = str(Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR / "owned")
    return {
        "version": 2,
        "kind": "interrupted",
        "path": path,
        "owner": {
            "operation_id": journal.operation_id,
            "item_id": item_id,
        },
        "sealed_manifest": {
            "identity": [1, 9, 0o100600, 10, 11, 12],
            "sha256": "a" * 64,
        },
    }


@pytest.fixture
def queue_paths(tmp_path, monkeypatch):
    legacy = tmp_path / ".qobuz_pending_queue.json"
    journals = tmp_path / ".qobuz_queue_journals"
    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", legacy)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", journals)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    return legacy, journals


def test_crash_left_publication_temp_blocks_the_whole_namespace(queue_paths):
    _legacy, journals = queue_paths
    saved = save_queue_journal(create_queue_journal(
        [_queue_item("Interrupted")],
        mode="album",
    ))
    journal_path = journals / f"{saved.operation_id}.json"
    temporary = journals / f".{saved.operation_id}.interrupted.tmp"
    temporary.write_bytes(journal_path.read_bytes())
    before = (journal_path.read_bytes(), temporary.read_bytes())

    listed = list_queue_journals()
    assert len(listed) == 1
    assert listed[0].status is QueueLoadStatus.BLOCKED
    assert listed[0].paths == (temporary,)
    assert load_queue_journal(saved.operation_id).status is QueueLoadStatus.BLOCKED
    with pytest.raises(QueueJournalBlocked, match="unrecognised persisted state"):
        transition_journal_item(
            saved,
            saved.items[0].item_id,
            QueuePhase.ACTIVE,
            completion_input=_completion_input(saved, saved.items[0].item_id),
        )
    assert (journal_path.read_bytes(), temporary.read_bytes()) == before


def test_blocked_operation_prevents_legacy_queue_migration(queue_paths):
    legacy, journals = queue_paths
    saved = save_queue_journal(create_queue_journal(
        [_queue_item("Needs attention")],
        mode="album",
    ))
    blocked = transition_journal_item(
        saved,
        saved.items[0].item_id,
        QueuePhase.BLOCKED,
        block_reason="manual recovery required",
    )
    legacy.write_text(json.dumps({
        "version": 1,
        "saved_at": "2026-07-12T08:00:00+00:00",
        "mode": "walk_queue",
        "count": 1,
        "items": [journal_module._serialize_queue_item(_queue_item("Legacy"))],
    }), encoding="utf-8")
    before = (
        legacy.read_bytes(),
        (journals / f"{blocked.operation_id}.json").read_bytes(),
    )

    listed = list_queue_journals()
    assert len(listed) == 1
    assert listed[0].status is QueueLoadStatus.BLOCKED
    assert legacy in listed[0].paths
    assert journals / f"{blocked.operation_id}.json" in listed[0].paths
    assert len(list(journals.glob("*.json"))) == 1
    assert (
        legacy.read_bytes(),
        (journals / f"{blocked.operation_id}.json").read_bytes(),
    ) == before


def test_durability_failures_leave_last_committed_snapshot(
    queue_paths, monkeypatch
):
    _legacy, base_journals = queue_paths
    journals = base_journals.parent / "new-data" / "state" / "journals"
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", journals)
    synced = []
    real_fsync_directory = journal_module._fsync_directory
    durability = {
        "ancestor_failed": False,
        "post_replace": False,
        "post_replace_failed": False,
        "replaced": False,
    }

    def controlled_fsync(path):
        if path == journals.parent.parent.parent and not durability["ancestor_failed"]:
            durability["ancestor_failed"] = True
            raise OSError("ancestor relationship not synced")
        if (
            path == journals
            and durability["post_replace"]
            and durability["replaced"]
            and not durability["post_replace_failed"]
        ):
            durability["post_replace_failed"] = True
            raise OSError("replacement namespace not synced")
        real_fsync_directory(path)
        synced.append(path)

    monkeypatch.setattr(journal_module, "_fsync_directory", controlled_fsync)
    uncommitted = create_queue_journal(
        [_queue_item("First"), _queue_item("Second")],
        mode="album",
    )
    with pytest.raises(OSError, match="ancestor relationship"):
        save_queue_journal(uncommitted)

    assert not list(journals.glob("*.json"))

    real_replace = journal_module.os.replace

    def mark_replace(source, destination):
        real_replace(source, destination)
        durability["replaced"] = True

    durability["post_replace"] = True
    monkeypatch.setattr(journal_module.os, "replace", mark_replace)
    with pytest.raises(OSError, match="replacement namespace"):
        save_queue_journal(uncommitted)

    recovered = load_queue_journal(uncommitted.operation_id)
    pending = recovered.journal

    assert recovered.status is QueueLoadStatus.READY
    assert pending.generation == 1
    assert {
        journals.parent.parent.parent,
        journals.parent.parent,
        journals.parent,
        journals,
    }.issubset(synced)
    current = transition_journal_item(
        pending,
        pending.items[0].item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(pending, pending.items[0].item_id),
    )
    reloaded = load_queue_journal(pending.operation_id)
    assert reloaded.journal.generation == 2
    assert [item.phase for item in reloaded.journal.items] == [
        QueuePhase.ACTIVE,
        QueuePhase.PENDING,
    ]

    def fail_write(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(journal_module, "_write_durable_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        transition_journal_item(
            current,
            current.items[1].item_id,
            QueuePhase.ACTIVE,
            completion_input=_completion_input(
                current, current.items[1].item_id),
        )

    assert current.items[1].phase is QueuePhase.PENDING
    assert load_queue_journal(current.operation_id).journal == reloaded.journal


def test_malformed_v2_blocks_replay_and_requires_explicit_discard(queue_paths):
    _legacy, journals = queue_paths
    operation_id = "a" * 64
    path = journals / f"{operation_id}.json"
    journals.mkdir()
    raw = b'{"version":2,"items":[{}]}'
    path.write_bytes(raw)

    lock_target = journals.parent / "wrong-lock-target"
    lock_target.write_text("do not lock this", encoding="utf-8")
    lock_path = journals / journal_module._JOURNAL_LOCK_NAME
    lock_path.symlink_to(lock_target)
    unsafe_lock = load_queue_journal(operation_id)
    assert unsafe_lock.status is QueueLoadStatus.BLOCKED
    assert lock_path.is_symlink()
    lock_path.unlink()

    loaded = load_queue_journal(operation_id)
    assert loaded.status is QueueLoadStatus.BLOCKED
    assert path.read_bytes() == raw
    with pytest.raises(QueueJournalBlocked):
        clear_queue_journal(operation_id, explicit_discard=True)
    assert path.read_bytes() == raw


def test_managed_carrier_reservation_is_promoted_exactly(queue_paths, monkeypatch):
    _legacy, journals = queue_paths
    config_dir = journals.parent / "beets"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", config_dir / "library.db")
    pending = save_queue_journal(create_queue_journal([_queue_item()], mode="web"))
    item_id = pending.items[0].item_id
    completion_input = _completion_input(pending, item_id, complete=True)
    active = transition_journal_item(
        pending,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=completion_input,
    )
    owner = {
        "operation_id": active.operation_id,
        "item_id": item_id,
    }
    nonce = "a" * 64
    carrier_path = config_dir / f".qobuz-managed-beets-{nonce}.jsonl"
    reservation = RecoveryReference(
        name="managed-import",
        kind="managed-beets-reservation",
        data={
            "version": 1,
            "path": str(carrier_path),
            "parent_device": 11,
            "parent_inode": 12,
            "nonce": nonce,
            "owner": owner,
        },
    )
    carrier = RecoveryReference(
        name="managed-import",
        kind="managed-beets",
        data={
            "version": 2,
            "path": str(carrier_path),
            "device": 21,
            "inode": 22,
            "parent_device": 11,
            "parent_inode": 12,
            "nonce": nonce,
            "owner": owner,
        },
    )

    with pytest.raises(QueueJournalBlocked, match="promoted from its reservation"):
        transition_journal_item(
            active,
            item_id,
            QueuePhase.RESOLVING,
            recovery_references=(carrier,),
        )
    reserved = journal_module.reserve_managed_carrier(
        active,
        item_id,
        reservation,
    )
    loaded = load_queue_journal(reserved.operation_id)
    assert loaded.status is QueueLoadStatus.READY
    assert loaded.journal.items[0].recovery_references == (reservation,)

    blocked = transition_journal_item(
        loaded.journal,
        item_id,
        QueuePhase.BLOCKED,
        block_reason="restart before carrier creation",
    )
    other_nonce = "b" * 64
    other_reservation = replace(
        reservation,
        data={
            **reservation.data,
            "path": str(
                config_dir / f".qobuz-managed-beets-{other_nonce}.jsonl"
            ),
            "nonce": other_nonce,
        },
    )
    with pytest.raises(QueueJournalBlocked, match="preserved exactly"):
        transition_journal_item(
            blocked,
            item_id,
            QueuePhase.ACTIVE,
            recovery_references=(other_reservation,),
        )
    resumed = transition_journal_item(blocked, item_id, QueuePhase.ACTIVE)
    with pytest.raises(QueueJournalBlocked, match="does not match persisted state"):
        journal_module.promote_managed_carrier(
            resumed,
            item_id,
            other_reservation,
            carrier,
            completion_input,
        )

    resolving = journal_module.promote_managed_carrier(
        resumed,
        item_id,
        reservation,
        carrier,
        completion_input,
    )
    reloaded = load_queue_journal(resolving.operation_id)
    assert reloaded.status is QueueLoadStatus.READY
    assert reloaded.journal.items[0].phase is QueuePhase.RESOLVING
    assert reloaded.journal.items[0].recovery_references == (carrier,)


def test_staging_references_are_owned_unique_and_canonically_ordered(queue_paths):
    pending = save_queue_journal(create_queue_journal([_queue_item()], mode="web"))
    item_id = pending.items[0].item_id
    active = transition_journal_item(
        pending,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(pending, item_id),
    )
    grouped = journal_module.append_staging_group_intent(
        active,
        item_id,
        _staging_group_record(active, item_id),
    )
    tracked = journal_module.append_staging_run_reference(
        grouped,
        item_id,
        _staging_run_record(grouped, item_id),
    )

    assert [
        reference.kind for reference in tracked.items[0].recovery_references
    ] == ["download-staging-run", "staging-group"]
    duplicate = journal_module.append_staging_run_reference(
        tracked,
        item_id,
        _staging_run_record(tracked, item_id),
    )
    assert duplicate.generation == tracked.generation

    with pytest.raises(QueueJournalBlocked, match="already checkpointed"):
        journal_module.append_staging_run_reference(
            tracked,
            item_id,
            {
                **_staging_run_record(tracked, item_id),
                "root_identity": [1, 99, 0o40700, 3, 4, 5],
            },
        )
    outside = Path(cfg.STAGING_DIR).parent / "outside"
    with pytest.raises(ValueError, match="malformed"):
        journal_module.append_staging_run_reference(
            tracked,
            item_id,
            _staging_run_record(
                tracked,
                item_id,
                str(outside / f".qobuz-run-{'b' * 24}"),
            ),
        )

    blocked = transition_journal_item(
        tracked,
        item_id,
        QueuePhase.BLOCKED,
        block_reason="staging requires inspection",
    )
    resumed = transition_journal_item(blocked, item_id, QueuePhase.ACTIVE)
    assert resumed.items[0].recovery_references == tracked.items[0].recovery_references

    item_payload = journal_module._item_payload(resumed.items[0])
    item_payload["recovery_references"].reverse()
    with pytest.raises(ValueError, match="canonical ordering"):
        journal_module._parse_item(
            item_payload,
            expected_operation_id=tracked.operation_id,
        )


def test_managed_promotion_preserves_staging_until_exact_reconciliation(
    queue_paths, monkeypatch
):
    _legacy, journals = queue_paths
    config_dir = journals.parent / "beets"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", config_dir / "library.db")
    pending = save_queue_journal(create_queue_journal([_queue_item()], mode="web"))
    item_id = pending.items[0].item_id
    complete_input = _completion_input(pending, item_id, complete=True)
    active = transition_journal_item(
        pending,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=complete_input,
    )
    with_run = journal_module.append_staging_run_reference(
        active,
        item_id,
        _staging_run_record(active, item_id),
    )
    staged = journal_module.append_staging_group_intent(
        with_run,
        item_id,
        _staging_group_record(with_run, item_id),
    )
    owner = {"operation_id": staged.operation_id, "item_id": item_id}
    nonce = "b" * 64
    carrier_path = config_dir / f".qobuz-managed-beets-{nonce}.jsonl"
    reservation = RecoveryReference(
        name="managed-import",
        kind="managed-beets-reservation",
        data={
            "version": 1,
            "path": str(carrier_path),
            "parent_device": 11,
            "parent_inode": 12,
            "nonce": nonce,
            "owner": owner,
        },
    )
    carrier = RecoveryReference(
        name="managed-import",
        kind="managed-beets",
        data={
            "version": 2,
            "path": str(carrier_path),
            "device": 21,
            "inode": 22,
            "parent_device": 11,
            "parent_inode": 12,
            "nonce": nonce,
            "owner": owner,
        },
    )

    reserved = journal_module.reserve_managed_carrier(
        staged,
        item_id,
        reservation,
    )
    promoted = journal_module.promote_managed_carrier(
        reserved,
        item_id,
        reservation,
        carrier,
        complete_input,
    )
    assert [
        reference.kind for reference in promoted.items[0].recovery_references
    ] == ["download-staging-run", "staging-group", "managed-beets"]
    with pytest.raises(ValueError, match="complete journal items"):
        transition_journal_item(
            promoted,
            item_id,
            QueuePhase.COMPLETE,
            completion_evidence=_completion_evidence(promoted, item_id).to_record(),
        )

    staging_references = tuple(
        reference
        for reference in promoted.items[0].recovery_references
        if reference.kind in {"download-staging-run", "staging-group"}
    )
    reconciled = journal_module.reconcile_staging_references(
        promoted,
        item_id,
        staging_references,
    )
    assert reconciled.items[0].recovery_references == (carrier,)
    complete = transition_journal_item(
        reconciled,
        item_id,
        QueuePhase.COMPLETE,
        completion_evidence=_completion_evidence(reconciled, item_id).to_record(),
    )
    assert complete.items[0].phase is QueuePhase.COMPLETE


def test_only_truly_unstarted_active_work_can_return_to_pending(queue_paths):
    pending = save_queue_journal(create_queue_journal([_queue_item()], mode="web"))
    item_id = pending.items[0].item_id
    active = transition_journal_item(
        pending,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(pending, item_id),
    )
    reset = journal_module.reset_unstarted_item_to_pending(active, item_id)
    assert reset.items[0].phase is QueuePhase.PENDING
    assert reset.items[0].completion_input is None

    restarted = transition_journal_item(
        reset,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(reset, item_id),
    )
    tracked = journal_module.append_staging_run_reference(
        restarted,
        item_id,
        _staging_run_record(restarted, item_id),
    )
    with pytest.raises(QueueJournalBlocked, match="unstarted"):
        journal_module.reset_unstarted_item_to_pending(tracked, item_id)

def test_blocked_partial_completion_can_resume_without_replacing_input(queue_paths):
    pending = save_queue_journal(create_queue_journal([_queue_item()], mode="web"))
    item_id = pending.items[0].item_id
    frozen = _completion_input(pending, item_id)
    with pytest.raises(ValueError, match="album does not match"):
        transition_journal_item(
            pending,
            item_id,
            QueuePhase.ACTIVE,
            completion_input=replace(
                frozen,
                expectation=replace(frozen.expectation, album_id="2"),
            ),
        )
    active = transition_journal_item(
        pending,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=frozen,
    )
    with pytest.raises(QueueJournalBlocked, match="recovery-bearing"):
        clear_queue_journal(active.operation_id, explicit_discard=True)
    blocked = transition_journal_item(
        active,
        item_id,
        QueuePhase.BLOCKED,
        block_reason="retry later",
    )

    resumed = transition_journal_item(blocked, item_id, QueuePhase.ACTIVE)
    assert resumed.items[0].completion_input == frozen.to_record()
    resolving = transition_journal_item(
        resumed,
        item_id,
        QueuePhase.RESOLVING,
        completion_input=_completion_input(resumed, item_id, complete=True),
        recovery_references=(_managed_reference(resumed, item_id),),
    )
    assert resolving.items[0].phase is QueuePhase.RESOLVING


def test_legacy_migration_replays_once_and_completed_removal_commits_first(
    queue_paths, monkeypatch
):
    legacy, journals = queue_paths
    queue_item = _queue_item("Legacy")
    legacy_payload = {
        "version": 1,
        "saved_at": "2026-07-12T08:00:00+00:00",
        "mode": "walk_queue",
        "count": 1,
        "items": [journal_module._serialize_queue_item(queue_item)],
    }
    original_legacy = json.dumps(legacy_payload)
    legacy.write_text(original_legacy, encoding="utf-8")
    real_unlink_and_sync = journal_module._unlink_and_sync

    def crash_before_legacy_unlink(path):
        if path == legacy:
            raise OSError("simulated crash boundary")
        real_unlink_and_sync(path)

    monkeypatch.setattr(journal_module, "_unlink_and_sync", crash_before_legacy_unlink)
    first_load = load_pending_queue()
    assert first_load.status is QueueLoadStatus.BLOCKED
    assert legacy.exists()
    assert len(list(journals.glob("*.json"))) == 1
    v2_path = next(journals.glob("*.json"))
    marker_bytes = v2_path.read_bytes()
    marker_generation = first_load.journal.generation
    with pytest.raises(QueueJournalBlocked, match="legacy migration"):
        transition_journal_item(
            first_load.journal,
            first_load.journal.items[0].item_id,
            QueuePhase.ACTIVE,
        )
    assert legacy.read_text(encoding="utf-8") == original_legacy
    assert v2_path.read_bytes() == marker_bytes
    assert json.loads(v2_path.read_text(encoding="utf-8"))["generation"] == (
        marker_generation
    )
    blocked_listing = list_queue_journals()
    assert len(blocked_listing) == 1
    assert blocked_listing[0].status is QueueLoadStatus.BLOCKED
    assert v2_path in blocked_listing[0].paths

    monkeypatch.setattr(journal_module, "_unlink_and_sync", real_unlink_and_sync)
    migrated = load_pending_queue()
    caller_items, mode, _saved_at = migrated
    assert migrated.status is QueueLoadStatus.READY
    assert mode == "walk_queue"
    assert [item.phase for item in migrated.journal.items] == [QueuePhase.PENDING]
    assert migrated.journal.legacy_source_sha256 is None
    assert not legacy.exists()
    assert len(list(journals.glob("*.json"))) == 1

    item_id = migrated.journal.items[0].item_id
    active = transition_journal_item(
        migrated.journal,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(migrated.journal, item_id),
    )
    resolved_carrier = _managed_reference(migrated.journal, item_id)
    resolving = transition_journal_item(
        active,
        item_id,
        QueuePhase.RESOLVING,
        completion_input=_completion_input(active, item_id, complete=True),
        recovery_references=(resolved_carrier,),
    )
    with pytest.raises(QueueJournalBlocked, match="preserved exactly"):
        transition_journal_item(
            resolving,
            item_id,
            QueuePhase.COMPLETE,
            recovery_references=(),
            completion_evidence=_completion_evidence(resolving, item_id).to_record(),
        )
    live_evidence = _completion_evidence(resolving, item_id)
    completion_evidence = live_evidence.to_record()
    complete = transition_journal_item(
        resolving,
        item_id,
        QueuePhase.COMPLETE,
        completion_evidence=completion_evidence,
    )
    assert complete.items[0].recovery_references == (resolved_carrier,)
    real_write = journal_module._write_durable_json
    with pytest.raises(QueueJournalBlocked, match="live completion"):
        commit_completed_item_removal(
            complete,
            caller_items,
            item_id=item_id,
            caller_item=caller_items[0],
            live_evidence=replace(
                live_evidence,
                managed_manifest_hash="e" * 64,
            ),
        )
    assert len(caller_items) == 1
    monkeypatch.setattr(
        journal_module,
        "_write_durable_json",
        lambda _path, _payload: (_ for _ in ()).throw(OSError("commit failed")),
    )
    with pytest.raises(OSError, match="commit failed"):
        commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=live_evidence,
        )
    assert len(caller_items) == 1
    retained = load_queue_journal(complete.operation_id).journal.items[0]
    assert retained.phase is QueuePhase.COMPLETE
    assert retained.recovery_references == (resolved_carrier,)
    assert load_queue_journal(complete.operation_id).journal.retirements == ()

    monkeypatch.setattr(journal_module, "_write_durable_json", real_write)
    emptied = commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=live_evidence,
    )
    assert emptied.items == ()
    assert len(emptied.retirements) == 1
    retirement = emptied.retirements[0]
    assert retirement.item_id == item_id
    assert retirement.reference == resolved_carrier
    assert retirement.manifest_hash == "c" * 64
    assert retirement.quarantine_name.startswith(
        ".qobuz-retire-managed-beets-"
    )
    assert len(caller_items) == 1
    reloaded_retirement = load_queue_journal(emptied.operation_id)
    assert reloaded_retirement.journal.retirements == (retirement,)
    assert reloaded_retirement.as_legacy_tuple() == (None, None, None)
    with pytest.raises(
        QueueJournalBlocked,
        match="retirement cannot be discarded",
    ):
        clear_queue_journal(emptied.operation_id, explicit_discard=True)

    from qobuz_librarian.integrations import beets

    uncertain = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.UNCERTAIN,
        "directory sync was interrupted",
    )
    monkeypatch.setattr(
        beets,
        "retire_managed_carrier",
        lambda *_args: uncertain,
    )
    retained_journal, retained_result = process_carrier_retirement(
        emptied,
        item_id=item_id,
    )
    assert retained_result is uncertain
    assert retained_journal.retirements == (retirement,)
    assert load_queue_journal(emptied.operation_id).journal.retirements == (
        retirement,
    )

    absent = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
    )
    monkeypatch.setattr(
        beets,
        "retire_managed_carrier",
        lambda *_args: absent,
    )
    monkeypatch.setattr(journal_module, "_write_durable_json", real_write)
    settled, settled_result = process_carrier_retirement(
        emptied,
        item_id=item_id,
    )
    assert settled_result is absent
    assert settled.retirements == ()
    assert load_queue_journal(emptied.operation_id).journal.retirements == ()
