import os
import sqlite3
import stat

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


