import errno
import gc
import multiprocessing
import os
import signal
import sqlite3
import stat
import threading
import time

import pytest

from qobuz_librarian.library import sqlite_atomic


def _seed_database(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE entries (value INTEGER NOT NULL)")
    connection.execute("INSERT INTO entries VALUES (1)")
    connection.commit()
    connection.close()


def _open_anchor(path):
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    descriptor = os.open(path.name, os.O_RDONLY, dir_fd=parent_fd)
    anchor = {
        "path": path,
        "parent_chain": [parent_fd],
        "parent_names": (),
        "name": path.name,
        "descriptor": descriptor,
    }

    def matches(current):
        return sqlite_atomic._named_entry_matches(
            parent_fd, path.name, current["descriptor"])

    return anchor, matches


def _close_anchor(anchor):
    os.close(anchor["descriptor"])
    os.close(anchor["parent_chain"][-1])


def _retained_prepared_transaction(path, *, exchanged=False):
    anchor, matches = _open_anchor(path)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    connection = transaction.open()
    connection.execute("INSERT INTO entries VALUES (2)")
    connection.commit()
    connection.close()
    transaction.connection = None
    os.fsync(transaction.work_descriptor)
    os.fsync(transaction.private_fd)
    transaction._write_prepared_receipt()
    transaction._close_source_reservation()
    private_name = transaction.private_name
    if exchanged:
        sqlite_atomic._rename_exchange(
            transaction.private_fd,
            "work.db",
            transaction.parent_fd,
            transaction.anchor["name"],
        )
        os.fsync(transaction.private_fd)
        os.fsync(transaction.parent_fd)
    os.close(transaction.receipt_descriptor)
    os.close(transaction.work_descriptor)
    os.close(transaction.private_fd)
    _close_anchor(anchor)
    return private_name


def _database_rows(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT value FROM entries ORDER BY value"
        ).fetchall()
    finally:
        connection.close()


def _hold_database_parent_exclusion(path, control):
    parent_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    exclusion = sqlite_atomic._SQLiteDatabaseExclusion()
    try:
        exclusion.acquire(parent_fd)
        control.send(("ready", None))
        control.recv()
    except BaseException as exc:
        control.send(("error", repr(exc)))
    finally:
        error = exclusion.release()
        os.close(parent_fd)
        try:
            control.send(("released", repr(error) if error else None))
        except (BrokenPipeError, EOFError, OSError):
            pass


def test_database_exclusion_blocks_before_recovery_and_nests_by_thread(
        tmp_path):
    database = tmp_path / "library.db"
    _seed_database(database)
    context = multiprocessing.get_context("fork")
    parent_control, child_control = context.Pipe()
    child = context.Process(
        target=_hold_database_parent_exclusion,
        args=(tmp_path, child_control),
    )
    child.start()
    child_control.close()
    anchor, matches = _open_anchor(database)
    prefix = sqlite_atomic._recovery_prefix(anchor)
    try:
        assert parent_control.recv() == ("ready", None)
        transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
        with pytest.raises(OSError) as blocked:
            transaction.open_source()
        assert blocked.value.errno == errno.EBUSY
        assert not any(
            entry.name.startswith(prefix) for entry in tmp_path.iterdir()
        )

        parent_control.send("release")
        assert parent_control.recv() == ("released", None)
        child.join(3)
        assert child.exitcode == 0

        exclusion = sqlite_atomic._SQLiteDatabaseExclusion()
        exclusion.acquire(anchor["parent_chain"][-1])
        try:
            assert sqlite_atomic.inspect_sqlite_source(
                anchor,
                matches,
                lambda connection: connection.execute(
                    "SELECT value FROM entries"
                ).fetchall(),
            ) == [(1,)]
        finally:
            assert exclusion.release() is None
        assert not any(
            entry.name.startswith(prefix) for entry in tmp_path.iterdir()
        )
    finally:
        parent_control.close()
        if child.is_alive():
            child.terminate()
            child.join(3)
        _close_anchor(anchor)


def test_transaction_close_retries_an_interrupted_database_unlock(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, matches = _open_anchor(database)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    connection = transaction.open()
    connection.execute("INSERT INTO entries VALUES (2)")
    transaction.commit_and_publish(lambda: matches(anchor))

    state = transaction._database_exclusion._state
    record = state.record
    descriptor = record["descriptor"]
    real_flock = sqlite_atomic.fcntl.flock
    interrupted = False

    def interrupt_first_unlock(current, operation):
        nonlocal interrupted
        if (
            current == descriptor
            and operation == sqlite_atomic.fcntl.LOCK_UN
            and not interrupted
        ):
            interrupted = True
            real_flock(current, operation)
            raise OSError(errno.EIO, "simulated lost unlock return")
        return real_flock(current, operation)

    monkeypatch.setattr(sqlite_atomic.fcntl, "flock", interrupt_first_unlock)

    def finish_successfully():
        try:
            return "success"
        finally:
            transaction.close()

    with pytest.raises(sqlite_atomic.SQLitePublicationUncertain) as failure:
        finish_successfully()
    assert failure.value.__cause__.errno == errno.EIO
    assert transaction.uncertain
    assert state.record is record
    assert state.reference in record["tokens"]
    assert record["transition_pending"]
    assert sqlite_atomic._SQLITE_DATABASE_EXCLUSIONS[record["identity"]] is record
    assert os.fstat(descriptor)

    transaction.close()
    assert state.record is None
    assert not record["tokens"]
    assert not record["locked"]
    assert not record["transition_pending"]

    reused = sqlite_atomic._SQLiteDatabaseExclusion()
    reused.acquire(anchor["parent_chain"][-1])
    assert reused._state.record is record
    assert reused.release() is None

    other_parent = tmp_path / "other-parent"
    other_parent.mkdir()
    other_parent_fd = os.open(
        other_parent, os.O_RDONLY | os.O_DIRECTORY)
    other = sqlite_atomic._SQLiteDatabaseExclusion()
    try:
        other.acquire(other_parent_fd)
        other_record = other._state.record
        assert other_record is not record
        assert record["identity"] not in sqlite_atomic._SQLITE_DATABASE_EXCLUSIONS
        assert record["descriptor"] is None
        assert other.release() is None
    finally:
        other.release()
        os.close(other_parent_fd)

    assert _database_rows(database) == [(1,), (2,)]
    _close_anchor(anchor)


def test_nested_database_exclusion_release_recovers_after_token_interrupt(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, _matches = _open_anchor(database)
    parent_fd = anchor["parent_chain"][-1]
    outer = sqlite_atomic._SQLiteDatabaseExclusion()
    inner = sqlite_atomic._SQLiteDatabaseExclusion()
    nested = sqlite_atomic._SQLiteDatabaseExclusion()

    class InterruptingTokens(set):
        interrupted = False

        def discard(self, reference):
            super().discard(reference)
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt("simulated interruption after discard")

    try:
        outer.acquire(parent_fd)
        inner.acquire(parent_fd)
        state = inner._state
        record = state.record
        record["tokens"] = InterruptingTokens(record["tokens"])

        assert isinstance(inner.release(), KeyboardInterrupt)
        assert state.reference not in record["tokens"]
        assert inner.release() is None
        assert state.record is None
        assert not record["transition_pending"]

        abandoned = sqlite_atomic._SQLiteDatabaseExclusion()
        record["transition_pending"] = abandoned._state.reference
        del abandoned
        gc.collect()

        nested.acquire(parent_fd)
        assert nested.release() is None

        inner.acquire(parent_fd)
        record["tokens"] = InterruptingTokens(record["tokens"])
        assert isinstance(inner.release(), KeyboardInterrupt)

        descriptor = record["descriptor"]
        real_flock = sqlite_atomic.fcntl.flock
        interrupted = False

        def interrupt_outer_unlock(current, operation):
            nonlocal interrupted
            if (
                current == descriptor
                and operation == sqlite_atomic.fcntl.LOCK_UN
                and not interrupted
            ):
                interrupted = True
                real_flock(current, operation)
                raise OSError(errno.EIO, "simulated lost unlock return")
            return real_flock(current, operation)

        monkeypatch.setattr(
            sqlite_atomic.fcntl, "flock", interrupt_outer_unlock)
        outer_error = outer.release()
        assert isinstance(outer_error, OSError)
        assert outer_error.errno == errno.EIO

        inner_retry = inner.release()
        assert isinstance(inner_retry, OSError)
        assert inner_retry.errno == errno.EBUSY
        assert record["transition_pending"]

        competitor_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            real_flock(
                competitor_fd,
                sqlite_atomic.fcntl.LOCK_EX | sqlite_atomic.fcntl.LOCK_NB,
            )
            with pytest.raises(OSError) as blocked:
                nested.acquire(parent_fd)
            assert blocked.value.errno == errno.EBUSY
        finally:
            real_flock(competitor_fd, sqlite_atomic.fcntl.LOCK_UN)
            os.close(competitor_fd)

        assert outer.release() is None
        assert inner.release() is None
        nested.acquire(parent_fd)
        assert nested.release() is None
    finally:
        inner.release()
        nested.release()
        outer.release()
        _close_anchor(anchor)


def test_transaction_close_preserves_cleanup_interrupt_over_active_error(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, matches = _open_anchor(database)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    cleanup_interrupt = KeyboardInterrupt("simulated cleanup interruption")
    monkeypatch.setattr(
        transaction._database_exclusion,
        "release",
        lambda: cleanup_interrupt,
    )

    body_error = OSError("simulated body failure")
    try:
        with pytest.raises(KeyboardInterrupt) as failure:
            try:
                raise body_error
            except BaseException:
                transaction.close()
                raise
        assert failure.value is cleanup_interrupt
        assert failure.value.__context__ is body_error
        assert transaction.uncertain
    finally:
        _close_anchor(anchor)


def test_recovery_cleans_prepared_original_state(tmp_path):
    tmp_path.chmod(stat.S_IMODE(tmp_path.stat().st_mode) | stat.S_ISGID)
    database = tmp_path / "library.db"
    _seed_database(database)
    private_name = _retained_prepared_transaction(database)

    anchor, matches = _open_anchor(database)
    try:
        assert sqlite_atomic.reconcile_atomic_sqlite(
            anchor, matches) == "original"
    finally:
        _close_anchor(anchor)

    assert _database_rows(database) == [(1,)]
    assert not (tmp_path / private_name).exists()


def test_recovery_cleans_exchanged_published_state(tmp_path):
    database = tmp_path / "library.db"
    _seed_database(database)
    private_name = _retained_prepared_transaction(
        database, exchanged=True)

    anchor, matches = _open_anchor(database)
    try:
        assert sqlite_atomic.reconcile_atomic_sqlite(
            anchor, matches) == "published"
    finally:
        _close_anchor(anchor)

    assert _database_rows(database) == [(1,), (2,)]
    assert not (tmp_path / private_name).exists()


def test_recovery_releases_leases_when_acquisition_is_interrupted(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    private_name = _retained_prepared_transaction(database)
    anchor, matches = _open_anchor(database)
    captured = {}
    real_acquire = sqlite_atomic._FileLeases.acquire

    def interrupted_acquire(owner):
        real_acquire(owner)
        captured["owner"] = owner
        raise KeyboardInterrupt

    monkeypatch.setattr(
        sqlite_atomic._FileLeases, "acquire", interrupted_acquire)
    try:
        with pytest.raises(KeyboardInterrupt):
            sqlite_atomic.reconcile_atomic_sqlite(anchor, matches)
        owner = captured["owner"]
        assert not owner._state.records
        assert owner._state.signal_reference is None
        assert (tmp_path / private_name).exists()
    finally:
        owner = captured.get("owner")
        if owner is not None:
            owner.release()
        _close_anchor(anchor)


def test_file_lease_cross_thread_release_retries_lost_returns(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, _matches = _open_anchor(database)
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    owner = sqlite_atomic._FileLeases()
    owner.add(
        anchor["descriptor"],
        sqlite_atomic.fcntl.F_RDLCK,
        sqlite_atomic._SOURCE_LEASE,
    )
    owner.acquire()

    reader_finished = threading.Event()

    def open_reader():
        try:
            descriptor = os.open(database, os.O_RDWR)
            os.close(descriptor)
        finally:
            reader_finished.set()

    reader = threading.Thread(target=open_reader)
    reader.start()
    deadline = time.monotonic() + 2
    while not owner.pending(sqlite_atomic._SOURCE_LEASE) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert owner.pending(sqlite_atomic._SOURCE_LEASE)
    assert not reader_finished.is_set()

    real_fcntl = sqlite_atomic.fcntl.fcntl
    signal_reference = owner._state.signal_reference
    real_reference_close = signal_reference.close
    failures = {"unlock": True, "reference": True}

    def interrupted_fcntl(descriptor, command, *args):
        if (
            command == sqlite_atomic.fcntl.F_SETLEASE
            and args == (sqlite_atomic.fcntl.F_UNLCK,)
            and failures["unlock"]
        ):
            failures["unlock"] = False
            assert real_fcntl(descriptor, command, *args) == 0
            raise KeyboardInterrupt
        return real_fcntl(descriptor, command, *args)

    def interrupted_reference_close():
        if failures["reference"]:
            failures["reference"] = False
            real_reference_close()
            raise KeyboardInterrupt
        return real_reference_close()

    monkeypatch.setattr(sqlite_atomic.fcntl, "fcntl", interrupted_fcntl)
    monkeypatch.setattr(signal_reference, "close", interrupted_reference_close)
    releases = []

    def release_from_worker():
        releases.extend(owner.release() for _attempt in range(3))

    worker = threading.Thread(target=release_from_worker)
    try:
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
        assert isinstance(releases[0], KeyboardInterrupt)
        assert reader_finished.wait(3)
        assert isinstance(releases[1], KeyboardInterrupt)
        assert releases[2] is None
        assert not owner._state.records
        assert owner._state.signal_reference is None
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == original_mask
    finally:
        owner.release()
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
        _close_anchor(anchor)
        reader.join(3)


@pytest.mark.parametrize(
    "ambiguous", ["malformed", "multiple", "private-journal"])
def test_recovery_preserves_ambiguous_state(tmp_path, ambiguous):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, matches = _open_anchor(database)
    prefix = sqlite_atomic._recovery_prefix(anchor)
    names = [f"{prefix}one"]
    if ambiguous == "multiple":
        names.append(f"{prefix}two")
    try:
        for name in names:
            os.mkdir(name, 0o700, dir_fd=anchor["parent_chain"][-1])
            private_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=anchor["parent_chain"][-1],
            )
            os.link(
                database.name,
                "source.db",
                src_dir_fd=anchor["parent_chain"][-1],
                dst_dir_fd=private_fd,
            )
            if ambiguous == "malformed":
                receipt = os.open(
                    sqlite_atomic._RECEIPT_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=private_fd,
                )
                os.write(receipt, b"not a receipt")
                os.close(receipt)
            elif ambiguous == "private-journal":
                journal = os.open(
                    "source.db-journal",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=private_fd,
                )
                os.write(journal, b"retained")
                os.close(journal)
            os.close(private_fd)

        with pytest.raises(sqlite_atomic.SQLiteRecoveryRequired):
            sqlite_atomic.reconcile_atomic_sqlite(anchor, matches)
        for name in names:
            assert (tmp_path / name / "source.db").exists()
    finally:
        _close_anchor(anchor)


def test_recovery_refuses_a_hot_canonical_journal(tmp_path):
    database = tmp_path / "library.db"
    _seed_database(database)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=DELETE")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE entries SET value = 2")
    assert (tmp_path / "library.db-journal").exists()
    anchor, matches = _open_anchor(database)
    try:
        with pytest.raises(sqlite_atomic.SQLiteRecoveryRequired):
            sqlite_atomic.reconcile_atomic_sqlite(anchor, matches)
        assert (tmp_path / "library.db-journal").exists()
    finally:
        _close_anchor(anchor)
        writer.rollback()
        writer.close()


def test_recovery_refuses_a_foreign_valid_canonical_database(tmp_path):
    database = tmp_path / "library.db"
    _seed_database(database)
    private_name = _retained_prepared_transaction(database)
    foreign = tmp_path / "foreign.db"
    _seed_database(foreign)
    connection = sqlite3.connect(foreign)
    connection.execute("UPDATE entries SET value = 99")
    connection.commit()
    connection.close()
    os.replace(foreign, database)

    anchor, matches = _open_anchor(database)
    try:
        with pytest.raises(sqlite_atomic.SQLiteRecoveryRequired):
            sqlite_atomic.reconcile_atomic_sqlite(anchor, matches)
    finally:
        _close_anchor(anchor)

    assert _database_rows(database) == [(99,)]
    assert (tmp_path / private_name).exists()


def test_old_sqlite_waiter_is_rejected_as_moved(tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, matches = _open_anchor(database)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    real_exchange = sqlite_atomic._rename_exchange
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def waiting_writer():
        started.set()
        try:
            connection = sqlite3.connect(database, timeout=2)
            try:
                connection.execute("INSERT INTO entries VALUES (99)")
                connection.commit()
                outcome["accepted"] = True
            except sqlite3.Error as exc:
                outcome["code"] = getattr(exc, "sqlite_errorcode", None)
            finally:
                connection.close()
        finally:
            finished.set()

    waiter = None

    def exchange_with_old_waiter(*args):
        nonlocal waiter
        waiter = threading.Thread(target=waiting_writer)
        waiter.start()
        assert started.wait(1)
        deadline = time.monotonic() + 2
        while (
            sqlite_atomic.fcntl.fcntl(
                transaction.original_descriptor,
                sqlite_atomic.fcntl.F_GETLEASE,
            ) == sqlite_atomic.fcntl.F_RDLCK
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert sqlite_atomic.fcntl.fcntl(
            transaction.original_descriptor,
            sqlite_atomic.fcntl.F_GETLEASE,
        ) != sqlite_atomic.fcntl.F_RDLCK
        return real_exchange(*args)

    monkeypatch.setattr(
        sqlite_atomic, "_rename_exchange", exchange_with_old_waiter)
    try:
        connection = transaction.open()
        connection.execute("INSERT INTO entries VALUES (2)")
        transaction.commit_and_publish(lambda: matches(anchor))
    finally:
        transaction.close()
    assert finished.wait(3)
    waiter.join()
    assert outcome == {"code": sqlite3.SQLITE_READONLY_DBMOVED}
    assert _database_rows(database) == [(1,), (2,)]
    _close_anchor(anchor)


def test_fresh_canonical_reader_waits_for_classification(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, matches = _open_anchor(database)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    real_exchange = sqlite_atomic._rename_exchange
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def fresh_reader():
        started.set()
        try:
            outcome["rows"] = _database_rows(database)
        finally:
            finished.set()

    reader = None

    def exchange_with_fresh_reader(*args):
        nonlocal reader
        real_exchange(*args)
        reader = threading.Thread(target=fresh_reader)
        reader.start()
        assert started.wait(1)
        deadline = time.monotonic() + 2
        while (
            sqlite_atomic.fcntl.fcntl(
                transaction.work_descriptor,
                sqlite_atomic.fcntl.F_GETLEASE,
            ) == sqlite_atomic.fcntl.F_WRLCK
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert sqlite_atomic.fcntl.fcntl(
            transaction.work_descriptor,
            sqlite_atomic.fcntl.F_GETLEASE,
        ) != sqlite_atomic.fcntl.F_WRLCK
        assert not finished.is_set()

    monkeypatch.setattr(
        sqlite_atomic, "_rename_exchange", exchange_with_fresh_reader)
    try:
        connection = transaction.open()
        connection.execute("INSERT INTO entries VALUES (2)")
        transaction.commit_and_publish(lambda: matches(anchor))
    finally:
        transaction.close()
    assert finished.wait(3)
    reader.join()
    assert outcome == {"rows": [(1,), (2,)]}
    _close_anchor(anchor)


def test_base_exception_after_exchange_preserves_published_database(
        tmp_path, monkeypatch):
    database = tmp_path / "library.db"
    _seed_database(database)
    anchor, matches = _open_anchor(database)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    real_exchange = sqlite_atomic._rename_exchange

    def interrupted_exchange(*args):
        real_exchange(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        sqlite_atomic, "_rename_exchange", interrupted_exchange)
    try:
        connection = transaction.open()
        connection.execute("INSERT INTO entries VALUES (2)")
        with pytest.raises(KeyboardInterrupt):
            transaction.commit_and_publish(lambda: matches(anchor))
        assert transaction.published
        assert transaction.durable
        assert not transaction.uncertain
    finally:
        transaction.close()

    assert _database_rows(database) == [(1,), (2,)]
    _close_anchor(anchor)


def test_anchor_assignment_interruption_is_recognized_without_rewriting(
        tmp_path):
    database = tmp_path / "library.db"
    _seed_database(database)
    plain_anchor, matches = _open_anchor(database)

    class InterruptingAnchor(dict):
        interrupt_adoption = False
        interrupted_stores = 0

        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "descriptor" and self.interrupt_adoption:
                self.interrupted_stores += 1
                raise KeyboardInterrupt

    anchor = InterruptingAnchor(plain_anchor)
    transaction = sqlite_atomic.AtomicSQLiteWrite(anchor, matches)
    try:
        connection = transaction.open()
        connection.execute("INSERT INTO entries VALUES (2)")
        anchor.interrupt_adoption = True
        with pytest.raises(KeyboardInterrupt):
            transaction.commit_and_publish(lambda: matches(anchor))
        assert transaction.published
        assert anchor.interrupted_stores == 1
        assert transaction.published_descriptor is None
        assert transaction.durable
        assert not transaction.uncertain
    finally:
        anchor.interrupt_adoption = False
        if (
            transaction.published_descriptor is not None
            and anchor.get("descriptor") == transaction.published_descriptor
        ):
            transaction._adopt_published_anchor()
        transaction.close()

    assert _database_rows(database) == [(1,), (2,)]
    _close_anchor(anchor)
