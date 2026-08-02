"""Focused guards for durable post-import action integration."""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

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
from qobuz_librarian.integrations.beets import ManagedCarrierRetirementOutcome
from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseIdentity,
    ReleaseManifestError,
    publish_release_identity,
    read_release_identity,
)
from qobuz_librarian.queue import (
    durable_runner,
    journal,
    post_import_finalizer,
    startup_recovery,
)


class _StopAfterPolicyCapture(RuntimeError):
    pass


def test_carrier_retirement_publishes_identity_before_acknowledgement(
    monkeypatch,
):
    item_id = "b" * 64
    retirement = SimpleNamespace(
        item_id=item_id,
        action=SimpleNamespace(phase=journal.PostImportActionPhase.PLANNED),
        final_path="/music/a",
    )
    saved = SimpleNamespace(retirements=(retirement,))
    committed_retirement = SimpleNamespace(
        item_id=item_id,
        action=SimpleNamespace(phase=journal.PostImportActionPhase.COMMITTED),
        final_path="/music/a",
    )
    committed = SimpleNamespace(retirements=(committed_retirement,))
    cleared_retirement = SimpleNamespace(
        item_id=item_id, action=None, final_path="/music/a",
    )
    cleared = SimpleNamespace(retirements=(cleared_retirement,))
    events = []

    authority_checks = []
    monkeypatch.setattr(
        post_import_finalizer,
        "_require_authority",
        lambda _a: authority_checks.append("check"),
    )
    def settle(current, *_args):
        events.append("settle")
        return committed if current is saved else cleared

    monkeypatch.setattr(post_import_finalizer, "_settle_action", settle)
    monkeypatch.setattr(
        post_import_finalizer, "_retirement_publication", lambda *_args: None
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "_publish_retirement_identity",
        lambda *_args, **_kwargs: events.append("identity"),
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "_acknowledge_completion",
        lambda current, *_args: events.append("acknowledge") or current,
    )
    monkeypatch.setattr(
        post_import_finalizer.queue_state,
        "process_carrier_retirement",
        lambda current, **_kwargs: (
            events.append("carrier") or current,
            SimpleNamespace(outcome=ManagedCarrierRetirementOutcome.RETIRED),
        ),
    )

    post_import_finalizer.finalize_carrier_retirement(
        saved,
        item_id,
        authority=object(),
        acknowledge_completion=None,
    )

    assert events == [
        "settle", "identity", "settle", "acknowledge", "carrier",
    ]


def test_identity_publication_failure_preserves_retirement_evidence(monkeypatch):
    item_id = "b" * 64
    action = SimpleNamespace(phase=journal.PostImportActionPhase.COMMITTED)
    retirement = SimpleNamespace(item_id=item_id, action=action, final_path="/music/a")
    saved = SimpleNamespace(retirements=(retirement,))
    events = []

    authority_checks = []
    monkeypatch.setattr(
        post_import_finalizer,
        "_require_authority",
        lambda _a: authority_checks.append("check"),
    )

    def refuse(*_args, **_kwargs):
        events.append("identity")
        raise ReleaseManifestError("release manifest identifies a different release")

    monkeypatch.setattr(
        post_import_finalizer,
        "_publish_retirement_identity",
        refuse,
    )
    monkeypatch.setattr(
        post_import_finalizer, "_retirement_publication", lambda *_args: None
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "_acknowledge_completion",
        lambda *_args: events.append("acknowledge"),
    )
    monkeypatch.setattr(
        post_import_finalizer.queue_state,
        "process_carrier_retirement",
        lambda *_args, **_kwargs: events.append("carrier"),
    )

    with pytest.raises(ReleaseManifestError, match="different release"):
        post_import_finalizer.finalize_carrier_retirement(
            saved,
            item_id,
            authority=object(),
            acknowledge_completion=None,
        )

    assert saved.retirements == (retirement,)
    assert events == ["identity"]


