import os

import pytest


def test_acquire_fsyncs_pid_to_disk(tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    fsynced_fds = []
    orig_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: fsynced_fds.append(fd) or orig_fsync(fd))

    from qobuz_librarian import run_lock

    fp = run_lock.acquire()
    try:
        assert fp is not None
        assert fp.intact() is True
        assert fp.fileno() in fsynced_fds
        assert lock_file.read_text().strip() == str(os.getpid())
    finally:
        if fp is not None:
            fp.close()
    assert fp.intact() is False


def test_second_acquire_while_held_raises_lockbusy_with_holder_pid(tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    held = run_lock.acquire()
    try:
        assert held is not None
        with pytest.raises(run_lock.LockBusy) as caught:
            run_lock.acquire()
        assert caught.value.pid == str(os.getpid())
    finally:
        held.close()

    again = run_lock.acquire()
    assert again is not None
    again.close()


def test_current_lease_returns_only_the_live_acquired_handle(
        tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    held = run_lock.acquire()
    assert held is not None
    assert run_lock.current_lease() is held

    held.close()
    assert run_lock.current_lease() is None


def test_current_lease_refuses_stale_fork_and_wrong_path_handles(
        tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    real_getpid = os.getpid
    real_pid = real_getpid()
    held = run_lock.acquire()
    assert held is not None
    try:
        monkeypatch.setattr(run_lock.os, "getpid", lambda: real_pid + 1)
        assert held.intact() is False
        assert run_lock.current_lease() is None
    finally:
        held.close()

    monkeypatch.setattr(run_lock.os, "getpid", real_getpid)

    held = run_lock.acquire()
    assert held is not None
    try:
        monkeypatch.setattr(
            "qobuz_librarian.config.LOCK_FILE", tmp_path / "different.lock")
        assert run_lock.current_lease() is None
    finally:
        held.close()


def test_acquire_refuses_a_symlink_without_touching_its_target(
        tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.write_text("keep me", encoding="utf-8")
    lock_file = tmp_path / "run.lock"
    lock_file.symlink_to(victim)
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    assert run_lock.acquire() is None
    assert victim.read_text(encoding="utf-8") == "keep me"


def test_replacing_the_lock_name_cannot_admit_a_second_writer(
        tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    first = run_lock.acquire()
    assert first is not None
    lock_file.unlink()
    try:
        assert first.intact() is False
        with pytest.raises(run_lock.LockBusy):
            run_lock.acquire()
    finally:
        first.close()


def test_acquire_degrades_to_none_when_flock_unsupported(tmp_path, monkeypatch, caplog):
    import errno
    import fcntl
    import logging

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    def no_flock(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")
    monkeypatch.setattr(fcntl, "flock", no_flock)

    from qobuz_librarian import run_lock

    with caplog.at_level(logging.WARNING, logger="qobuz_librarian"):
        assert run_lock.acquire() is None
    warning = next(
        r.getMessage() for r in caplog.records
        if "single-instance lock" in r.getMessage()
    )
    assert "data folder supports file locking" in warning
    assert "accept" not in warning


def test_cli_refuses_to_run_when_the_lock_is_unavailable(monkeypatch):
    from qobuz_librarian import cli, run_lock

    monkeypatch.setattr(run_lock, "acquire", lambda: None)

    with pytest.raises(SystemExit) as stopped:
        cli.acquire_run_lock()
    assert stopped.value.code == 1


def test_cli_refuses_unsettled_durable_recovery_after_acquiring_lock(
        monkeypatch):
    from qobuz_librarian import cli, run_lock
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    class Lease:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    lease = Lease()
    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(
        cli,
        "_recover_startup_queue",
        lambda authority: StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="queue-item-blocked",
        ),
    )

    with pytest.raises(SystemExit) as stopped:
        cli.acquire_run_lock()

    assert stopped.value.code == 1
    assert lease.closed is True


def test_cli_retry_choice_reopens_one_exact_blocked_terminal_queue(monkeypatch):
    from qobuz_librarian import cli, run_lock
    from qobuz_librarian.completion import (
        CompletionExpectation,
        CompletionInput,
        CompletionOrigin,
        CompletionOriginKind,
        CompletionScope,
        QualityTarget,
        RecoveryOwner,
    )
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue import startup_recovery

    class Lease:
        def intact(self):
            return True

        def close(self):
            pass

    lease = Lease()
    item = startup_recovery.StartupRecoveryItem(
        operation_id="a" * 64,
        item_id="b" * 64,
        mode="walk_queue",
        phase=queue_state.QueuePhase.BLOCKED,
        action=startup_recovery.StartupRecoveryAction.BLOCKED,
        reason="managed-reservation-origin",
    )
    attention = startup_recovery.StartupRecoveryResult(
        startup_recovery.StartupRecoveryStatus.ATTENTION_REQUIRED,
        (item,),
        "queue-item-blocked",
    )
    resume = startup_recovery.StartupRecoveryResult(
        startup_recovery.StartupRecoveryStatus.RESUME_REQUIRED,
        (
            startup_recovery.StartupRecoveryItem(
                operation_id=item.operation_id,
                item_id=item.item_id,
                mode=item.mode,
                phase=queue_state.QueuePhase.PENDING,
                action=startup_recovery.StartupRecoveryAction.PENDING,
            ),
        ),
    )
    recoveries = iter((attention, resume))
    slot = "qobuz:track-1"
    completion_input = CompletionInput(
        owner=RecoveryOwner(item.operation_id, item.item_id),
        origin=CompletionOrigin(
            CompletionOriginKind.CLI,
            "download-queue",
        ),
        expectation=CompletionExpectation(
            album_id="album-1",
            scope=CompletionScope.ALBUM,
            catalogue_slots=(slot,),
            requested_slots=(slot,),
            quality_targets=(QualityTarget(slot, 16, 44_100),),
        ),
        effective_tier=2,
    ).to_record()
    planned = {
        "album": {"id": "album-1", "title": "Interrupted album"},
        "label": "Interrupted album",
    }
    blocked_journal = queue_state.QueueJournal(
        operation_id=item.operation_id,
        mode=item.mode,
        saved_at="2026-07-12T00:00:00Z",
        items=(
            queue_state.JournalItem(
                item_id=item.item_id,
                phase=queue_state.QueuePhase.BLOCKED,
                planned=planned,
                recovery_references=(
                    queue_state.RecoveryReference(
                        name="managed-import-reservation",
                        kind="managed-beets-reservation",
                    ),
                ),
                block_reason="managed-reservation-origin",
                completion_input=completion_input,
            ),
        ),
    )
    pending_journal = queue_state.QueueJournal(
        operation_id=item.operation_id,
        mode=item.mode,
        saved_at="2026-07-12T00:00:01Z",
        items=(
            queue_state.JournalItem(
                item_id=item.item_id,
                phase=queue_state.QueuePhase.PENDING,
                planned=planned,
            ),
        ),
    )
    journal_loads = iter(
        (
            queue_state.QueueLoad(
                queue_state.QueueLoadStatus.READY,
                blocked_journal,
            ),
            queue_state.QueueLoad(
                queue_state.QueueLoadStatus.READY,
                pending_journal,
            ),
        )
    )
    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(
        queue_state,
        "load_queue_journal",
        lambda operation_id: next(journal_loads),
    )
    monkeypatch.setattr(
        cli, "_recover_startup_queue", lambda authority: next(recoveries)
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "r")
    settled = []
    monkeypatch.setattr(
        startup_recovery,
        "settle_blocked_item",
        lambda **kwargs: settled.append(kwargs)
        or startup_recovery.BlockedItemSettlementResult(
            startup_recovery.BlockedItemSettlementStatus.RETRYABLE,
            "The item can be retried.",
        ),
    )

    assert cli.acquire_run_lock() is lease
    assert cli._startup_recovery_status() is (
        startup_recovery.StartupRecoveryStatus.RESUME_REQUIRED
    )
    assert settled == [
        {
            "authority": lease,
            "operation_id": "a" * 64,
            "item_id": "b" * 64,
            "action": startup_recovery.BlockedItemSettlementAction.RETRY,
        }
    ]
