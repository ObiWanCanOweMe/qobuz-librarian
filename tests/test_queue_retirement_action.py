
import errno
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

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
    MANIFEST_NAME,
    ReleaseIdentity,
    capture_directory_path_receipt,
    read_release_identity,
)
from qobuz_librarian.queue import journal, post_import_finalizer
from qobuz_librarian.queue.builder import _build_queue_item


def _read_lease_is_live(path):
    value = path.stat()
    key = (
        f"{os.major(value.st_dev):02x}:{os.minor(value.st_dev):02x}:"
        f"{value.st_ino}"
    )
    return any(
        " LEASE " in line
        and " ACTIVE " in line
        and (" READ " in line or " WRITE " in line)
        and key in line
        for line in Path("/proc/locks").read_text(encoding="utf-8").splitlines()
    )


def _nonblocking_writer_errno(path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "\ntry: descriptor=os.open(sys.argv[1], os.O_WRONLY|os.O_NONBLOCK)"
                "\nexcept OSError as error: sys.exit(error.errno)"
                "\nelse: os.close(descriptor); sys.exit(0)"
            ),
            os.fspath(path),
        ],
        check=False,
    )
    return result.returncode


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_verified_inventory_releases_persistent_resources_in_reverse_order(
    tmp_path, monkeypatch
):
    from qobuz_librarian.library.release_identity import (
        RetainedReleaseManifestAuthority,
    )

    album = tmp_path / "Artist" / "Album"
    nested = album / "Disc 1"
    nested.mkdir(parents=True)
    audio = nested / "01.flac"
    audio.write_bytes(b"verified audio")
    value = audio.stat()
    expected_audio = {
        Path("Disc 1/01.flac"): (
            (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            ),
            hashlib.sha256(audio.read_bytes()).hexdigest(),
        )
    }
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    acquisition = []
    descriptors = {}
    directory_descriptors = []
    closed = []
    real_open_file = post_import_finalizer.AlbumAuthority.open_file
    real_hold_directories = (
        post_import_finalizer.VerifiedAlbumInventory._hold_directories
    )
    real_publish_retained = (
        post_import_finalizer.publish_release_identity_authorized_retained
    )
    real_retained_close = RetainedReleaseManifestAuthority.close
    real_close = os.close

    def open_file(instance, relative, **kwargs):
        held = real_open_file(instance, relative, **kwargs)
        acquisition.append(relative)
        descriptors[relative] = held.descriptor
        return held

    def hold_directories(instance, descriptor):
        previous = len(instance._directories)
        real_hold_directories(instance, descriptor)
        added = instance._directories[previous:]
        if added:
            acquisition.append("directories")
            directory_descriptors.extend(
                binding.descriptor for binding in added
            )

    def close(descriptor):
        if (
            descriptor in directory_descriptors
            and "directories" not in closed
        ):
            closed.append("directories")
        elif descriptor == descriptors.get(Path("Disc 1/01.flac")):
            closed.append("audio")
        return real_close(descriptor)

    def publish_retained(album_authority, identity):
        retained = real_publish_retained(album_authority, identity)
        acquisition.append(Path(MANIFEST_NAME))
        descriptors[Path(MANIFEST_NAME)] = retained.descriptor
        return retained

    def close_retained(instance):
        closed.append("manifest")
        return real_retained_close(instance)

    monkeypatch.setattr(
        post_import_finalizer.AlbumAuthority,
        "open_file",
        open_file,
    )
    monkeypatch.setattr(
        post_import_finalizer.VerifiedAlbumInventory,
        "_hold_directories",
        hold_directories,
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "publish_release_identity_authorized_retained",
        publish_retained,
    )
    monkeypatch.setattr(
        RetainedReleaseManifestAuthority,
        "close",
        close_retained,
    )
    monkeypatch.setattr(post_import_finalizer.os, "close", close)
    authority = run_lock.acquire()
    try:
        with post_import_finalizer.open_verified_album_inventory(
            album,
            authority,
            capture_directory_path_receipt(album),
            expected_audio,
        ) as inventory:
            inventory.publish(ReleaseIdentity("qobuz", "1"))
    finally:
        authority.close()

    assert acquisition == [
        Path("Disc 1/01.flac"),
        "directories",
        Path(MANIFEST_NAME),
    ]
    assert closed == ["manifest", "directories", "audio"]


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
    saved,
    item_id,
    staging,
    *,
    complete=False,
    release_destination=None,
    origin_kind=CompletionOriginKind.CLI,
):
    slot = "qobuz:track-1"
    return CompletionInput(
        owner=RecoveryOwner(saved.operation_id, item_id),
        origin=CompletionOrigin(origin_kind, "test-queue"),
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
    origin_kind=CompletionOriginKind.CLI,
):
    completion_input = _completion_input(
        saved, item_id, staging, complete=True,
        release_destination=release_destination,
        origin_kind=origin_kind,
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
    origin_kind=CompletionOriginKind.CLI,
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
            origin_kind=origin_kind,
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
            origin_kind=origin_kind,
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
        origin_kind=origin_kind,
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


@pytest.mark.parametrize(
    "cancel_at_commit,replace_during_release",
    ((False, False), (True, False), (False, True)),
)
def test_real_committed_relocation_publishes_before_evidence_retirement(
    queue_paths, monkeypatch, cancel_at_commit, replace_during_release
):
    tmp_path, _music, source, destination, _staging = queue_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")

    complete, item_id, evidence = _completed_journal(
        queue_paths, release_destination=source
    )
    displaced = tmp_path / "displaced-after-release"
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
            assert _read_lease_is_live(destination / "01 Test.flac")
            result = real_publish(current, retirement, **kwargs)
            assert _read_lease_is_live(destination / "01 Test.flac")
            assert _read_lease_is_live(destination / MANIFEST_NAME)
            events.append("identity")
            return result

        def release(*args, **kwargs):
            assert (destination / MANIFEST_NAME).is_file()
            assert _read_lease_is_live(destination / "01 Test.flac")
            assert _read_lease_is_live(destination / MANIFEST_NAME)
            events.append("release")
            if replace_during_release:
                destination.rename(displaced)
                destination.mkdir()
            return None

        def acknowledge(current, retirement, callback, **kwargs):
            assert retirement.action is None
            assert _read_lease_is_live(destination / "01 Test.flac")
            assert _read_lease_is_live(destination / MANIFEST_NAME)
            events.append("acknowledge")
            return real_ack(current, retirement, callback, **kwargs)

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
            lambda *_args: (
                assert_retirement_lease()
                or events.append("carrier")
                or carrier_result
            ),
        )

        def assert_retirement_lease():
            assert _read_lease_is_live(destination / "01 Test.flac")
            assert _read_lease_is_live(destination / MANIFEST_NAME)

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
            assert not _read_lease_is_live(destination / "01 Test.flac")
            post_import_finalizer.finalize_carrier_retirement(
                held,
                item_id,
                authority=authority,
                acknowledge_completion=None,
            )
        elif replace_during_release:
            with pytest.raises(OSError, match="namespace"):
                post_import_finalizer.finalize_carrier_retirement(
                    retired,
                    item_id,
                    authority=authority,
                    acknowledge_completion=None,
                    cancel_check=cancel_check,
                )
            held = journal.load_queue_journal(retired.operation_id).journal
            assert held is not None
            assert held.retirements[0].action is not None
            assert (
                held.retirements[0].action.phase
                is journal.PostImportActionPhase.COMMITTED
            )
            assert held.retirements[0].completion_acknowledged is False
            assert events == ["identity", "release"]
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
            assert read_release_identity(destination) == ReleaseIdentity(
                "qobuz", "1"
            )
            assert seen_phases == [
                journal.PostImportActionPhase.PLANNED,
                journal.PostImportActionPhase.HANDOFF,
                journal.PostImportActionPhase.COMMITTED,
            ]
        retained = displaced if replace_during_release else destination
        assert not _read_lease_is_live(retained / "01 Test.flac")
        assert not _read_lease_is_live(retained / MANIFEST_NAME)
    finally:
        authority.close()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_durable_inventory_acquisition_failure_keeps_retirement_evidence(
    queue_paths, monkeypatch
):
    tmp_path, _music, source, _destination, _staging = queue_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")
    complete, item_id, evidence = _completed_journal(
        queue_paths, release_destination=source
    )
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=None,
    )
    before = retired.retirements[0]
    authority = run_lock.acquire()
    writer = os.open(source / "01 Test.flac", os.O_RDWR)
    try:
        with pytest.raises(OSError, match="protect|writer|authority"):
            post_import_finalizer.finalize_carrier_retirement(
                retired,
                item_id,
                authority=authority,
                acknowledge_completion=None,
            )
    finally:
        os.close(writer)
        authority.close()

    persisted = journal.load_queue_journal(retired.operation_id).journal
    assert persisted is not None
    assert persisted.retirements == (before,)
    assert persisted.retirements[0].completion_acknowledged is False
    assert not (source / MANIFEST_NAME).exists()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
