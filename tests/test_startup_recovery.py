"""Restart recovery settles only work backed by live, exact evidence."""

import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.completion import (
    AlbumInventory,
    CompletionEvidence,
    CompletionExpectation,
    CompletionInput,
    CompletionOrigin,
    CompletionOriginKind,
    CompletionScope,
    DownloadCounts,
    LandingReceipt,
    ManagedMapping,
    QualityTarget,
    RecoveryOwner,
    SourceLineage,
    StagedReceipt,
    TrackQuality,
)
from qobuz_librarian.integrations import beets
from qobuz_librarian.queue import journal, startup_recovery
from qobuz_librarian.queue.builder import _build_queue_item


def test_post_import_relocation_recovery_precedes_queue_recovery(
    tmp_path,
    monkeypatch,
    caplog,
):
    affected_paths = (
        tmp_path / "music" / "Artist One" / "Album One",
        tmp_path / "music" / "Artist Two" / "Album Two",
    )
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    authority = run_lock.acquire()
    try:
        def needs_attention(
            *,
            authority: run_lock.RunLockLease,
            handoff_matches,
        ):
            assert authority.intact() is True
            assert callable(handoff_matches)
            return SimpleNamespace(
                status=startup_recovery.RelocationRecoveryStatus.ATTENTION_REQUIRED,
                reason="exact relocation evidence changed",
                paths=affected_paths,
            )

        monkeypatch.setattr(
            startup_recovery,
            "reconcile_post_import_relocations",
            needs_attention,
        )
        monkeypatch.setattr(
            startup_recovery,
            "_load_namespace",
            lambda _authority: pytest.fail(
                "queue recovery ran before relocation attention was settled"
            ),
        )
        blocked = startup_recovery.recover_startup_state(authority=authority)
    finally:
        authority.close()

    assert blocked.status is startup_recovery.StartupRecoveryStatus.ATTENTION_REQUIRED
    assert blocked.reason == "post-import-relocation-unsettled"
    assert blocked.post_import_relocation.reason == "exact relocation evidence changed"
    assert blocked.post_import_relocation.paths == affected_paths
    recovery_log = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(
            "Post-import folder-move recovery needs attention"
        )
    )
    assert "exact relocation evidence changed" in recovery_log
    assert "Paths needing attention" in recovery_log
    assert all(str(path) in recovery_log for path in affected_paths)


def test_restart_pins_a_gap_fill_carrier_before_queue_work_can_resume(
    tmp_path,
    monkeypatch,
):
    from qobuz_librarian.library.backup import (
        backup_gap_fill_files,
        library_backup_record,
    )
    from qobuz_librarian.queue.durable_album import (
        initial_completion_input,
        plan_durable_new_album,
    )

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01.flac"
    owned.write_bytes(b"owned original")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")

    tracks = [
        {"id": str(number), "media_number": 1, "track_number": number}
        for number in range(1, 6)
    ]
    queued = _build_queue_item(
        album={
            "id": "1",
            "title": "Album",
            "maximum_bit_depth": 24,
            "maximum_sampling_rate": 96,
            "tracks": {"items": tracks},
        },
        album_dir=album_dir,
        label="Album",
        missing=tracks[1:],
        present=tracks[:1],
        upgrade_only=False,
        auto_upgrade=False,
    )
    current = journal.save_queue_journal(
        journal.create_queue_journal([queued], mode="album")
    )
    item_id = current.items[0].item_id
    owner = RecoveryOwner(current.operation_id, item_id)
    owner_record = {
        "operation_id": owner.operation_id,
        "item_id": owner.item_id,
    }
    plan = plan_durable_new_album(
        queued,
        SimpleNamespace(no_import=False, no_downsample=True),
    )
    assert plan is not None
    current = journal.transition_journal_item(
        current,
        item_id,
        journal.QueuePhase.ACTIVE,
        completion_input=initial_completion_input(
            plan,
            owner,
            CompletionOrigin(
                CompletionOriginKind.CLI,
                "test-gap-fill-restart",
            ),
        ),
    )

    def commit_intent(record):
        nonlocal current
        current = journal.append_library_backup_intent(
            current,
            item_id,
            record,
        )
        assert owned.exists()
        assert current.items[0].recovery_references[0].kind == (
            "library-backup-intent"
        )

    backup = backup_gap_fill_files(
        [owned],
        album_dir,
        owner=owner_record,
        on_intent=commit_intent,
    )
    assert backup is not None and backup.complete and not owned.exists()
    intent = current.items[0].recovery_references[0]
    carrier = library_backup_record(backup, expected_owner=owner_record)
    assert carrier is not None
    current = journal.promote_library_backup_carrier(
        current,
        item_id,
        intent,
        carrier,
    )

    authority = run_lock.acquire()
    try:
        result = startup_recovery.recover_startup_state(authority=authority)
    finally:
        authority.close()

    assert result.status is startup_recovery.StartupRecoveryStatus.ATTENTION_REQUIRED
    loaded = journal.load_queue_journal(current.operation_id).journal
    assert loaded is not None
    blocked = loaded.items[0]
    assert blocked.phase is journal.QueuePhase.BLOCKED
    assert blocked.block_reason == "library-backup-carrier-awaiting-recovery"
    assert tuple(ref.kind for ref in blocked.recovery_references) == (
        "library-backup",
    )
    assert (backup.path / ".ql_upgrade_unverified").is_file()


