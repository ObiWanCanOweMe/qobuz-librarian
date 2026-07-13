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
import json
import logging
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
# The db opened fine but a write later failed (typically a full disk). Surface
# it once at INFO — durability is silently gone otherwise — then stay quiet so
# a stuck volume doesn't flood the log on every status change.
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


_SCHEMA_VERSION = 4


def init() -> None:
    """Create the schema (and run additive migrations). Safe to call repeatedly."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(_SCHEMA)
            conn.execute(_DURABLE_COMPLETION_SCHEMA)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_durable_job_completion_album "
                "ON durable_job_completions(job_id, job_created_at, album_id)"
            )
            # Terminal-row index: history_count() / history_page() / prune_finished()
            # all filter on status and order by finished_at. Without this they full-
            # scan the table, touching every row's record header just to skip past
            # the multi-MB ``candidates`` blob of parked reviews. COUNT(*) is served
            # entirely from the index; the page query uses it to filter+sort before
            # fetching only the LIMIT rows. created_at is the ORDER BY's COALESCE
            # fallback, so it's carried in the index too. IF NOT EXISTS = idempotent.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_terminal "
                "ON jobs(status, finished_at, created_at)"
            )
            # Schema versioning so a FUTURE column addition can ALTER TABLE instead
            # of silently failing every persist() against an old jobs.db — that
            # failure is swallowed by _note_write_failure, leaving the archive
            # non-durable with no visible sign. To add a column later: bump
            # _SCHEMA_VERSION and add an `if version < N: conn.execute("ALTER TABLE
            # jobs ADD COLUMN ...")` block here (SQLite ADD COLUMN is online-safe).
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                # v2: persist Job.single (single-track download undo info) so a restart
                # doesn't drop the Undo affordance on a completed one-track download.
                # _SCHEMA already adds the column for a fresh db (CREATE TABLE), so
                # only ALTER an existing table that predates it. ADD COLUMN is
                # online-safe and the DEFAULT backfills old rows with '{}'.
                cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
                if "single" not in cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN single TEXT NOT NULL DEFAULT '{}'")
                # v3: persist Job.attention (finished-job needs-review marker,
                # e.g. a download that stayed under the quality target) so the
                # History chip and nav dot survive a restart.
                if "attention" not in cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN attention TEXT NOT NULL DEFAULT ''")
                # v4: exact retained Repair-backup state. This is deliberately
                # separate from logs so cancellation/restart cannot discard the
                # only recovery location and receipt.
                if "recoveries" not in cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN recoveries TEXT NOT NULL DEFAULT '[]'")
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
        except sqlite3.Error as e:
            # A transient/locked/full/corrupt jobs.db here would otherwise
            # propagate out of restore_jobs() into the caller's broad "couldn't
            # restore prior jobs — starting fresh" handler, masking a recoverable
            # condition and then leaving every later persist() silently non-
            # durable. Surface it distinctly and degrade to the in-memory
            # registry; a restart once the volume recovers re-runs this.
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


def _job_values(job):
    """Serialise one job while its lock is held, or return None."""
    try:
        candidates_json = json.dumps(job.candidates or [], default=str)
        execute_args_json = json.dumps(job.execute_args or {}, default=str)
        single_json = json.dumps(job.single or {}, default=str)
        # Recovery receipts are a safety boundary. Never stringify an unknown
        # value into a plausible-looking record: either the exact JSON carrier
        # is saved or this persist reports failure.
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


def persist(job) -> bool:
    """Write the job's current state to disk. Return whether it reached disk."""
    # Keep the job lock through the database commit. Besides producing one
    # coherent snapshot, this preserves save order: an older delayed save can't
    # pause after serialising and then overwrite a newer selection. The database
    # lock is always taken second; readers and cleanup never take a job lock.
    with job._lock:
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
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.execute(
                "DELETE FROM durable_job_completions WHERE job_id=?",
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
            # FAILED) or displayed as history without candidates. Skipping the
            # json.loads for the rest avoids deserialising ~950 multi-MB blobs
            # on startup when only a handful of parked reviews exist.
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
