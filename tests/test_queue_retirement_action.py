
import hashlib
import shutil

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
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
from qobuz_librarian.library.post_import_relocation import (
    RelocationResult,
    capture_post_import_relocation_expectation,
)
from qobuz_librarian.library.release_identity import (
    ReleaseIdentity,
    read_release_identity,
)
from qobuz_librarian.queue import journal, post_import_finalizer
from qobuz_librarian.queue.builder import _build_queue_item


def _queue_item(*, album_dir=None):
    return _build_queue_item(
        album={"id": "1", "title": "Album"},
        album_dir=album_dir,
        label="Album",
        missing=[],
        present=[],
        upgrade_only=False,
        auto_upgrade=False,
    )


def _completion_input(
    saved, item_id, staging, *, complete=False, release_destination=None
):
    slot = "qobuz:track-1"
    return CompletionInput(
        owner=RecoveryOwner(saved.operation_id, item_id),
        origin=CompletionOrigin(CompletionOriginKind.CLI, "test-queue"),
        expectation=CompletionExpectation(
            album_id="1",
            scope=CompletionScope.ALBUM,
            catalogue_slots=(slot,),
            requested_slots=(slot,),
            quality_targets=(QualityTarget(slot, 16, 44_100),),
        ),
        effective_tier=2,
        release_identity=(
            ReleaseIdentity("qobuz", "1")
            if release_destination is not None else None
        ),
        placement_destination=(
            str(release_destination) if release_destination is not None else None
        ),
        lineages=(
            (SourceLineage(
                slot=slot,
                origin=StagedReceipt(
                    path=str(staging / f"{item_id}.flac"),
                    identity=(1, 2, 3, 4, 5, 6),
                ),
            ),)
            if complete
            else ()
        ),
        counts=DownloadCounts() if complete else None,
    )


def _completion_evidence(
    saved,
    item_id,
    staging,
    music,
    *,
    album_path="Artist, Other/Album",
    release_destination=None,
):
    completion_input = _completion_input(
        saved, item_id, staging, complete=True,
        release_destination=release_destination,
    )
    lineage = completion_input.lineages[0]
    if release_destination is None:
        destination_identity = (2, 111, 100, 1_000, 3_000)
        album_identity = (2, 99, 0, 900, 2_900)
        digest = "d" * 64
    else:
        audio_value = (release_destination / "01 Test.flac").stat()
        album_value = release_destination.stat()
        destination_identity = (
            audio_value.st_dev, audio_value.st_ino, audio_value.st_size,
            audio_value.st_mtime_ns, audio_value.st_ctime_ns,
        )
        album_identity = (
            album_value.st_dev, album_value.st_ino, album_value.st_size,
            album_value.st_mtime_ns, album_value.st_ctime_ns,
        )
        digest = hashlib.sha256(
            (release_destination / "01 Test.flac").read_bytes()
        ).hexdigest()
    destination_path = f"{album_path}/01 Test.flac"
    landing = LandingReceipt(
        lineage.slot,
        destination_path,
        destination_identity,
        digest,
    )
    assessment = assess_completion(
        owner=completion_input.owner,
        expectation=completion_input.expectation,
        download=completion_input.download_coverage(),
        managed=ManagedImportEvidence(
            owner=completion_input.owner,
            library_root=str(music),
            library_root_identity=(2, 10, 0, 1_000, 2_000),
            album_path=album_path,
            album_identity=album_identity,
            manifest_hash="c" * 64,
            mappings=(ManagedMapping(
                lineage.slot,
                lineage.current.path,
                lineage.current.identity,
                destination_path,
                destination_identity,
            ),),
        ),
        landings=(landing,),
        inventory=AlbumInventory(
            path=album_path,
            identity=album_identity,
            audio=(landing,),
        ),
        quality=(TrackQuality(
            slot=lineage.slot,
            path=destination_path,
            identity=destination_identity,
            sha256=landing.sha256,
            served_bits=16,
            served_rate=44_100,
        ),),
    )
    assert assessment.evidence is not None
    return assessment.evidence


def _managed_reference(saved, item_id, tmp_path):
    return journal.RecoveryReference(
        name="managed-import",
        kind="managed-beets",
        data={
            "version": 1,
            "path": str(tmp_path / "managed-carrier.jsonl"),
            "device": 1,
            "inode": 2,
            "parent_device": 3,
            "parent_inode": 4,
            "nonce": "b" * 64,
            "owner": {
                "operation_id": saved.operation_id,
                "item_id": item_id,
            },
        },
    )


