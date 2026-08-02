"""Write-through SQLite persistence for the job registry.

Without this, the registry + work queues in ``jobs.py`` are in-memory only:
a container restart (compose update, OOM, host reboot) silently drops every
queued/running download (orphaning staging) and throws away a completed
scan's AWAITING_REVIEW candidates — minutes of API work gone, the user
re-scans from artist #1.

With this module:

* Job state is mirrored into ``DATA_DIR/jobs.db`` on every meaningful
  transition (add, RUNNING start, AWAITING_REVIEW, approve, terminal).
* On startup, ``restore`` reloads the rows into the registry:
  - DONE / FAILED / CANCELED come back as historical entries browsable
    in the History view (see ``history_page`` / ``history_count``).
  - AWAITING_REVIEW comes back with candidates intact so the user can
    still approve. The execute function is resolved from ``execute_kind``
    via a lookup table the caller provides — closures aren't serialisable.
  - PENDING / RUNNING from the prior session are marked
    FAILED("interrupted on restart — submit again") so the user sees
    them rather than them silently vanishing.

Log lines and live progress are NOT persisted: too chatty (a long walk
logs thousands of lines) and not load-bearing for resume. A reloaded
job's log starts empty with one explanatory line.

If the SQLite file can't be opened (read-only volume, disk full), ordinary
history updates degrade to a no-op.  Job admission is stricter: work that can
change the library is not allowed to enter a worker unless its owner row is
durable first.
"""
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Optional

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import RecoveryOwner, normalise_album_id

_log = logging.getLogger("qobuz_librarian")
_lock = threading.Lock()
_disabled = False
_conn: Optional[sqlite3.Connection] = None
# The db opened fine but a write later failed (typically a full disk).
_warned_write_failure = False


@dataclass(frozen=True)
class RecoveryResolutionPlan:
    """Exact jobs.db snapshots to retire after one proved manual restore."""

    location: str
    receipt: dict | None
    rows: tuple[tuple[str, str], ...]


_RECOVERY_FIELDS = {
    "version", "kind", "status", "location", "album_dir", "stage",
    "reason", "complete", "requested", "backed_up", "receipt",
}


def _valid_repair_recovery(record) -> bool:
    return (
        type(record) is dict
        and set(record) == _RECOVERY_FIELDS
        and record.get("version") == 1
        and record.get("kind") == "repair-backup"
        and record.get("status") == "retained"
        and isinstance(record.get("location"), str)
        and bool(record["location"])
        and isinstance(record.get("album_dir"), str)
        and bool(record["album_dir"])
        and isinstance(record.get("stage"), str)
        and isinstance(record.get("reason"), str)
        and type(record.get("complete")) is bool
        and type(record.get("requested")) is int
        and type(record.get("backed_up")) is int
        and 0 <= record["backed_up"] <= record["requested"]
        and (record.get("receipt") is None
             or type(record.get("receipt")) is dict)
    )


def _decode_recoveries(value) -> tuple[list[dict], bool]:
    """Return validated recovery records and whether the payload is unreadable."""
    try:
        recoveries = json.loads(value or "[]")
    except (TypeError, ValueError):
        return [], True
    if (
        type(recoveries) is not list
        or any(not _valid_repair_recovery(record) for record in recoveries)
    ):
        return [], True
    return recoveries, False


def _note_write_failure(what: str, e: Exception) -> None:
    global _warned_write_failure
    if not _warned_write_failure:
        _warned_write_failure = True
        _log.info("job persistence write failed (%s); jobs may not survive a "
                  "restart until the volume recovers: %s", what, e)
    else:
        _log.debug("%s failed: %s", what, e)


def _path():
    return cfg.DATA_DIR / "jobs.db"


def _get_conn() -> Optional[sqlite3.Connection]:
    """Return the persistent WAL connection, opening it on first call.

    Returns None and disables further attempts when the volume isn't
    writable — the in-memory registry is still correct, the user just
    forgoes restart durability rather than seeing a stream of OSError
    on every status change.
    """
    global _disabled, _conn
    if _disabled:
        return None
    if _conn is not None:
        return _conn
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_path()), timeout=5.0,
                                check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        return _conn
    except (OSError, sqlite3.Error) as e:
        _log.info("job persistence disabled (%s); jobs won't survive restart.", e)
        _disabled = True
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    artist        TEXT NOT NULL DEFAULT '',
    album_id      TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'download',
    status        TEXT NOT NULL,
    phase         TEXT NOT NULL DEFAULT '',
    candidates    TEXT NOT NULL DEFAULT '[]',
    error         TEXT,
    summary       TEXT NOT NULL DEFAULT '',
    review_verb   TEXT NOT NULL DEFAULT 'Download',
    execute_kind  TEXT NOT NULL DEFAULT '',
    execute_args  TEXT NOT NULL DEFAULT '{}',
    created_at    REAL,
    finished_at   REAL,
    single        TEXT NOT NULL DEFAULT '{}',
    attention     TEXT NOT NULL DEFAULT '',
    recoveries    TEXT NOT NULL DEFAULT '[]'
)
"""

_DURABLE_COMPLETION_SCHEMA = """
CREATE TABLE IF NOT EXISTS durable_job_completions (
    job_id        TEXT NOT NULL,
    operation_id  TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    job_created_at REAL NOT NULL,
    album_id      TEXT NOT NULL,
    completion_hash TEXT NOT NULL,
    acknowledged_at REAL NOT NULL,
    PRIMARY KEY (job_id, operation_id, item_id)
)
"""

_POST_IMPORT_RELOCATION_HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS post_import_relocation_handoffs (
    job_id         TEXT NOT NULL,
    operation_id   TEXT NOT NULL UNIQUE,
    job_created_at REAL NOT NULL,
    album_id       TEXT NOT NULL,
    handoff_hash   TEXT NOT NULL,
    acknowledged_at REAL NOT NULL
)
"""