@pytest.mark.parametrize(
    (
        "crash_point",
        "change_replacement",
        "ambiguous",
        "persisted_disposal",
        "expected_state",
    ),
    (
        ("before-move", False, False, True, "none"),
        ("before-delete", True, False, True, "restored"),
        ("after-unlink", False, False, True, "disposed"),
        ("after-unlink", False, True, True, "attention"),
        ("before-delete", False, False, False, "visible"),
    ),
)
def test_restart_reconciles_exact_backup_disposal_quarantine(
    tmp_path,
    monkeypatch,
    crash_point,
    change_replacement,
    ambiguous,
    persisted_disposal,
    expected_state,
):
    import qobuz_librarian.library.backup as backup_module

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    original = album / "01.flac"
    original.write_bytes(b"original")
    backups = tmp_path / "backups"
    monkeypatch.setattr(backup_module.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(backup_module.cfg, "UPGRADE_BACKUP_DIR", backups)
    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    backup = backup_module.backup_gap_fill_files(
        [original],
        album,
        owner=owner,
        on_intent=lambda _record: None,
    )
    assert backup is not None and backup.complete
    carrier = backup_module.library_backup_record(
        backup, expected_owner=owner)
    disposal = backup_module.library_backup_disposal_record(
        backup, expected_owner=owner)
    assert carrier is not None and disposal is not None
    assert backup_module.pin_unverified_upgrade_backup(
        backup,
        "kept after settlement",
        expected_owner=owner,
    )

    replacement = album / "01.flac"
    replacement.write_bytes(b"replacement")
    replacement_receipt = backup_module.capture_album_source_receipt(album)
    assert replacement_receipt is not None
    settlement = journal.RecoveryReference(
        "library-backup",
        "library-backup-settlement",
        {
            "version": 2,
            "owner": owner,
            "carrier": carrier,
            "replacement_path": str(album),
            "replacement_receipt": replacement_receipt,
            "disposal": disposal,
        },
    )
    assert journal._strict_library_backup_settlement(
        settlement,
        expected_operation_id=owner["operation_id"],
        expected_item_id=owner["item_id"],
    )

    child = os.fork()
    if child == 0:
        if crash_point == "before-move":
            backup_module._rename_exact_noreplace_at = (
                lambda *_args, **_kwargs: os._exit(91)
            )
        elif crash_point == "before-delete":
            backup_module._delete_exact_tree_contents = (
                lambda *_args, **_kwargs: os._exit(91)
            )
        else:
            original_unlink = backup_module._unlink_exact_at

            def crash_after_unlink(*args, **kwargs):
                original_unlink(*args, **kwargs)
                os._exit(91)

            backup_module._unlink_exact_at = crash_after_unlink
        backup_module.dispose_backup(
            backup,
            replacement_path=album,
            expected_replacement_receipt=replacement_receipt,
            replacement_validator=lambda *_paths: True,
            expected_owner=owner,
            expected_disposal_record=(
                disposal if persisted_disposal else None
            ),
        )
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    quarantine = backups / disposal["quarantine_name"]
    if crash_point == "before-move":
        assert backup.path.is_dir()
        assert quarantine.is_dir()
        assert not tuple(quarantine.iterdir())
    else:
        assert not backup.path.exists()
        assert (quarantine / "held").is_dir()

    if expected_state == "visible":
        backup_module._only_copy_cache = None
        assert (quarantine, album) in backup_module.find_only_copy_backups()
        return

    if change_replacement:
        replacement.write_bytes(b"changed replacement")
    if ambiguous:
        (quarantine / "held" / "unexpected").write_bytes(b"not owned")
    reconciled = backup_module.reconcile_library_backup_disposal(
        carrier,
        disposal,
        replacement_path=album,
        expected_replacement_receipt=replacement_receipt,
        expected_owner=owner,
    )

    assert reconciled.state == expected_state
    if expected_state == "none":
        assert not quarantine.exists()
        assert (backup.path / "01.flac").read_bytes() == b"original"
        assert replacement.read_bytes() == b"replacement"
    elif expected_state == "attention":
        assert quarantine.is_dir()
        assert (quarantine / "held" / "unexpected").read_bytes() == b"not owned"
        assert not backup.path.exists()
        assert replacement.read_bytes() == b"replacement"
    elif expected_state == "restored":
        assert not quarantine.exists()
        assert (backup.path / "01.flac").read_bytes() == b"original"
        assert replacement.read_bytes() == b"changed replacement"
    else:
        assert not quarantine.exists()
        assert not backup.path.exists()
        assert replacement.read_bytes() == b"replacement"


def test_startup_recovery_is_fail_closed_and_settles_exact_work(
    tmp_path,
    monkeypatch,
):
    journals_dir = tmp_path / "queue-journals"
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", journals_dir)
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets" / "library.db")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))

    identifiers = iter(f"{number:064x}" for number in range(1, 20))
    monkeypatch.setattr(journal, "_new_id", lambda: next(identifiers))

    def queue_item(title):
        return _build_queue_item(
            album={"id": "1", "title": title},
            album_dir=Path(f"/music/{title.lower()}"),
            label=title,
            missing=[],
            present=[],
            upgrade_only=False,
            auto_upgrade=False,
        )

    saved = journal.save_queue_journal(
        journal.create_queue_journal(
            [
                queue_item("Retirement"),
                queue_item("Complete"),
                queue_item("Sealed"),
                queue_item("Unsealed"),
                queue_item("Download incomplete"),
            ],
            mode="album",
        )
    )
    (
        retirement_id,
        complete_id,
        sealed_id,
        unsealed_id,
        download_incomplete_id,
    ) = (item.item_id for item in saved.items)

    def completion_input(item_id, *, ready):
        slot = "qobuz:track-1"
        return CompletionInput(
            owner=RecoveryOwner(saved.operation_id, item_id),
            origin=CompletionOrigin(CompletionOriginKind.CLI, "test-recovery"),
            expectation=CompletionExpectation(
                album_id="1",
                scope=CompletionScope.ALBUM,
                catalogue_slots=(slot,),
                requested_slots=(slot,),
                quality_targets=(QualityTarget(slot, 16, 44_100),),
            ),
            effective_tier=2,
            lineages=(
                SourceLineage(
                    slot=slot,
                    origin=StagedReceipt(
                        path=f"/staging/{item_id}.flac",
                        identity=(1, int(item_id, 16), 3, 4, 5, 6),
                    ),
                ),
            )
            if ready
            else (),
            counts=DownloadCounts() if ready else None,
        )

    def completion_evidence(item_id):
        frozen = completion_input(item_id, ready=True)
        lineage = frozen.lineages[0]
        source = lineage.current
        destination = (2, int(item_id, 16), 100, 1_000, 3_000)
        album_identity = (2, int(item_id, 16) + 100, 0, 900, 2_900)
        destination_path = f"Artist/Album/{item_id[-2:]}.flac"
        landing = LandingReceipt(
            lineage.slot,
            destination_path,
            destination,
            "d" * 64,
        )
        return CompletionEvidence(
            owner=frozen.owner,
            expectation=frozen.expectation,
            library_root="/music",
            library_root_identity=(2, 10, 0, 1_000, 2_000),
            album_path="Artist/Album",
            album_identity=album_identity,
            managed_manifest_hash="c" * 64,
            imports=(
                ManagedMapping(
                    lineage.slot,
                    source.path,
                    source.identity,
                    destination_path,
                    destination,
                ),
            ),
            landings=(landing,),
            baseline=(),
            inventory=AlbumInventory(
                path="Artist/Album",
                identity=album_identity,
                audio=(landing,),
            ),
            quality=(
                TrackQuality(
                    slot=lineage.slot,
                    path=destination_path,
                    identity=destination,
                    sha256=landing.sha256,
                    served_bits=16,
                    served_rate=44_100,
                ),
            ),
            counts=DownloadCounts(),
        )

    def managed_reference(item_id):
        return journal.RecoveryReference(
            name="managed-import",
            kind="managed-beets",
            data={
                "version": 1,
                "path": f"/private/{item_id}.jsonl",
                "device": 1,
                "inode": int(item_id, 16),
                "parent_device": 3,
                "parent_inode": 4,
                "nonce": "d" * 64,
                "owner": {
                    "operation_id": saved.operation_id,
                    "item_id": item_id,
                },
            },
        )

    ready_inputs = {
        item_id: completion_input(item_id, ready=True)
        for item_id in (
            retirement_id,
            complete_id,
            sealed_id,
            unsealed_id,
        )
    }
    evidence = {
        item_id: completion_evidence(item_id) for item_id in (retirement_id, complete_id, sealed_id)
    }

    def make_resolving(current, item_id):
        current = journal.transition_journal_item(
            current,
            item_id,
            journal.QueuePhase.ACTIVE,
            completion_input=ready_inputs[item_id],
        )
        return journal.transition_journal_item(
            current,
            item_id,
            journal.QueuePhase.RESOLVING,
            recovery_references=(managed_reference(item_id),),
        )

    saved = make_resolving(saved, retirement_id)
    saved = journal.transition_journal_item(
        saved,
        retirement_id,
        journal.QueuePhase.COMPLETE,
        completion_evidence=evidence[retirement_id].to_record(),
    )
    saved = journal.commit_recovered_completed_item_removal(
        saved,
        item_id=retirement_id,
        live_evidence=evidence[retirement_id],
    )

    saved = make_resolving(saved, complete_id)
    saved = journal.transition_journal_item(
        saved,
        complete_id,
        journal.QueuePhase.COMPLETE,
        completion_evidence=evidence[complete_id].to_record(),
    )
    saved = make_resolving(saved, sealed_id)
    saved = make_resolving(saved, unsealed_id)
    saved = journal.transition_journal_item(
        saved,
        download_incomplete_id,
        journal.QueuePhase.ACTIVE,
        completion_input=completion_input(download_incomplete_id, ready=False),
    )

    events = []

    class ManagedLease:
        def __init__(self, owner):
            self.owner = owner

        def revalidate(self):
            return SimpleNamespace(manifest_hash="c" * 64)

    class LiveLease:
        def __init__(self, item_id):
            self.item_id = item_id

        def revalidate(self):
            events.append(("live-proof", self.item_id))
            return evidence[self.item_id]

    @contextmanager
    def reopen(reference, owner):
        assert reference["owner"] == {
            "operation_id": owner.operation_id,
            "item_id": owner.item_id,
        }
        events.append(("managed-open", owner.item_id))
        try:
            yield ManagedLease(owner)
        finally:
            events.append(("managed-close", owner.item_id))

    @contextmanager
    def capture(*, managed_lease, owner, expectation, download):
        frozen = ready_inputs[owner.item_id]
        assert managed_lease.owner == owner
        assert expectation == frozen.expectation
        assert download == frozen.download_coverage()
        events.append(("live-open", owner.item_id))
        try:
            yield LiveLease(owner.item_id)
        finally:
            events.append(("live-close", owner.item_id))

    def inspect(reference, owner):
        assert reference["owner"]["item_id"] == owner.item_id
        events.append(("inspect", owner.item_id))
        if owner.item_id == sealed_id:
            return beets.ManagedCarrierInspectionResult(
                beets.ManagedCarrierInspectionOutcome.SEALED,
                "c" * 64,
            )
        return beets.ManagedCarrierInspectionResult(
            beets.ManagedCarrierInspectionOutcome.UNSEALED_ACTIVITY,
        )

    def retire(_reference, owner, _manifest_hash, _quarantine_name):
        events.append(("retire", owner.item_id))
        return beets.ManagedCarrierRetirementResult(
            beets.ManagedCarrierRetirementOutcome.RETIRED,
        )

    expected_plans = {item.item_id: item.planned for item in saved.items}
    acknowledged = []

    def acknowledge_completion(
        origin,
        owner,
        *,
        album_id,
        completion_hash,
        planned,
        post_dir,
    ):
        frozen = ready_inputs[owner.item_id]
        assert origin == frozen.origin
        assert owner == frozen.owner
        assert album_id == frozen.expectation.album_id
        assert len(completion_hash) == 64
        assert planned == expected_plans[owner.item_id]
        assert post_dir == "/music/Artist/Album"
        acknowledged.append(owner.item_id)
        return True

    monkeypatch.setattr(startup_recovery, "reopen_managed_evidence", reopen)
    monkeypatch.setattr(startup_recovery, "capture_new_album_completion", capture)
    monkeypatch.setattr(startup_recovery, "inspect_managed_carrier", inspect)
    monkeypatch.setattr(beets, "retire_managed_carrier", retire)

    authority = run_lock.acquire()
    assert authority is not None
    try:
        journal_path = journals_dir / f"{saved.operation_id}.json"
        residue = journals_dir / f".{saved.operation_id}.interrupted.tmp"
        residue.write_bytes(journal_path.read_bytes())
        before = journal_path.read_bytes(), residue.read_bytes()

        blocked = startup_recovery.recover_startup_state(authority=authority)

        assert blocked.status is startup_recovery.StartupRecoveryStatus.ATTENTION_REQUIRED
        assert blocked.reason == "queue-namespace-blocked"
        assert events == []
        assert (journal_path.read_bytes(), residue.read_bytes()) == before

        residue.unlink()
        complete_before = next(
            item
            for item in journal.load_queue_journal(saved.operation_id).journal.items
            if item.item_id == complete_id
        )
        deferred = startup_recovery.recover_startup_state(authority=authority)
        complete_after = next(
            item
            for item in journal.load_queue_journal(saved.operation_id).journal.items
            if item.item_id == complete_id
        )
        assert deferred.status is startup_recovery.StartupRecoveryStatus.RESUME_REQUIRED
        assert complete_after == complete_before
        assert next(item for item in deferred.items if item.item_id == complete_id).action is (
            startup_recovery.StartupRecoveryAction.FINALISE_COMPLETION
        )

        recovered = startup_recovery.recover_startup_state(
            authority=authority,
            acknowledge_completion=acknowledge_completion,
        )
    finally:
        authority.close()

    assert recovered.status is startup_recovery.StartupRecoveryStatus.ATTENTION_REQUIRED
    assert recovered.reason == "queue-item-blocked"
    assert events[0] == ("retire", retirement_id)
    assert ("live-proof", complete_id) in events
    assert ("live-proof", sealed_id) in events
    assert events.index(("live-close", complete_id)) < events.index(("retire", complete_id))
    assert events.index(("live-close", sealed_id)) < events.index(("retire", sealed_id))
    assert acknowledged == [complete_id, sealed_id]

    loaded = journal.load_queue_journal(saved.operation_id)
    assert loaded.status is journal.QueueLoadStatus.READY
    final = loaded.journal
    assert final is not None
    assert final.retirements == ()
    remaining = {item.item_id: item for item in final.items}
    assert set(remaining) == {
        unsealed_id,
        download_incomplete_id,
    }
    unsealed = remaining[unsealed_id]
    assert unsealed.phase is journal.QueuePhase.BLOCKED
    assert unsealed.block_reason == "managed-carrier-unsealed-activity"
    assert unsealed.recovery_references == (managed_reference(unsealed_id),)
    assert unsealed.completion_input == ready_inputs[unsealed_id].to_record()

    reported = {item.item_id: item for item in recovered.items}
    assert (
        reported[download_incomplete_id].action
        is startup_recovery.StartupRecoveryAction.RESUME_DOWNLOAD
    )
    assert remaining[download_incomplete_id].phase is journal.QueuePhase.ACTIVE