@pytest.mark.parametrize(
    "failure",
    (
        "writer",
        "manifest_writer",
        "namespace",
        "cancel",
        "exception",
        "crash",
    ),
)
def test_post_publish_failure_keeps_completion_action_and_carrier_evidence(
    queue_paths, monkeypatch, failure
):
    tmp_path, _music, source, _destination, _staging = queue_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")
    complete, item_id, evidence = _completed_journal(
        queue_paths, release_destination=source
    )
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=None,
    )
    before = retired.retirements[0]
    authority = run_lock.acquire()
    audio = source / "01 Test.flac"
    displaced = tmp_path / "displaced-album"
    writer_errors = []
    real_publish = post_import_finalizer._publish_retirement_identity

    def publish_then_interrupt(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        if failure == "writer":
            writer_errors.append(_nonblocking_writer_errno(audio))
        elif failure == "manifest_writer":
            writer_errors.append(
                _nonblocking_writer_errno(source / MANIFEST_NAME)
            )
        elif failure == "namespace":
            source.rename(displaced)
            source.mkdir()
        elif failure == "exception":
            raise RuntimeError("injected acknowledgement boundary failure")
        elif failure == "crash":
            raise KeyboardInterrupt("injected crash after publication")
        return result

    monkeypatch.setattr(
        post_import_finalizer,
        "_publish_retirement_identity",
        publish_then_interrupt,
    )
    if failure == "cancel":
        monkeypatch.setattr(
            post_import_finalizer,
            "_acknowledge_completion",
            lambda *_args: pytest.fail(
                "completion was acknowledged after late cancellation"
            ),
        )

    def cancel_check():
        return failure == "cancel" and (source / MANIFEST_NAME).exists()

    try:
        if failure == "crash":
            with pytest.raises(KeyboardInterrupt, match="injected crash"):
                post_import_finalizer.finalize_carrier_retirement(
                    retired,
                    item_id,
                    authority=authority,
                    acknowledge_completion=None,
                    cancel_check=cancel_check,
                )
        elif failure == "exception":
            with pytest.raises(RuntimeError, match="injected acknowledgement"):
                post_import_finalizer.finalize_carrier_retirement(
                    retired,
                    item_id,
                    authority=authority,
                    acknowledge_completion=None,
                    cancel_check=cancel_check,
                )
        else:
            with pytest.raises(
                OSError,
                match="cancelled|namespace|manifest",
            ):
                post_import_finalizer.finalize_carrier_retirement(
                    retired,
                    item_id,
                    authority=authority,
                    acknowledge_completion=None,
                    cancel_check=cancel_check,
                )
    finally:
        authority.close()

    persisted = journal.load_queue_journal(retired.operation_id).journal
    assert persisted is not None
    assert persisted.retirements == (before,)
    assert persisted.retirements[0].completion_acknowledged is False
    assert persisted.retirements[0].reference == before.reference
    retained_audio = displaced / audio.name if failure == "namespace" else audio
    assert not _read_lease_is_live(retained_audio)
    if failure in {"writer", "manifest_writer"}:
        assert writer_errors == [errno.EAGAIN]
        writable = (
            source / MANIFEST_NAME
            if failure == "manifest_writer"
            else audio
        )
        descriptor = os.open(writable, os.O_WRONLY | os.O_NONBLOCK)
        os.close(descriptor)
    if failure == "namespace":
        assert read_release_identity(source) is None
        assert read_release_identity(displaced) == ReleaseIdentity("qobuz", "1")


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
@pytest.mark.parametrize("boundary", ("acknowledgement", "carrier"))
@pytest.mark.parametrize("disturbance", ("namespace", "run_lock"))
def test_callback_authority_loss_does_not_advance_retirement_evidence(
    queue_paths, monkeypatch, boundary, disturbance
):
    tmp_path, _music, source, _destination, _staging = queue_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")
    origin_kind = (
        CompletionOriginKind.WEB_JOB
        if boundary == "acknowledgement"
        else CompletionOriginKind.CLI
    )
    complete, item_id, evidence = _completed_journal(
        queue_paths,
        release_destination=source,
        origin_kind=origin_kind,
    )
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=None,
    )
    before = retired.retirements[0]
    displaced = tmp_path / f"displaced-during-{boundary}"

    authority = run_lock.acquire()

    def disturb_authority():
        if disturbance == "namespace":
            source.rename(displaced)
            source.mkdir()
        else:
            authority.close()

    acknowledgement = None
    if boundary == "acknowledgement":
        def acknowledgement(*_args, **_kwargs):
            disturb_authority()
            return True
    else:
        from qobuz_librarian.integrations import beets

        outcome = beets.ManagedCarrierRetirementResult(
            beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT
        )

        def retire(*_args):
            disturb_authority()
            return outcome

        monkeypatch.setattr(beets, "retire_managed_carrier", retire)

    try:
        with pytest.raises(OSError, match="namespace|authority|run-lock"):
            post_import_finalizer.finalize_carrier_retirement(
                retired,
                item_id,
                authority=authority,
                acknowledge_completion=acknowledgement,
            )
    finally:
        authority.close()

    persisted = journal.load_queue_journal(retired.operation_id).journal
    assert persisted is not None
    assert persisted.retirements == (before,)
    retained = displaced if disturbance == "namespace" else source
    if disturbance == "namespace":
        assert read_release_identity(source) is None
    assert read_release_identity(retained) == ReleaseIdentity("qobuz", "1")
    assert not _read_lease_is_live(retained / "01 Test.flac")
    assert not _read_lease_is_live(retained / MANIFEST_NAME)


@pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "fork"),
    reason="requires Linux inode leases and fork crash semantics",
)
def test_process_crash_releases_inventory_authority_and_keeps_evidence(
    queue_paths, monkeypatch
):
    tmp_path, _music, source, _destination, _staging = queue_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")
    complete, item_id, evidence = _completed_journal(
        queue_paths,
        release_destination=source,
    )
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=None,
    )
    before = retired.retirements[0]
    real_publish = post_import_finalizer._publish_retirement_identity

    def publish_then_crash(*args, **kwargs):
        real_publish(*args, **kwargs)
        os._exit(73)

    monkeypatch.setattr(
        post_import_finalizer,
        "_publish_retirement_identity",
        publish_then_crash,
    )
    child = os.fork()
    if child == 0:
        authority = run_lock.acquire()
        post_import_finalizer.finalize_carrier_retirement(
            retired,
            item_id,
            authority=authority,
            acknowledge_completion=None,
        )
        os._exit(0)

    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 73
    persisted = journal.load_queue_journal(retired.operation_id).journal
    assert persisted is not None
    assert persisted.retirements == (before,)
    assert read_release_identity(source) == ReleaseIdentity("qobuz", "1")
    assert not _read_lease_is_live(source / "01 Test.flac")
    assert not _read_lease_is_live(source / MANIFEST_NAME)
    for path in (source / "01 Test.flac", source / MANIFEST_NAME):
        descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(descriptor)


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