_MIGRATION_CANDIDATE_PAYLOAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_candidate_payloads (
    job_id                     TEXT NOT NULL,
    candidate_id               TEXT NOT NULL,
    source_root                TEXT,
    source_root_receipt        TEXT NOT NULL,
    dest_root_receipt          TEXT NOT NULL,
    destination_name_semantics TEXT NOT NULL,
    manifest_artifact          TEXT NOT NULL,
    PRIMARY KEY (job_id, candidate_id)
)
"""


_MIGRATION_REVIEW_ARTIFACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_review_artifacts (
    job_id            TEXT PRIMARY KEY,
    manifest_artifact TEXT NOT NULL
)
"""


_MIGRATION_CANDIDATE_ENTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_candidate_entries (
    job_id              TEXT NOT NULL,
    candidate_id        TEXT NOT NULL,
    entry_kind          TEXT NOT NULL
                        CHECK (entry_kind IN (
                            'entry', 'resume', 'companion', 'release_identity'
                        )),
    ordinal             INTEGER NOT NULL CHECK (ordinal >= 0),
    source              TEXT,
    destination         TEXT,
    source_receipt      TEXT NOT NULL,
    destination_receipt TEXT,
    PRIMARY KEY (job_id, candidate_id, entry_kind, ordinal)
)
"""


_SCHEMA_VERSION = 7


def _table_schema(conn, name: str):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return None if row is None else (row[0] or "")


def _upgrade_schema_transaction(conn, version: int) -> None:
    """Atomically upgrade jobs.db and recover an interrupted v6 rebuild."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if version < _SCHEMA_VERSION:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            if "single" not in cols:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN single TEXT NOT NULL "
                    "DEFAULT '{}'"
                )
            if "attention" not in cols:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN attention TEXT NOT NULL "
                    "DEFAULT ''"
                )
            if "recoveries" not in cols:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN recoveries TEXT NOT NULL "
                    "DEFAULT '[]'"
                )

        current_schema = _table_schema(
            conn, "migration_candidate_entries")
        leftover_schema = _table_schema(
            conn, "migration_candidate_entries_v6")
        if leftover_schema is not None:
            if current_schema is None:
                conn.execute(_MIGRATION_CANDIDATE_ENTRY_SCHEMA)
                current_schema = _table_schema(
                    conn, "migration_candidate_entries")
            if "release_identity" not in (current_schema or ""):
                raise sqlite3.DatabaseError(
                    "cannot safely recover migration candidate entry schema"
                )
            conn.execute(
                "INSERT OR IGNORE INTO migration_candidate_entries "
                "SELECT * FROM migration_candidate_entries_v6"
            )
            conn.execute("DROP TABLE migration_candidate_entries_v6")
        elif (
            current_schema is not None
            and "release_identity" not in current_schema
        ):
            conn.execute(
                "ALTER TABLE migration_candidate_entries "
                "RENAME TO migration_candidate_entries_v6"
            )
            conn.execute(_MIGRATION_CANDIDATE_ENTRY_SCHEMA)
            conn.execute(
                "INSERT INTO migration_candidate_entries "
                "SELECT * FROM migration_candidate_entries_v6"
            )
            conn.execute("DROP TABLE migration_candidate_entries_v6")
        elif current_schema is None:
            conn.execute(_MIGRATION_CANDIDATE_ENTRY_SCHEMA)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_migration_candidate_entries "
            "ON migration_candidate_entries(job_id, candidate_id, "
            "entry_kind, ordinal)"
        )
        if version < _SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def init() -> None:
    """Create the schema (and run additive migrations). Safe to call repeatedly."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(_SCHEMA)
            conn.execute(_DURABLE_COMPLETION_SCHEMA)
            conn.execute(_POST_IMPORT_RELOCATION_HANDOFF_SCHEMA)
            conn.execute(_MIGRATION_REVIEW_ARTIFACT_SCHEMA)
            conn.execute(_MIGRATION_CANDIDATE_PAYLOAD_SCHEMA)
            conn.execute(_MIGRATION_CANDIDATE_ENTRY_SCHEMA)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_durable_job_completion_album "
                "ON durable_job_completions(job_id, job_created_at, album_id)"
            )
            # Terminal-row index: history_count() / history_page() /
            # prune_finished() all filter on status and order by finished_at.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_terminal "
                "ON jobs(status, finished_at, created_at)"
            )
            # Schema versioning so a FUTURE column addition can ALTER TABLE
            # instead of silently failing every persist() against an old
            # jobs.db — that failure is swallowed by _note_write_failure,
            # leaving the archive non-durable with no visible sign.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if (
                version < _SCHEMA_VERSION
                or _table_schema(
                    conn, "migration_candidate_entries_v6") is not None
            ):
                _upgrade_schema_transaction(conn, version)
            else:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_migration_candidate_entries "
                    "ON migration_candidate_entries(job_id, candidate_id, "
                    "entry_kind, ordinal)"
                )
            conn.commit()
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.rollback()
            # A transient/locked/full/corrupt jobs.db here would otherwise
            # propagate out of restore_jobs() into the caller's broad
            # "couldn't restore prior jobs — starting fresh" handler, masking
            # a recoverable condition and then leaving every later persist()
            # silently non- durable.
            _log.warning("job persistence schema/migration failed; running "
                         "without crash durability until the volume recovers "
                         "and the app restarts: %s", e)


_PERSIST_SQL = (
    "INSERT OR REPLACE INTO jobs "
    "(id, title, artist, album_id, kind, status, phase, candidates, "
    " error, summary, review_verb, execute_kind, execute_args, "
    " created_at, finished_at, single, attention, recoveries) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_CURRENT_JOB_SINGLE = object()


def _job_values(job, *, single=_CURRENT_JOB_SINGLE):
    """Serialise one job while its lock is held, or return None."""
    try:
        candidates_json = json.dumps(job.candidates or [], default=str)
        execute_args_json = json.dumps(job.execute_args or {}, default=str)
        single_value = job.single if single is _CURRENT_JOB_SINGLE else single
        single_json = json.dumps(single_value or {}, default=str)
        # Recovery receipts are a safety boundary.
        recoveries_json = json.dumps(
            getattr(job, "recoveries", []) or [],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as e:
        _note_write_failure(f"serialize recoveries for {job.id}", e)
        return None
    return (
        job.id, job.title or "", job.artist or "",
        job.album_id or "", job.kind or "download",
        job.status.value if hasattr(job.status, "value") else str(job.status),
        job.phase or "",
        candidates_json,
        job.error,
        job.summary or "",
        job.review_verb or "Download",
        job.execute_kind or "",
        execute_args_json,
        job.created_at,
        job.finished_at,
        single_json,
        getattr(job, "attention", "") or "",
        recoveries_json,
    )


def _persist_locked(job) -> bool:
    """Write one ordinary snapshot while the caller holds ``job._lock``."""
    values = _job_values(job)
    if values is None:
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute(_PERSIST_SQL, values)
            conn.commit()
            return True
        except sqlite3.Error as e:
            _note_write_failure(f"persist {job.id}", e)
            return False


def persist(job) -> bool:
    """Write the job's current state to disk. Return whether it reached disk."""
    # Keep the job lock through both the preservation decision and the
    # database commit.
    with job._lock:
        if getattr(job, "_preserve_persisted_single", False) is True:
            return _persist_preserving_single_locked(job)
        return _persist_locked(job)