@pytest.fixture
def queue_paths(tmp_path, monkeypatch):
    music = tmp_path / "music"
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    staging = tmp_path / "staging"
    source.mkdir(parents=True)
    staging.mkdir()
    (source / "01 Test.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    return tmp_path, music, source, destination, staging


def _completed_journal(
    queue_paths,
    *,
    planned_album_dir=None,
    completion_album_path="Artist, Other/Album",
    release_destination=None,
):
    tmp_path, music, _source, _destination, staging = queue_paths
    pending = journal.save_queue_journal(
        journal.create_queue_journal(
            [_queue_item(album_dir=planned_album_dir)], mode="test"
        )
    )
    item_id = pending.items[0].item_id
    active = journal.transition_journal_item(
        pending,
        item_id,
        journal.QueuePhase.ACTIVE,
        completion_input=_completion_input(
            pending, item_id, staging,
            release_destination=release_destination,
        ),
        multi_artist_filing=True,
    )
    resolving = journal.transition_journal_item(
        active,
        item_id,
        journal.QueuePhase.RESOLVING,
        completion_input=_completion_input(
            active, item_id, staging, complete=True,
            release_destination=release_destination,
        ),
        recovery_references=(_managed_reference(active, item_id, tmp_path),),
    )
    evidence = _completion_evidence(
        resolving,
        item_id,
        staging,
        music,
        album_path=completion_album_path,
        release_destination=release_destination,
    )
    complete = journal.transition_journal_item(
        resolving,
        item_id,
        journal.QueuePhase.COMPLETE,
        completion_evidence=evidence.to_record(),
    )
    return complete, item_id, evidence


def test_completed_removal_and_action_handoff_are_one_durable_state_machine(
    queue_paths, monkeypatch
):
    _tmp_path, _music, source, destination, _staging = queue_paths
    complete, item_id, evidence = _completed_journal(queue_paths)
    authority = run_lock.acquire()
    try:
        expectation = capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
    finally:
        authority.close()
    action_id = "a" * 64
    action = {
        "action_id": action_id,
        "kind": "whole-album",
        "source": str(source),
        "destination": str(destination),
        "expectation": expectation,
        "phase": "planned",
        "relocation_operation_id": None,
        "handoff_hash": None,
    }

    real_write = journal._write_durable_json
    monkeypatch.setattr(
        journal,
        "_write_durable_json",
        lambda *_args: (_ for _ in ()).throw(OSError("injected write failure")),
    )
    with pytest.raises(OSError, match="injected write failure"):
        journal.commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=evidence,
            post_import_action=action,
        )
    unchanged = journal.load_queue_journal(complete.operation_id).journal
    assert unchanged is not None
    assert len(unchanged.items) == 1
    assert unchanged.retirements == ()

    monkeypatch.setattr(journal, "_write_durable_json", real_write)
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=action,
    )
    assert retired.items == ()
    assert retired.retirements[0].action is not None
    assert retired.retirements[0].completion_acknowledged is False
    with pytest.raises(journal.QueueJournalBlocked, match="post-import"):
        journal.process_carrier_retirement(retired, item_id=item_id)

    relocation_id = "e" * 64
    handoff_hash = "f" * 64
    handoff = {
        "consumer": {
            "kind": "queue-completion",
            "queue_operation_id": retired.operation_id,
            "item_id": item_id,
            "action_id": action_id,
        },
        "hash": handoff_hash,
    }
    assert not journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    handed_off = journal.checkpoint_post_import_action_handoff(
        retired,
        item_id=item_id,
        action_id=action_id,
        operation_id=relocation_id,
        handoff_hash=handoff_hash,
    )
    assert journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    assert not journal.post_import_relocation_handoff_matches(
        "0" * 64, handoff
    )

    committed = journal.commit_post_import_action(
        handed_off,
        item_id=item_id,
        action_id=action_id,
        operation_id=relocation_id,
        handoff_hash=handoff_hash,
        final_path=destination,
    )
    assert committed.retirements[0].final_path == str(destination)
    assert journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    cleared = journal.clear_committed_post_import_action(
        committed,
        item_id=item_id,
        action_id=action_id,
    )
    assert cleared.retirements[0].action is None
    assert not journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    with pytest.raises(journal.QueueJournalBlocked, match="post-import"):
        journal.process_carrier_retirement(cleared, item_id=item_id)

    acknowledged = journal.acknowledge_carrier_retirement_completion(
        cleared,
        item_id=item_id,
    )
    from qobuz_librarian.integrations import beets

    outcome = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT
    )
    monkeypatch.setattr(beets, "retire_managed_carrier", lambda *_args: outcome)
    settled, result = journal.process_carrier_retirement(
        acknowledged,
        item_id=item_id,
    )
    assert result is outcome
    assert settled.retirements == ()