def test_unacknowledged_relocation_never_publishes_identity(monkeypatch):
    item_id = "b" * 64
    action = SimpleNamespace(phase=journal.PostImportActionPhase.HANDOFF)
    retirement = SimpleNamespace(item_id=item_id, action=action, final_path="/music/a")
    saved = SimpleNamespace(retirements=(retirement,))
    events = []

    monkeypatch.setattr(post_import_finalizer, "_require_authority", lambda _a: None)

    def unavailable(*_args):
        events.append("settle")
        raise post_import_finalizer.PostImportFinalizationUnavailable(
            "relocation handoff is unavailable"
        )

    monkeypatch.setattr(post_import_finalizer, "_settle_action", unavailable)
    monkeypatch.setattr(
        post_import_finalizer,
        "_publish_retirement_identity",
        lambda *_args: events.append("identity"),
    )

    with pytest.raises(
        post_import_finalizer.PostImportFinalizationUnavailable,
        match="relocation handoff",
    ):
        post_import_finalizer.finalize_carrier_retirement(
            saved,
            item_id,
            authority=object(),
            acknowledge_completion=None,
        )

    assert saved.retirements == (retirement,)
    assert events == ["settle"]


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_conflicting_identity_keeps_completion_carrier_and_beets_rows(
    monkeypatch,
    tmp_path,
):
    operation_id = "a" * 64
    item_id = "b" * 64
    owner = RecoveryOwner(operation_id, item_id)
    final_dir = tmp_path / "music" / "Artist" / "Album [qobuz-200]"
    final_dir.mkdir(parents=True)
    audio = final_dir / "01.flac"
    audio.write_bytes(b"audio")
    source = StagedReceipt(
        "/staging/01.flac",
        (1, 2, 0o100600, 5, 6, 7),
    )
    slot = "qobuz:track-1"
    completion_input = CompletionInput(
        owner=owner,
        origin=CompletionOrigin(CompletionOriginKind.CLI, "test-queue"),
        expectation=CompletionExpectation(
            album_id="200",
            scope=CompletionScope.ALBUM,
            catalogue_slots=(slot,),
            requested_slots=(slot,),
            quality_targets=(QualityTarget(slot, 16, 44_100),),
        ),
        effective_tier=2,
        release_identity=ReleaseIdentity("qobuz", "200"),
        placement_destination=str(final_dir),
        lineages=(SourceLineage(slot, source),),
        counts=DownloadCounts(),
    )
    album_stat = final_dir.stat()
    audio_stat = audio.stat()
    album_identity = (
        album_stat.st_dev,
        album_stat.st_ino,
        album_stat.st_size,
        album_stat.st_mtime_ns,
        album_stat.st_ctime_ns,
    )
    destination_identity = (
        audio_stat.st_dev,
        audio_stat.st_ino,
        audio_stat.st_size,
        audio_stat.st_mtime_ns,
        audio_stat.st_ctime_ns,
    )
    relative_album = "Artist/Album [qobuz-200]"
    relative_audio = f"{relative_album}/01.flac"
    landing = LandingReceipt(
        slot,
        relative_audio,
        destination_identity,
        hashlib.sha256(audio.read_bytes()).hexdigest(),
    )
    assessment = assess_completion(
        owner=owner,
        expectation=completion_input.expectation,
        download=completion_input.download_coverage(),
        managed=ManagedImportEvidence(
            owner=owner,
            library_root=str(tmp_path / "music"),
            library_root_identity=(2, 10, 0, 1_000, 2_000),
            album_path=relative_album,
            album_identity=album_identity,
            manifest_hash="c" * 64,
            mappings=(ManagedMapping(
                slot,
                source.path,
                source.identity,
                relative_audio,
                destination_identity,
            ),),
        ),
        landings=(landing,),
        inventory=AlbumInventory(
            relative_album,
            album_identity,
            (landing,),
        ),
        quality=(TrackQuality(
            slot,
            relative_audio,
            destination_identity,
            landing.sha256,
            16,
            44_100,
        ),),
    )
    assert assessment.evidence is not None
    retirement = SimpleNamespace(
        item_id=item_id,
        action=None,
        final_path=str(final_dir),
        planned={"album": {"id": "200"}},
        completion_input=completion_input.to_record(),
        completion_evidence=assessment.evidence.to_record(),
        completion_acknowledged=False,
    )
    saved = SimpleNamespace(operation_id=operation_id, retirements=(retirement,))
    publish_release_identity(final_dir, ReleaseIdentity("qobuz", "100"))
    manifest_before = (final_dir / MANIFEST_NAME).read_bytes()
    beets_db = tmp_path / "beets" / "library.db"
    beets_db.parent.mkdir()
    beets_db.write_bytes(b"beets rows before conflict")
    rows_before = beets_db.read_bytes()
    events = []
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    authority = run_lock.acquire()

    monkeypatch.setattr(
        post_import_finalizer,
        "_acknowledge_completion",
        lambda *_args: events.append("acknowledge"),
    )
    monkeypatch.setattr(
        post_import_finalizer.queue_state,
        "process_carrier_retirement",
        lambda *_args, **_kwargs: events.append("carrier"),
    )

    try:
        with pytest.raises(ReleaseManifestError, match="different release"):
            post_import_finalizer.finalize_carrier_retirement(
                saved,
                item_id,
                authority=authority,
                acknowledge_completion=None,
            )
    finally:
        authority.close()

    assert saved.retirements == (retirement,)
    assert retirement.completion_evidence == assessment.evidence.to_record()
    assert (final_dir / MANIFEST_NAME).read_bytes() == manifest_before
    assert beets_db.read_bytes() == rows_before
    assert events == []


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux inode leases are the authoritative write-exclusion backend",
)
def test_committed_whole_album_relocation_publishes_at_new_directory_identity(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "music" / "Artist" / "Album"
    destination = tmp_path / "music" / "Various Artists" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    audio = destination / "01.flac"
    audio.write_bytes(b"relocated audio")
    source_stat = source.stat()
    audio_stat = audio.stat()
    identity = ReleaseIdentity("qobuz", "200")
    completion_input = SimpleNamespace(
        release_identity=identity,
        placement_destination=str(source),
        expectation=object(),
        download_coverage=lambda: object(),
    )
    evidence = SimpleNamespace(
        library_root=str(tmp_path / "music"),
        album_path="Artist/Album",
        album_identity=(
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        ),
        inventory=SimpleNamespace(
            path="Artist/Album",
            audio=(SimpleNamespace(
                path="Artist/Album/01.flac",
                identity=(
                    audio_stat.st_dev,
                    audio_stat.st_ino,
                    audio_stat.st_size,
                    audio_stat.st_mtime_ns,
                    audio_stat.st_ctime_ns,
                ),
                sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
            ),),
        ),
    )
    action = SimpleNamespace(
        phase=journal.PostImportActionPhase.COMMITTED,
        kind=post_import_finalizer.RelocationKind.WHOLE_ALBUM.value,
        destination=str(destination),
    )
    retirement = SimpleNamespace(
        item_id="b" * 64,
        action=action,
        final_path=str(destination),
        planned={"album": {"id": "200"}},
        completion_input={"frozen": True},
        completion_evidence={"exact": True},
    )
    saved = SimpleNamespace(operation_id="a" * 64)
    monkeypatch.setattr(
        post_import_finalizer,
        "parse_completion_input_record",
        lambda *_args, **_kwargs: completion_input,
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "completion_input_ready",
        lambda value: value is completion_input,
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "parse_completion_record",
        lambda *_args, **_kwargs: evidence,
    )
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    authority = run_lock.acquire()
    try:
        assert post_import_finalizer._publish_retirement_identity(
            saved, retirement, authority=authority
        )
        assert (destination.stat().st_dev, destination.stat().st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        )
        assert read_release_identity(destination) == identity
        assert not post_import_finalizer._publish_retirement_identity(
            saved, retirement, authority=authority
        )

        retirement.action = None
        assert not post_import_finalizer._publish_retirement_identity(
            saved, retirement, authority=authority
        )

        (destination / MANIFEST_NAME).unlink()
        with pytest.raises(
            ReleaseManifestError,
            match="unavailable after publication",
        ):
            post_import_finalizer._publish_retirement_identity(
                saved, retirement, authority=authority
            )
    finally:
        authority.close()

    assert read_release_identity(destination) is None


def test_current_identity_inventory_rejects_extra_and_replaced_audio(
        tmp_path, monkeypatch):
    final_dir = tmp_path / "music" / "Artist" / "Album"
    final_dir.mkdir(parents=True)
    audio = final_dir / "01.flac"
    audio.write_bytes(b"completed audio")
    value = audio.stat()
    identity = (
        value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    receipt = SimpleNamespace(
        path="Artist/Album/01.flac",
        identity=identity,
        sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
    )
    evidence = SimpleNamespace(
        album_path="Artist/Album",
        inventory=SimpleNamespace(path="Artist/Album", audio=(receipt,)),
    )

    assert post_import_finalizer._current_inventory_matches(
        evidence, str(final_dir), relocated=False
    )
    (final_dir / "99.flac").write_bytes(b"contradictory")
    assert not post_import_finalizer._current_inventory_matches(
        evidence, str(final_dir), relocated=False
    )
    (final_dir / "99.flac").unlink()
    audio.write_bytes(b"replaced audio")
    assert not post_import_finalizer._current_inventory_matches(
        evidence, str(final_dir), relocated=False
    )

    audio.write_bytes(b"completed audio")
    value = audio.stat()
    receipt.identity = (
        value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    receipt.sha256 = hashlib.sha256(audio.read_bytes()).hexdigest()
    real_lstat = post_import_finalizer.os.lstat
    real_stat = post_import_finalizer.os.stat
    overwritten = False

    def lstat_then_overwrite(path, *args, **kwargs):
        nonlocal overwritten
        named = real_lstat(path, *args, **kwargs)
        if Path(path) == audio:
            audio.write_bytes(b"corrupted audio")
            overwritten = True
        return named

    def stat_then_overwrite(path, *args, **kwargs):
        nonlocal overwritten
        named = real_stat(path, *args, **kwargs)
        if (
            not overwritten
            and path == audio.name
            and kwargs.get("dir_fd") is not None
        ):
            audio.write_bytes(b"corrupted audio")
            overwritten = True
        return named

    monkeypatch.setattr(
        post_import_finalizer.os,
        "lstat",
        lstat_then_overwrite,
    )
    monkeypatch.setattr(
        post_import_finalizer.os,
        "stat",
        stat_then_overwrite,
    )

    assert not post_import_finalizer._current_inventory_matches(
        evidence, str(final_dir), relocated=False
    )
    assert overwritten is True
    assert audio.read_bytes() == b"corrupted audio"


def test_split_relocation_noop_keeps_action_and_refuses_publication(
    monkeypatch, tmp_path
):
    source = tmp_path / "music" / "Other" / "Album"
    destination = tmp_path / "music" / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "01.flac").write_bytes(b"source recording")
    (destination / "01.flac").write_bytes(b"contradictory recording")
    action = SimpleNamespace(
        action_id="c" * 64,
        kind=post_import_finalizer.RelocationKind.SPLIT_GAP_FILL.value,
        source=str(source),
        destination=str(destination),
        expectation={"exact": True},
        phase=journal.PostImportActionPhase.PLANNED,
    )
    retirement = SimpleNamespace(
        item_id="b" * 64,
        action=action,
        final_path=action.destination,
    )
    saved = SimpleNamespace(retirements=(retirement,))
    monkeypatch.setattr(post_import_finalizer, "_require_authority", lambda _a: None)
    monkeypatch.setattr(
        post_import_finalizer,
        "relocate_post_import_album",
        lambda *_args, **_kwargs: SimpleNamespace(
            changed=False,
            operation_id=None,
            ownership_receipt=None,
            published_files=0,
            destination=Path(action.destination),
            reason="all destination names already exist",
        ),
    )

    with pytest.raises(OSError, match="no exact move"):
        post_import_finalizer._settle_action(saved, retirement, object())
    assert saved.retirements[0].action is action
    assert (source / "01.flac").read_bytes() == b"source recording"
    assert not (destination / MANIFEST_NAME).exists()


def test_split_partial_conflict_keeps_action_and_refuses_publication(
    monkeypatch, tmp_path
):
    source = tmp_path / "music" / "Other" / "Album"
    destination = tmp_path / "music" / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    conflict = source / "01.flac"
    conflict.write_bytes(b"source conflict")
    (destination / "01.flac").write_bytes(b"destination conflict")
    (destination / "02.flac").write_bytes(b"already moved")
    action = SimpleNamespace(
        action_id="c" * 64,
        kind=post_import_finalizer.RelocationKind.SPLIT_GAP_FILL.value,
        source=str(source),
        destination=str(destination),
        expectation={"exact": True},
        phase=journal.PostImportActionPhase.PLANNED,
    )
    retirement = SimpleNamespace(
        item_id="b" * 64, action=action, final_path=str(destination),
    )
    saved = SimpleNamespace(retirements=(retirement,))
    monkeypatch.setattr(post_import_finalizer, "_require_authority", lambda _a: None)
    monkeypatch.setattr(
        post_import_finalizer,
        "relocate_post_import_album",
        lambda *_args, **_kwargs: SimpleNamespace(
            changed=True,
            operation_id="d" * 64,
            ownership_receipt={"exact": True},
            published_files=1,
            destination=destination,
            reason="kept 1 existing destination name(s)",
        ),
    )

    with pytest.raises(OSError, match="no exact move"):
        post_import_finalizer._settle_action(saved, retirement, object())
    assert saved.retirements[0].action is action
    assert conflict.read_bytes() == b"source conflict"
    assert not (destination / MANIFEST_NAME).exists()


def test_late_cancel_refuses_durable_identity_publication(monkeypatch):
    item_id = "b" * 64
    planned = SimpleNamespace(phase=journal.PostImportActionPhase.PLANNED)
    committed = SimpleNamespace(phase=journal.PostImportActionPhase.COMMITTED)
    retirement = SimpleNamespace(
        item_id=item_id,
        action=planned,
        final_path="/music/Artist/Album",
    )
    saved = SimpleNamespace(retirements=(retirement,))
    settled_retirement = SimpleNamespace(
        item_id=item_id,
        action=committed,
        final_path="/music/Artist/Album",
    )
    settled = SimpleNamespace(retirements=(settled_retirement,))
    events = []
    monkeypatch.setattr(post_import_finalizer, "_require_authority", lambda _a: None)
    monkeypatch.setattr(
        post_import_finalizer,
        "_publish_retirement_identity",
        lambda *_args, **_kwargs: events.append("identity"),
    )
    monkeypatch.setattr(
        post_import_finalizer,
        "_settle_action",
        lambda *_args: events.append("settle") or settled,
    )
    checks = iter((False, True))

    with pytest.raises(OSError, match="cancelled"):
        post_import_finalizer.finalize_carrier_retirement(
            saved,
            item_id,
            authority=object(),
            acknowledge_completion=None,
            cancel_check=lambda: next(checks),
        )
    assert events == ["settle"]
    assert settled.retirements[0].action is committed


def test_new_durable_item_freezes_multi_artist_filing_policy(monkeypatch):
    saved = SimpleNamespace(operation_id="a" * 64)
    saved_item = SimpleNamespace(
        item_id="b" * 64,
        phase=journal.QueuePhase.PENDING,
    )
    monkeypatch.setattr(durable_runner, "_require_authority", lambda _value: None)
    monkeypatch.setattr(durable_runner, "_require_current_plan", lambda *_args: None)
    monkeypatch.setattr(
        durable_runner,
        "_claim_pending_item",
        lambda *_args, **_kwargs: (saved, saved_item),
    )
    monkeypatch.setattr(
        durable_runner,
        "initial_completion_input",
        lambda *_args: {"frozen": True},
    )
    monkeypatch.setattr(durable_runner, "isolated_staging_run_names", lambda: ())

    def capture_policy(current, item_id, phase, **values):
        assert current is saved
        assert item_id == saved_item.item_id
        assert phase is journal.QueuePhase.ACTIVE
        assert values["multi_artist_filing"] is True
        raise _StopAfterPolicyCapture

    monkeypatch.setattr(
        durable_runner.queue_state,
        "transition_journal_item",
        capture_policy,
    )

    with pytest.raises(_StopAfterPolicyCapture):
        durable_runner.execute_durable_new_album(
            [],
            {},
            SimpleNamespace(migrate_multi_artist=True),
            plan=object(),
            origin=object(),
            mode="test",
            authority=object(),
        )


def test_startup_retirement_uses_action_finalizer(monkeypatch):
    retirement = SimpleNamespace(item_id="e" * 64)
    current = SimpleNamespace(
        operation_id="f" * 64,
        items=(),
        retirements=(retirement,),
    )
    state = {"journal": current}
    callback = object()

    monkeypatch.setattr(startup_recovery, "_require_authority", lambda _value: None)
    monkeypatch.setattr(
        startup_recovery,
        "reconcile_post_import_relocations",
        lambda **_kwargs: SimpleNamespace(
            status=startup_recovery.RelocationRecoveryStatus.CLEAR
        ),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_load_namespace",
        lambda _authority: (
            ()
            if state["journal"] is None
            else (
                SimpleNamespace(
                    status=journal.QueueLoadStatus.READY,
                    journal=state["journal"],
                ),
            )
        ),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_recover_active_library_backups",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        startup_recovery,
        "_unclaimed_staging_run_names",
        lambda *_args: (),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_recover_staging_references",
        lambda *_args: False,
    )

    def finalize(saved, item_id, *, authority, acknowledge_completion):
        assert saved is current
        assert item_id == retirement.item_id
        assert authority == "authority"
        assert acknowledge_completion is callback
        state["journal"] = SimpleNamespace(
            operation_id=current.operation_id,
            items=(),
            retirements=(),
        )
        return (
            state["journal"],
            Path("/music/Various Artists/Album"),
            SimpleNamespace(
                outcome=ManagedCarrierRetirementOutcome.RETIRED
            ),
        )

    def clear(operation_id):
        assert operation_id == current.operation_id
        state["journal"] = None

    monkeypatch.setattr(
        startup_recovery,
        "finalize_carrier_retirement",
        finalize,
    )
    monkeypatch.setattr(startup_recovery.queue_state, "clear_queue_journal", clear)

    result = startup_recovery.recover_startup_state(
        authority="authority",
        acknowledge_completion=callback,
    )

    assert result.status is startup_recovery.StartupRecoveryStatus.CLEAR