def _persist_preserving_single_locked(job) -> bool:
    """Save while preserving Undo; the caller holds ``job._lock``."""
    values = _job_values(job)
    if values is None:
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            job._single_undo_unavailable = True
            return False
        try:
            updated = conn.execute(
                "UPDATE jobs SET title=?, artist=?, kind=?, status=?, "
                "phase=?, candidates=?, error=?, summary=?, "
                "review_verb=?, execute_kind=?, execute_args=?, "
                "finished_at=?, attention=?, recoveries=? "
                "WHERE id=? AND created_at=? AND album_id=?",
                (
                    values[1], values[2], values[4], values[5], values[6],
                    values[7], values[8], values[9], values[10], values[11],
                    values[12], values[14], values[16], values[17], values[0],
                    values[13], values[3],
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                job._single_undo_unavailable = True
                return False
            row = conn.execute(
                "SELECT single FROM jobs "
                "WHERE id=? AND created_at=? AND album_id=?",
                (values[0], values[13], values[3]),
            ).fetchone()
            try:
                durable_single = json.loads(row[0])
            except (IndexError, TypeError, ValueError):
                durable_single = None
            conn.commit()
            if type(durable_single) is dict:
                job.single = durable_single
                job.__dict__.pop("_single_undo_unavailable", None)
            else:
                job.single = {}
                job._single_undo_unavailable = True
            return True
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            _note_write_failure(
                f"persist {job.id} while preserving its Undo record",
                exc,
            )
            job._single_undo_unavailable = True
            return False


def persist_preserving_single(job) -> bool:
    """Save job state without changing an indeterminate durable Undo record."""
    with job._lock:
        return _persist_preserving_single_locked(job)


_MIGRATION_PAYLOAD_KEYS = {
    "entries", "resume_entries", "source_root", "source_root_receipt",
    "dest_root_receipt", "destination_name_semantics", "manifest_artifact",
    "companion_receipts", "release_identities",
}


def migration_payload_reference() -> dict:
    """Return a fresh compact reference for one durable migration payload."""
    return {"migration_payload_ref": {"version": 1}}


_MIGRATION_ARTIFACT_REFERENCE = {
    "migration_artifact_ref": {"version": 1},
}


def _migration_json_dump(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _migration_json_load(value):
    if type(value) is not str:
        raise TypeError("migration payload JSON is not text")
    return json.loads(value)


def _migration_payload_rows(payload: dict) -> tuple[tuple, list[tuple]]:
    if type(payload) is not dict or set(payload) != _MIGRATION_PAYLOAD_KEYS:
        raise ValueError("migration payload fields are malformed")
    source_root = payload["source_root"]
    if source_root is not None and type(source_root) is not str:
        raise TypeError("migration source root is malformed")
    parent = (
        source_root,
        _migration_json_dump(payload["source_root_receipt"]),
        _migration_json_dump(payload["dest_root_receipt"]),
        _migration_json_dump(payload["destination_name_semantics"]),
        _migration_json_dump(payload["manifest_artifact"]),
    )
    rows = []
    for key, kind in (("entries", "entry"),
                      ("resume_entries", "resume")):
        values = payload[key]
        if type(values) not in (list, tuple):
            raise TypeError(f"migration {key} are malformed")
        for ordinal, entry in enumerate(values):
            if type(entry) not in (list, tuple) or len(entry) != 4:
                raise ValueError(f"migration {kind} row is malformed")
            source, destination, source_receipt, destination_receipt = entry
            if type(source) is not str or type(destination) is not str:
                raise TypeError(f"migration {kind} paths are malformed")
            rows.append((
                kind, ordinal, source, destination,
                _migration_json_dump(source_receipt),
                _migration_json_dump(destination_receipt),
            ))
    companions = payload["companion_receipts"]
    if type(companions) not in (list, tuple):
        raise TypeError("migration companion receipts are malformed")
    for ordinal, receipt in enumerate(companions):
        rows.append((
            "companion", ordinal, None, None,
            _migration_json_dump(receipt), None,
        ))
    release_identities = payload["release_identities"]
    if type(release_identities) not in (list, tuple):
        raise TypeError("migration release identities are malformed")
    for ordinal, record in enumerate(release_identities):
        rows.append((
            "release_identity", ordinal, None, None,
            _migration_json_dump(record), None,
        ))
    return parent, rows


def persist_migration_candidate_payload(
        job_id: str, candidate_id: str, payload: dict) -> bool:
    """Durably replace one migration candidate's normalized safety payload."""
    if not job_id or type(job_id) is not str:
        return False
    if not candidate_id or type(candidate_id) is not str:
        return False
    try:
        parent, rows = _migration_payload_rows(payload)
    except (KeyError, TypeError, ValueError) as exc:
        _note_write_failure("serialize migration candidate payload", exc)
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            artifact_json = parent[-1]
            existing_artifact = conn.execute(
                "SELECT manifest_artifact FROM migration_review_artifacts "
                "WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing_artifact is None:
                conn.execute(
                    "INSERT INTO migration_review_artifacts "
                    "(job_id, manifest_artifact) VALUES (?,?)",
                    (job_id, artifact_json),
                )
            elif existing_artifact[0] != artifact_json:
                raise ValueError(
                    "migration candidates have inconsistent preview artifacts"
                )
            conn.execute(
                "DELETE FROM migration_candidate_entries "
                "WHERE job_id=? AND candidate_id=?",
                (job_id, candidate_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO migration_candidate_payloads "
                "(job_id, candidate_id, source_root, source_root_receipt, "
                "dest_root_receipt, destination_name_semantics, "
                "manifest_artifact) VALUES (?,?,?,?,?,?,?)",
                (
                    job_id, candidate_id, *parent[:-1],
                    _migration_json_dump(_MIGRATION_ARTIFACT_REFERENCE),
                ),
            )
            conn.executemany(
                "INSERT INTO migration_candidate_entries "
                "(job_id, candidate_id, entry_kind, ordinal, source, "
                "destination, source_receipt, destination_receipt) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(job_id, candidate_id, *row) for row in rows],
            )
            conn.commit()
            return True
        except (OverflowError, ValueError, sqlite3.Error) as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            _note_write_failure("persist migration candidate payload", exc)
            return False


_UNSET_MIGRATION_ARTIFACT = object()


def load_migration_candidate_payload(
        job_id: str, candidate_id: str, *,
        _shared_artifact=_UNSET_MIGRATION_ARTIFACT) -> dict | None:
    """Load and validate one normalized migration payload by exact owner."""
    if not job_id or type(job_id) is not str:
        return None
    if not candidate_id or type(candidate_id) is not str:
        return None
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            parent = conn.execute(
                "SELECT source_root, source_root_receipt, dest_root_receipt, "
                "destination_name_semantics, manifest_artifact "
                "FROM migration_candidate_payloads "
                "WHERE job_id=? AND candidate_id=?",
                (job_id, candidate_id),
            ).fetchone()
            rows = conn.execute(
                "SELECT entry_kind, ordinal, source, destination, "
                "source_receipt, destination_receipt "
                "FROM migration_candidate_entries "
                "WHERE job_id=? AND candidate_id=? "
                "ORDER BY entry_kind, ordinal",
                (job_id, candidate_id),
            ).fetchall()
        except sqlite3.Error:
            return None
    if parent is None:
        return None
    try:
        source_root = parent[0]
        if source_root is not None and type(source_root) is not str:
            return None
        stored_artifact = _migration_json_load(parent[4])
        if stored_artifact == _MIGRATION_ARTIFACT_REFERENCE:
            if _shared_artifact is _UNSET_MIGRATION_ARTIFACT:
                with _lock:
                    conn = _get_conn()
                    if conn is None:
                        return None
                    artifact_row = conn.execute(
                        "SELECT manifest_artifact "
                        "FROM migration_review_artifacts WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                if artifact_row is None:
                    return None
                _shared_artifact = _migration_json_load(artifact_row[0])
            if type(_shared_artifact) is not dict:
                return None
            manifest_artifact = _shared_artifact
        else:
            manifest_artifact = stored_artifact
        payload = {
            "entries": [],
            "resume_entries": [],
            "source_root": source_root,
            "source_root_receipt": _migration_json_load(parent[1]),
            "dest_root_receipt": _migration_json_load(parent[2]),
            "destination_name_semantics": _migration_json_load(parent[3]),
            "manifest_artifact": manifest_artifact,
            "companion_receipts": [],
            "release_identities": [],
        }
        expected = {
            "entry": 0,
            "resume": 0,
            "companion": 0,
            "release_identity": 0,
        }
        for kind, ordinal, source, destination, source_json, dest_json in rows:
            if kind not in expected or ordinal != expected[kind]:
                return None
            expected[kind] += 1
            source_receipt = _migration_json_load(source_json)
            if kind in {"companion", "release_identity"}:
                if source is not None or destination is not None or dest_json is not None:
                    return None
                target = (
                    "companion_receipts"
                    if kind == "companion"
                    else "release_identities"
                )
                payload[target].append(source_receipt)
                continue
            if type(source) is not str or type(destination) is not str:
                return None
            destination_receipt = _migration_json_load(dest_json)
            target = "entries" if kind == "entry" else "resume_entries"
            payload[target].append([
                source, destination, source_receipt, destination_receipt,
            ])
        return payload
    except (TypeError, ValueError):
        return None


def resolve_migration_candidates(
        job_id: str, candidates: list[dict]) -> list[dict] | None:
    """Hydrate one selected review format without trusting payload ownership."""
    if type(job_id) is not str or not job_id or type(candidates) is not list:
        return None
    formats = []
    for candidate in candidates:
        if type(candidate) is not dict or type(candidate.get("payload")) is not dict:
            return None
        payload = candidate["payload"]
        reference = payload.get("migration_payload_ref")
        if reference is None:
            formats.append("inline")
        elif (
            set(payload) == {"migration_payload_ref"}
            and type(reference) is dict
            and set(reference) == {"version"}
            and reference.get("version") == 1
        ):
            formats.append("reference")
        else:
            return None
    if len(set(formats)) > 1:
        return None
    if not formats or formats[0] == "inline":
        return [dict(candidate) for candidate in candidates]
    shared_artifact = _UNSET_MIGRATION_ARTIFACT
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            artifact_row = conn.execute(
                "SELECT manifest_artifact FROM migration_review_artifacts "
                "WHERE job_id=?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
    if artifact_row is not None:
        try:
            shared_artifact = _migration_json_load(artifact_row[0])
        except (TypeError, ValueError):
            return None
    resolved = []
    for candidate in candidates:
        candidate_id = candidate.get("cid")
        if type(candidate_id) is not str or not candidate_id:
            return None
        payload = load_migration_candidate_payload(
            job_id, candidate_id, _shared_artifact=shared_artifact,
        )
        if payload is None:
            return None
        hydrated = dict(candidate)
        hydrated["payload"] = payload
        resolved.append(hydrated)
    return resolved


def delete_migration_payloads(job_id: str) -> None:
    """Best-effort removal of every migration payload owned by one job."""
    if type(job_id) is not str or not job_id:
        return
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "DELETE FROM migration_candidate_entries WHERE job_id=?",
                (job_id,),
            )
            conn.execute(
                "DELETE FROM migration_candidate_payloads WHERE job_id=?",
                (job_id,),
            )
            conn.execute(
                "DELETE FROM migration_review_artifacts WHERE job_id=?",
                (job_id,),
            )
            conn.commit()
        except sqlite3.Error:
            pass


def admit(job) -> bool:
    """Durably save a job before it may enter or start on a worker lane.

    This named boundary distinguishes mandatory admission from later history
    updates, whose callers may still degrade to the in-memory view.
    """
    return persist(job)


def admit_review_transition(job, parked_jobs=()) -> bool:
    """Atomically save one locked approval and its unpublished remnants.

    The caller holds ``job._lock`` across the state change and this call. Each
    parked job is new and cannot yet be reached by another thread. Keeping that
    lock through the commit prevents Cancel from observing PENDING before the
    durable owner row exists.
    """
    parked = sorted(
        {other.id: other for other in parked_jobs}.values(), key=lambda j: j.id
    )
    with ExitStack() as locks:
        for other in parked:
            locks.enter_context(other._lock)
        ordered = [job, *parked]
        values = [_job_values(other) for other in ordered]
        if any(value is None for value in values):
            return False
        with _lock:
            conn = _get_conn()
            if conn is None:
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(_PERSIST_SQL, values)
                conn.commit()
                return True
            except sqlite3.Error as e:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                _note_write_failure("admit related jobs", e)
                return False


def persist_recoveries(job) -> bool:
    """Durably replace only a job's exact Repair-recovery state.

    The Repair safety gate calls this before it starts downloading. Requiring
    an existing job row makes a lost initial job save fail closed instead of
    manufacturing a partial row that cannot describe the running operation.
    """
    with job._lock:
        try:
            recoveries_json = json.dumps(
                getattr(job, "recoveries", []) or [],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            job_id = job.id
            attention = getattr(job, "attention", "") or ""
        except (TypeError, ValueError) as exc:
            _note_write_failure(
                f"serialize Repair recovery for {getattr(job, 'id', '?')}",
                exc,
            )
            return False

        # Match persist(): the job lock stays held through the database commit
        # so this narrow writer cannot replay an older recovery snapshot after
        # manual Restore has retired it.
        with _lock:
            conn = _get_conn()
            if conn is None:
                return False
            try:
                changed = conn.execute(
                    "UPDATE jobs SET recoveries=?, attention=? WHERE id=?",
                    (recoveries_json, attention, job_id),
                )
                if changed.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except sqlite3.Error as exc:
                _note_write_failure(
                    f"persist Repair recovery for {job_id}", exc)
                return False


def acknowledge_durable_completion(
    job_id: str,
    owner: RecoveryOwner,
    *,
    album_id: str,
    completion_hash: str,
) -> bool:
    """Record one exact queue completion before its journal proof is removed.

    This lives outside the mutable Job snapshot so a delayed ordinary status
    save cannot erase the acknowledgement. Repeating the same acknowledgement
    is safe; changing any of its exact completion evidence is refused.
    """
    if (
        type(job_id) is not str
        or not job_id
        or type(owner) is not RecoveryOwner
        or normalise_album_id(album_id) != album_id
        or type(completion_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", completion_hash) is None
    ):
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT created_at, album_id FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if (
                job_row is None
                or type(job_row[0]) not in (int, float)
                or normalise_album_id(job_row[1]) != album_id
            ):
                conn.rollback()
                return False
            existing = conn.execute(
                "SELECT job_created_at, album_id, completion_hash "
                "FROM durable_job_completions "
                "WHERE job_id=? AND operation_id=? AND item_id=?",
                (job_id, owner.operation_id, owner.item_id),
            ).fetchone()
            if existing is not None:
                matches = existing == (
                    job_row[0],
                    album_id,
                    completion_hash,
                )
                conn.commit() if matches else conn.rollback()
                return matches
            conn.execute(
                "INSERT INTO durable_job_completions "
                "(job_id, operation_id, item_id, job_created_at, album_id, "
                "completion_hash, acknowledged_at) VALUES (?,?,?,?,?,?,?)",
                (
                    job_id,
                    owner.operation_id,
                    owner.item_id,
                    job_row[0],
                    album_id,
                    completion_hash,
                    time.time(),
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            _note_write_failure(
                f"acknowledge durable completion for {job_id}",
                exc,
            )
            return False


def durable_completion_acknowledged(
    job_id: str,
    *,
    job_created_at: float,
    album_id: str,
) -> bool | None:
    """Check an exact job incarnation and album, or None if DB is unavailable."""
    if (
        type(job_id) is not str
        or not job_id
        or type(job_created_at) not in (int, float)
        or normalise_album_id(album_id) != album_id
    ):
        return None
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            return conn.execute(
                "SELECT 1 FROM durable_job_completions "
                "WHERE job_id=? AND job_created_at=? AND album_id=? LIMIT 1",
                (job_id, job_created_at, album_id),
            ).fetchone() is not None
        except sqlite3.Error as exc:
            _log.debug(
                "couldn't inspect durable completion for %s: %s",
                job_id,
                exc,
            )
            return None


def _relocation_handoff_consumer(job) -> dict | None:
    try:
        job_id = job.id
        created_at = job.created_at
        album_id = job.album_id
    except AttributeError:
        return None
    if (
        type(job_id) is not str
        or not job_id
        or "\x00" in job_id
        or type(created_at) not in (int, float)
        or not math.isfinite(created_at)
        or normalise_album_id(album_id) != album_id
    ):
        return None
    return {
        "kind": "web-single",
        "job_id": job_id,
        "job_created_at": created_at,
        "album_id": album_id,
    }


def _valid_relocation_handoff_consumer(value) -> bool:
    return (
        type(value) is dict
        and set(value) == {
            "kind", "job_id", "job_created_at", "album_id",
        }
        and value.get("kind") == "web-single"
        and type(value.get("job_id")) is str
        and bool(value["job_id"])
        and "\x00" not in value["job_id"]
        and type(value.get("job_created_at")) in (int, float)
        and math.isfinite(value["job_created_at"])
        and normalise_album_id(value.get("album_id")) == value.get("album_id")
    )


def _relocation_handoff_hash(consumer, payload) -> str | None:
    if not _valid_relocation_handoff_consumer(consumer) or type(payload) is not dict:
        return None
    try:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if json.loads(encoded_payload) != payload:
            return None
        encoded = json.dumps(
            {"consumer": consumer, "payload": payload},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def persist_post_import_relocation_handoff(
    job,
    *,
    operation_id: str,
    handoff_hash: str,
    single: dict,
) -> bool:
    """Atomically save one Web-single Undo snapshot and relocation handoff."""
    if (
        type(operation_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None
        or type(handoff_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", handoff_hash) is None
        or type(single) is not dict
    ):
        return False
    try:
        single_snapshot = json.loads(json.dumps(
            single,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    except (TypeError, ValueError, UnicodeError):
        return False
    if single_snapshot != single:
        return False
    with job._lock:
        consumer = _relocation_handoff_consumer(job)
        if consumer is None:
            return False
        if _relocation_handoff_hash(consumer, single_snapshot) != handoff_hash:
            return False
        values = _job_values(job, single=single_snapshot)
        if values is None:
            return False
        with _lock:
            conn = _get_conn()
            if conn is None:
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                job_row = conn.execute(
                    "SELECT created_at, album_id, single FROM jobs WHERE id=?",
                    (consumer["job_id"],),
                ).fetchone()
                if (
                    job_row is None
                    or job_row[0] != consumer["job_created_at"]
                    or job_row[1] != consumer["album_id"]
                ):
                    conn.rollback()
                    return False
                existing = conn.execute(
                    "SELECT job_id, job_created_at, album_id, handoff_hash "
                    "FROM post_import_relocation_handoffs "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                expected = (
                    consumer["job_id"],
                    consumer["job_created_at"],
                    consumer["album_id"],
                    handoff_hash,
                )
                if existing is not None:
                    try:
                        saved_single = json.loads(job_row[2])
                    except (TypeError, ValueError):
                        conn.rollback()
                        return False
                    if existing != expected or saved_single != single_snapshot:
                        conn.rollback()
                        return False
                conn.execute(_PERSIST_SQL, values)
                if existing is None:
                    conn.execute(
                        "INSERT INTO post_import_relocation_handoffs "
                        "(job_id, operation_id, job_created_at, album_id, "
                        "handoff_hash, acknowledged_at) VALUES (?,?,?,?,?,?)",
                        (
                            consumer["job_id"],
                            operation_id,
                            consumer["job_created_at"],
                            consumer["album_id"],
                            handoff_hash,
                            time.time(),
                        ),
                    )
                conn.commit()
                job.single = single_snapshot
                job.__dict__.pop("_single_undo_unavailable", None)
                return True
            except sqlite3.Error as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                _note_write_failure(
                    f"persist relocation handoff for {consumer['job_id']}",
                    exc,
                )
                return False


def post_import_relocation_handoff_persisted(
    operation_id: str,
    handoff: dict,
) -> bool | None:
    """Verify an exact sealed handoff; only definite absence permits rollback."""
    if (
        type(operation_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None
        or type(handoff) is not dict
        or set(handoff) != {"consumer", "hash"}
        or not _valid_relocation_handoff_consumer(handoff.get("consumer"))
        or type(handoff.get("hash")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", handoff["hash"]) is None
    ):
        return None
    consumer = handoff["consumer"]
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT h.job_id, h.job_created_at, h.album_id, "
                "h.handoff_hash, j.created_at, j.album_id, j.single "
                "FROM post_import_relocation_handoffs AS h "
                "LEFT JOIN jobs AS j ON j.id=h.job_id "
                "WHERE h.operation_id=?",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            _log.debug(
                "couldn't inspect relocation handoff %s: %s",
                operation_id,
                exc,
            )
            return None
    if row is None:
        return False
    if (
        row[:4] != (
            consumer["job_id"],
            consumer["job_created_at"],
            consumer["album_id"],
            handoff["hash"],
        )
        or row[4] != consumer["job_created_at"]
        or row[5] != consumer["album_id"]
    ):
        return None
    try:
        single = json.loads(row[6])
    except (TypeError, ValueError):
        return None
    if _relocation_handoff_hash(consumer, single) != handoff["hash"]:
        return None
    return True


def prepare_recovery_resolution(
        location: str, receipt: dict | None) -> RecoveryResolutionPlan | None:
    """Seal exact persisted Repair records before a manual Restore mutates."""
    try:
        location = str(location)
        if not location or "\x00" in location:
            return None
        canonical_receipt = json.loads(json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
        if canonical_receipt is not None and type(canonical_receipt) is not dict:
            return None
    except (TypeError, ValueError):
        return None

    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            rows = conn.execute(
                "SELECT id, recoveries FROM jobs "
                "WHERE COALESCE(recoveries, '[]') != '[]'"
            ).fetchall()
        except sqlite3.Error as exc:
            _log.debug("couldn't inspect Repair recovery records: %s", exc)
            return None

    matched = []
    for job_id, raw in rows:
        try:
            records = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if (
            type(records) is not list
            or any(not _valid_repair_recovery(record) for record in records)
        ):
            return None
        if any(
            record["location"] == location
            and record["receipt"] == canonical_receipt
            for record in records
        ):
            matched.append((str(job_id), str(raw)))
    return RecoveryResolutionPlan(
        location=location,
        receipt=canonical_receipt,
        rows=tuple(matched),
    )


def resolve_recovery_resolution(
        plan: RecoveryResolutionPlan) -> dict[str, dict] | None:
    """CAS-remove the exact retained records after Restore succeeds."""
    if type(plan) is not RecoveryResolutionPlan:
        return None
    resolved = {}
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            conn.execute("BEGIN IMMEDIATE")
            for job_id, expected_raw in plan.rows:
                row = conn.execute(
                    "SELECT recoveries, attention FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if row is None or row[0] != expected_raw:
                    conn.rollback()
                    return None
                records = json.loads(expected_raw)
                if (
                    type(records) is not list
                    or any(
                        not _valid_repair_recovery(record)
                        for record in records
                    )
                ):
                    conn.rollback()
                    return None
                remaining = [
                    record for record in records
                    if not (
                        record["location"] == plan.location
                        and record["receipt"] == plan.receipt
                    )
                ]
                if len(remaining) == len(records):
                    conn.rollback()
                    return None
                attention = row[1] or ""
                if not remaining and attention == "recovery":
                    attention = ""
                encoded = json.dumps(
                    remaining,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                changed = conn.execute(
                    "UPDATE jobs SET recoveries=?, attention=? WHERE id=?",
                    (encoded, attention, job_id),
                )
                if changed.rowcount != 1:
                    conn.rollback()
                    return None
                resolved[job_id] = {
                    "recoveries": remaining,
                    "attention": attention,
                }
            conn.commit()
            return resolved
        except (sqlite3.Error, TypeError, ValueError) as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            _note_write_failure("resolve Repair recovery", exc)
            return None


def delete(job_id: str) -> None:
    """Drop the row for a job pruned from the registry."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "DELETE FROM migration_candidate_entries WHERE job_id=?",
                (job_id,),
            )
            conn.execute(
                "DELETE FROM migration_candidate_payloads WHERE job_id=?",
                (job_id,),
            )
            conn.execute(
                "DELETE FROM migration_review_artifacts WHERE job_id=?",
                (job_id,),
            )
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.execute(
                "DELETE FROM durable_job_completions WHERE job_id=?",
                (job_id,),
            )
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs WHERE job_id=?",
                (job_id,),
            )
            conn.commit()
        except sqlite3.Error:
            pass


def load_one(job_id: str) -> Optional[dict]:
    """Return one persisted job by id (the same shape ``load_all`` yields
    per row), or None if it isn't on disk. Used by the read-only "this
    job was archived" page so a registry eviction doesn't make a job's
    history disappear from view."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at, single, attention, "
                "recoveries "
                "FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _log.debug("load_one failed for %s: %s", job_id, e)
            return None
    if row is None:
        return None
    try:
        return {
            "id": row[0], "title": row[1], "artist": row[2], "album_id": row[3],
            "kind": row[4], "status": row[5], "phase": row[6],
            "candidates": json.loads(row[7] or "[]"),
            "error": row[8], "summary": row[9] or "", "review_verb": row[10] or "Download",
            "execute_kind": row[11] or "",
            "execute_args": json.loads(row[12] or "{}"),
            "created_at": row[13], "finished_at": row[14],
            "single": json.loads(row[15] or "{}"),
            "attention": row[16] or "",
            "recoveries": json.loads(row[17] or "[]"),
        }
    except (ValueError, TypeError):
        return None


def prune_finished(keep: int, *, retain_job_id: str | None = None) -> None:
    """Drop the oldest terminal rows past ``keep`` so the archive doesn't
    grow without bound. Non-terminal jobs and unresolved Repair recoveries are
    never pruned here. Best-effort: a sqlite error logs and bows out."""
    if keep <= 0:
        return
    retained = (
        retain_job_id
        if isinstance(retain_job_id, str) and retain_job_id
        else None
    )
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            retained_sql = " AND id != ?" if retained is not None else ""
            params = (retained, keep) if retained is not None else (keep,)
            conn.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT id FROM jobs "
                "  WHERE status IN ('done', 'failed', 'canceled') "
                "    AND COALESCE(recoveries, '[]') = '[]'"
                f"{retained_sql} "
                "  ORDER BY COALESCE(finished_at, created_at) DESC "
                "  LIMIT -1 OFFSET ?)",
                params,
            )
            conn.execute(
                "DELETE FROM durable_job_completions "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=durable_job_completions.job_id)"
            )
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=post_import_relocation_handoffs.job_id)"
            )
            conn.execute(
                "DELETE FROM migration_candidate_entries "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=migration_candidate_entries.job_id)"
            )
            conn.execute(
                "DELETE FROM migration_candidate_payloads "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=migration_candidate_payloads.job_id)"
            )
            conn.execute(
                "DELETE FROM migration_review_artifacts "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=migration_review_artifacts.job_id)"
            )
            conn.commit()
        except sqlite3.Error as e:
            _log.debug("prune_finished(%d) failed: %s", keep, e)


_TERMINAL_SQL = "status IN ('done', 'failed', 'canceled')"
# The History view also lists parked reviews (with an Open link back to their
# surface) — but clearing and pruning must never touch them.
_HISTORY_SQL = "status IN ('done', 'failed', 'canceled', 'awaiting_review')"


# History splits finished work into two layers: meaningful jobs get cards,
# plain downloads get a compact table. The kinds below are the card layer.
BULK_KINDS = ("library", "new_releases", "upgrade", "downsample", "repair",
              "lyrics", "migration")

_BULK_MARKS = ",".join("?" for _ in BULK_KINDS)


def _kind_clause(bulk):
    # Downloads always carry the album they fetched; scans and other bulk work
    # never do. That split survives legacy rows whose execute_kind is empty.
    if bulk is None:
        return "", ()
    if bulk:
        return (f"AND (execute_kind IN ({_BULK_MARKS}) "
                "OR COALESCE(album_id, '') = '') ", BULK_KINDS)
    return (f"AND execute_kind NOT IN ({_BULK_MARKS}) "
            "AND COALESCE(album_id, '') != '' ", BULK_KINDS)


def history_count(
        bulk: Optional[bool] = None, *, exclude_recoveries: bool = False) -> int:
    """How many finished jobs are on disk — for paginating the History view."""
    clause, params = _kind_clause(bulk)
    if exclude_recoveries:
        clause += "AND COALESCE(recoveries, '[]') = '[]' "
    with _lock:
        conn = _get_conn()
        if conn is None:
            return 0
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {_HISTORY_SQL} {clause}",
                params).fetchone()[0]
        except sqlite3.Error:
            return 0


def history_page(limit: int, offset: int,
                 bulk: Optional[bool] = None,
                 status: Optional[str] = None, *,
                 exclude_recoveries: bool = False) -> list[dict]:
    """A page of finished jobs, newest first — the browsable record behind the
    History view. Lighter than ``load_all`` (no candidates/args): just what a
    history row shows, plus the id to open the full job. The ``id`` tiebreaker
    keeps paging stable when finish times collide. ``bulk`` narrows to the
    card layer (True), the downloads table (False), or everything (None).
    ``status`` narrows in SQL — a caller filtering the returned page itself
    would silently lose older matching rows whenever the newest ``limit``
    rows are mostly other statuses."""
    clause, params = _kind_clause(bulk)
    if exclude_recoveries:
        clause += "AND COALESCE(recoveries, '[]') = '[]' "
    if status:
        clause += "AND status = ? "
        params = (*params, status)
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, status, error, summary, "
                "execute_kind, created_at, finished_at, attention, recoveries "
                "FROM jobs "
                f"WHERE {_HISTORY_SQL} {clause}"
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        except sqlite3.Error as e:
            _log.debug("history_page failed: %s", e)
            return []
    result = []
    for row in rows:
        recoveries, recovery_unreadable = _decode_recoveries(row[11])
        result.append({
            "id": row[0], "title": row[1] or "", "artist": row[2] or "",
            "album_id": row[3] or "", "status": row[4], "error": row[5],
            "summary": row[6] or "", "execute_kind": row[7] or "",
            "created_at": row[8], "finished_at": row[9],
            "attention": row[10] or "", "recoveries": recoveries,
            "recovery_unreadable": recovery_unreadable,
        })
    return result


def recovery_history() -> list[dict]:
    """Every unresolved recovery obligation, outside ordinary History caps."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, status, error, summary, "
                "execute_kind, created_at, finished_at, attention, recoveries "
                "FROM jobs "
                "WHERE COALESCE(recoveries, '[]') != '[]' "
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            _log.debug("recovery_history failed: %s", exc)
            return []
    result = []
    for row in rows:
        recoveries, recovery_unreadable = _decode_recoveries(row[11])
        if not recoveries and not recovery_unreadable:
            continue
        result.append({
            "id": row[0], "title": row[1] or "", "artist": row[2] or "",
            "album_id": row[3] or "", "status": row[4], "error": row[5],
            "summary": row[6] or "", "execute_kind": row[7] or "",
            "created_at": row[8], "finished_at": row[9],
            "attention": row[10] or "", "recoveries": recoveries,
            "recovery_unreadable": recovery_unreadable,
        })
    return result


def last_finished_at(execute_kind: str) -> Optional[float]:
    """When a job of this ``execute_kind`` last finished cleanly, or None — backs
    the per-tool "Last scan …" freshness line. Only DONE counts (a failed/cancelled
    run isn't a completed pass), and the archive outlives the in-memory cap, so the
    line survives restarts. Reads the durable jobs.db rather than a separate stamp
    file so there's nothing extra to keep in sync."""
    if not execute_kind:
        return None
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT MAX(finished_at) FROM jobs "
                "WHERE status='done' AND execute_kind=? AND finished_at IS NOT NULL",
                (execute_kind,),
            ).fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row and row[0] is not None else None


def attention_count() -> int:
    """How many finished jobs still carry an attention marker — drives the
    warning dot on the Queue nav until each flagged job page has been opened."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return 0
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE attention != ''"
            ).fetchone()[0]
        except sqlite3.Error:
            return 0


def clear_history(*, retain_job_id: str | None = None) -> None:
    """Delete ordinary finished jobs; retain unresolved Repair recoveries."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            retained = (
                retain_job_id
                if isinstance(retain_job_id, str) and retain_job_id
                else None
            )
            retained_sql = " AND id != ?" if retained is not None else ""
            conn.execute(
                f"DELETE FROM jobs WHERE {_TERMINAL_SQL} "
                "AND COALESCE(recoveries, '[]') = '[]'"
                f"{retained_sql}",
                (retained,) if retained is not None else (),
            )
            conn.execute(
                "DELETE FROM durable_job_completions "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=durable_job_completions.job_id)"
            )
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=post_import_relocation_handoffs.job_id)"
            )
            conn.execute(
                "DELETE FROM migration_candidate_entries "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=migration_candidate_entries.job_id)"
            )
            conn.execute(
                "DELETE FROM migration_candidate_payloads "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=migration_candidate_payloads.job_id)"
            )
            conn.execute(
                "DELETE FROM migration_review_artifacts "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=migration_review_artifacts.job_id)"
            )
            conn.commit()
        except sqlite3.Error as e:
            _log.debug("clear_history failed: %s", e)


def load_all() -> list[dict]:
    """Return every persisted job as a plain dict — caller rehydrates into
    a Job. Returns [] when the db can't be opened."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at, single, attention, "
                "recoveries "
                "FROM jobs ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error as e:
            _log.info("couldn't read jobs.db on startup (%s); starting fresh.", e)
            return []
    out = []
    for r in rows:
        try:
            recoveries, recovery_unreadable = _decode_recoveries(r[17])
            if recovery_unreadable:
                raise ValueError("recovery payload is invalid")
            # Only AWAITING_REVIEW jobs need their candidates on restore; all
            # other statuses are either rehydrated as live state (RUNNING →
            # FAILED) or displayed as history without candidates.
            candidates = json.loads(r[7] or "[]") if r[5] == "awaiting_review" else []
            out.append({
                "id": r[0], "title": r[1], "artist": r[2], "album_id": r[3],
                "kind": r[4], "status": r[5], "phase": r[6],
                "candidates": candidates,
                "error": r[8], "summary": r[9] or "", "review_verb": r[10] or "Download",
                "execute_kind": r[11] or "",
                "execute_args": json.loads(r[12] or "{}"),
                "created_at": r[13], "finished_at": r[14],
                "single": json.loads(r[15] or "{}"),
                "attention": r[16] or "",
                "recoveries": recoveries,
            })
        except (ValueError, TypeError) as e:
            _log.info("skipping unreadable jobs.db row %s: %s", r[0], e)
    return out


def _reset_for_tests() -> None:
    """Test-only hook: drop the on-disk db so a fresh test starts clean."""
    global _disabled, _conn, _warned_write_failure
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    _disabled = False
    _warned_write_failure = False
    p = _path()
    for q in (p, p.with_suffix(".db-wal"), p.with_suffix(".db-shm")):
        try:
            q.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