@pytest.mark.parametrize("cancel_at_commit", (False, True))
def test_real_committed_relocation_publishes_before_evidence_retirement(
    queue_paths, monkeypatch, cancel_at_commit
):
    tmp_path, _music, source, destination, _staging = queue_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")

    complete, item_id, evidence = _completed_journal(
        queue_paths, release_destination=source
    )
    authority = run_lock.acquire()
    events = []
    try:
        expectation = capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
        retired = journal.commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=evidence,
            post_import_action={
                "action_id": "a" * 64,
                "kind": "whole-album",
                "source": str(source),
                "destination": str(destination),
                "expectation": expectation,
                "phase": "planned",
                "relocation_operation_id": None,
                "handoff_hash": None,
            },
        )

        real_publish = post_import_finalizer._publish_retirement_identity
        real_ack = post_import_finalizer._acknowledge_completion

        def relocate(*_args, **_kwargs):
            destination.mkdir(parents=True)
            shutil.copy2(source / "01 Test.flac", destination / "01 Test.flac")
            return RelocationResult(
                destination,
                1,
                "d" * 64,
                {"exact": True},
                True,
                None,
            )

        monkeypatch.setattr(
            post_import_finalizer, "relocate_post_import_album", relocate
        )
        monkeypatch.setattr(
            post_import_finalizer,
            "seal_post_import_relocation_handoff",
            lambda *_args, **_kwargs: "e" * 64,
        )
        monkeypatch.setattr(
            post_import_finalizer,
            "acknowledge_post_import_relocation",
            lambda *_args, **_kwargs: None,
        )

        def publish(current, retirement, **kwargs):
            assert retirement.action.phase is journal.PostImportActionPhase.COMMITTED
            assert retirement.completion_evidence is not None
            result = real_publish(current, retirement, **kwargs)
            events.append("identity")
            return result

        def release(*args, **kwargs):
            assert read_release_identity(destination) == ReleaseIdentity("qobuz", "1")
            events.append("release")
            return None

        def acknowledge(current, retirement, callback):
            assert retirement.action is None
            events.append("acknowledge")
            return real_ack(current, retirement, callback)

        monkeypatch.setattr(
            post_import_finalizer, "_publish_retirement_identity", publish
        )
        monkeypatch.setattr(
            post_import_finalizer, "release_post_import_relocation", release
        )
        monkeypatch.setattr(post_import_finalizer, "_acknowledge_completion", acknowledge)
        from qobuz_librarian.integrations import beets

        carrier_result = beets.ManagedCarrierRetirementResult(
            beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT
        )
        monkeypatch.setattr(
            beets,
            "retire_managed_carrier",
            lambda *_args: events.append("carrier") or carrier_result,
        )

        committed_checks = 0
        seen_phases = []

        def cancel_check():
            nonlocal committed_checks
            current = journal.load_queue_journal(retired.operation_id).journal
            action = current.retirements[0].action
            if action is not None and action.phase not in seen_phases:
                seen_phases.append(action.phase)
            if action is not None and action.phase is journal.PostImportActionPhase.COMMITTED:
                committed_checks += 1
                return cancel_at_commit and committed_checks == 3
            return False

        if cancel_at_commit:
            with pytest.raises(OSError, match="cancelled"):
                post_import_finalizer.finalize_carrier_retirement(
                    retired,
                    item_id,
                    authority=authority,
                    acknowledge_completion=None,
                    cancel_check=cancel_check,
                )
            held = journal.load_queue_journal(retired.operation_id).journal
            assert held.retirements[0].action.phase is journal.PostImportActionPhase.COMMITTED
            assert held.retirements[0].completion_evidence is not None
            assert read_release_identity(destination) is None
            assert events == []
            assert committed_checks == 3
            assert seen_phases == [
                journal.PostImportActionPhase.PLANNED,
                journal.PostImportActionPhase.HANDOFF,
                journal.PostImportActionPhase.COMMITTED,
            ]
            post_import_finalizer.finalize_carrier_retirement(
                held,
                item_id,
                authority=authority,
                acknowledge_completion=None,
            )
        else:
            settled, final_path, result = (
                post_import_finalizer.finalize_carrier_retirement(
                    retired,
                    item_id,
                    authority=authority,
                    acknowledge_completion=None,
                    cancel_check=cancel_check,
                )
            )
            assert final_path == destination
            assert result is carrier_result
            assert settled.retirements == ()
            assert events == ["identity", "release", "acknowledge", "carrier"]
            assert seen_phases == [
                journal.PostImportActionPhase.PLANNED,
                journal.PostImportActionPhase.HANDOFF,
                journal.PostImportActionPhase.COMMITTED,
            ]
    finally:
        authority.close()


def test_conflicting_planned_action_settles_as_an_exact_noop(
    queue_paths, monkeypatch
):
    tmp_path, _music, source, destination, _staging = queue_paths
    destination.mkdir(parents=True)
    (destination / "01 Test.flac").write_bytes(b"different audio")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)

    complete, item_id, evidence = _completed_journal(queue_paths)
    authority = run_lock.acquire()
    try:
        expectation = capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
        retired = journal.commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=evidence,
            post_import_action={
                "action_id": "a" * 64,
                "kind": "whole-album",
                "source": str(source),
                "destination": str(destination),
                "expectation": expectation,
                "phase": "planned",
                "relocation_operation_id": None,
                "handoff_hash": None,
            },
        )
        settled = post_import_finalizer._settle_action(
            retired,
            retired.retirements[0],
            authority,
        )
    finally:
        authority.close()

    assert settled.retirements[0].action is None
    assert settled.retirements[0].final_path == str(source)
    assert (source / "01 Test.flac").read_bytes() == b"audio"
    assert (destination / "01 Test.flac").read_bytes() == b"different audio"
