# Durable Migration Review Payloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist large migration reviews as compact job candidates backed by normalized, restart-safe SQLite payload rows, while preserving the existing migration engine's receipt verification.

**Architecture:** Add migration-specific tables and APIs to `job_persistence.py`. `scan_migration` writes each album payload through that API and retains only a versioned reference in the job candidate; `execute_migration` resolves selected references back to the existing inline payload shape immediately before validation and mutation. Legacy inline candidates remain supported, and every missing or malformed reference fails closed.

**Tech Stack:** Python 3.14, SQLite WAL, pytest, existing Qobuz Librarian job and migration modules.

## Global Constraints

- Do not change migration planning, placement, copy, move, or filesystem verification behavior.
- Do not change review-page controls or candidate display fields.
- Do not use the CSV manifest as execution authority.
- New structured safety data must use canonical JSON with `sort_keys=True`, compact separators, `allow_nan=False`, and no `default=str`.
- Legacy inline migration candidates remain executable.
- Missing, malformed, unsupported, cross-job, or mixed-format payloads fail before filesystem mutation.
- The workspace snapshot contains no `.git` directory; replace commit steps with explicit diff checkpoints unless git metadata is restored before execution.

---

### Task 1: Durable migration payload schema and round-trip API

**Files:**
- Modify: `src/qobuz_librarian/web/job_persistence.py:145-240`
- Modify: `src/qobuz_librarian/web/job_persistence.py:955-1055`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `persist_migration_candidate_payload(job_id: str, candidate_id: str, payload: dict) -> bool`
- Produces: `load_migration_candidate_payload(job_id: str, candidate_id: str) -> dict | None`
- Produces: `delete_migration_payloads(job_id: str) -> None`
- Produces: `migration_payload_reference() -> dict`, returning `{"migration_payload_ref": {"version": 1}}`

- [ ] **Step 1: Write failing schema and round-trip tests**

Add tests that initialize a temporary jobs database, persist a payload containing ordinary entries, resume entries, and companion receipts, then assert:

```python
reference = job_persistence.migration_payload_reference()
assert reference == {"migration_payload_ref": {"version": 1}}
assert job_persistence.persist_migration_candidate_payload(
    "job-a", "c7", payload
)
assert job_persistence.load_migration_candidate_payload(
    "job-a", "c7"
) == payload
```

Query SQLite directly and assert one parent row, ordered `entry`, `resume`, and `companion` child rows, and no full payload in `jobs.candidates`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd source
pytest -q tests/test_web.py -k 'migration_payload_schema or migration_payload_round_trip'
```

Expected: failure because the schema and public functions do not exist.

- [ ] **Step 3: Add the normalized schema**

Create `migration_candidate_payloads` keyed by `(job_id, candidate_id)` with canonical JSON columns for source-root receipt, destination-root receipt, name semantics, and manifest artifact. Create `migration_candidate_entries` keyed by `(job_id, candidate_id, entry_kind, ordinal)` with `entry_kind` constrained to `entry`, `resume`, or `companion`; store paths and receipt JSON in bounded per-row fields. Add the required lookup index and increment `_SCHEMA_VERSION` from 4 to 5.

- [ ] **Step 4: Implement strict canonical encoding and transactional round trip**

Add private `_migration_json_dump` and `_migration_json_load` helpers that reject invalid JSON types and non-finite numbers. Implement one `BEGIN IMMEDIATE` transaction per candidate: remove old child rows, upsert the parent, insert ordered child rows, and commit. On any SQLite, type, key, or JSON error, rollback, log via `_note_write_failure`, and return `False`. Loading must validate ownership, required columns, allowed entry classes, contiguous ordinals, and exact tuple/list shapes before returning the existing payload dictionary.

- [ ] **Step 5: Integrate deletion paths**

Delete migration payload rows in `delete(job_id)`. Add orphan cleanup to `prune_finished` and `clear_history` alongside the existing durable completion cleanup. Use explicit deletion because the existing connection does not enable SQLite foreign keys.

- [ ] **Step 6: Run focused persistence tests and verify GREEN**

Run:

```bash
cd source
pytest -q tests/test_web.py -k 'migration_payload or prune_finished or clear_history'
```

Expected: all selected tests pass.

- [ ] **Step 7: Diff checkpoint**

Run `git diff -- src/qobuz_librarian/web/job_persistence.py tests/test_web.py` if git metadata exists; otherwise use `diff -u` against a saved pre-task copy and inspect only Task 1 changes.

---

### Task 2: Compact migration scan persistence

**Files:**
- Modify: `src/qobuz_librarian/web/flows.py:2539-2695`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `job_persistence.persist_migration_candidate_payload(job_id, candidate_id, payload) -> bool`
- Consumes: `job_persistence.migration_payload_reference() -> dict`
- Produces: migration review candidates whose `payload` contains only the versioned reference.

- [ ] **Step 1: Write a failing compact-scan regression test**

Stub the migration engine with a plan containing multiple entries whose receipts include large sentinel strings. Run `scan_migration`, then assert every candidate retains its display fields but has only:

```python
{"migration_payload_ref": {"version": 1}}
```

Assert the serialized `job.candidates` does not contain the receipt sentinel and remains nearly constant when receipt strings grow, while `load_migration_candidate_payload(job.id, cid)` returns the complete original entries.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd source
pytest -q tests/test_migrate.py -k compact_migration_scan_payload
```

Expected: failure because `scan_migration` still embeds the full payload.

- [ ] **Step 3: Persist payloads as candidates are built**

Import `job_persistence` in `flows.py`. Add each display candidate with the compact reference to obtain its final `cid`, then persist the full payload under `(job.id, cid)`. Do not allow the job to become actionable unless every candidate payload commit succeeds.

- [ ] **Step 4: Add fail-closed scan behavior**

If candidate creation returns no ID or payload persistence returns `False`, call `delete_migration_payloads(job.id)`, clear all migration candidates created by the scan, and set both `job.error` and `job.summary` to a message explaining that the preview could not be saved safely and nothing can be approved. Return without execution metadata exposed.

- [ ] **Step 5: Test bounded growth and write failure**

Add a test with many albums and large receipt sentinels that asserts `len(json.dumps(job.candidates))` scales with album display metadata, not track receipt size. Add a failure-injection test proving payload-store failure leaves no candidates or payload rows and sets a non-actionable error.

- [ ] **Step 6: Run migration scan tests and verify GREEN**

Run:

```bash
cd source
pytest -q tests/test_migrate.py -k 'compact_migration_scan_payload or migration_scan_payload_write_failure'
```

Expected: all selected tests pass.

- [ ] **Step 7: Diff checkpoint**

Inspect changes to `flows.py` and `test_migrate.py`; confirm the migration engine and manifest-writing functions are unchanged.

---

### Task 3: Lazy resolution, restart support, and fail-closed execution

**Files:**
- Modify: `src/qobuz_librarian/web/job_persistence.py`
- Modify: `src/qobuz_librarian/web/flows.py:2698-2840`
- Test: `tests/test_migrate.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `resolve_migration_candidates(job_id: str, candidates: list[dict]) -> list[dict] | None`
- Consumes: version-1 compact references or legacy inline payload dictionaries.
- Produces: the exact legacy candidate payload shape consumed by the current `execute_migration` loop.

- [ ] **Step 1: Write failing resolver tests**

Cover a version-1 candidate round trip, candidate order preservation, selected-subset loading after `_reset_for_tests()` and `init()`, and legacy inline pass-through. Assert the hydrated candidate is a copy and display metadata is unchanged.

- [ ] **Step 2: Write failing integrity tests**

Test unsupported reference versions, missing parent rows, missing/duplicate/non-contiguous child rows, malformed JSON, lookup under the wrong job ID, and a selection mixing inline and referenced formats. Each must return `None` without invoking the migration engine.

- [ ] **Step 3: Run resolver tests and verify RED**

Run:

```bash
cd source
pytest -q tests/test_web.py tests/test_migrate.py -k 'resolve_migration or referenced_migration'
```

Expected: failure because the resolver is absent and execution does not hydrate references.

- [ ] **Step 4: Implement strict lazy resolution**

Classify the entire selection before loading anything. Accept all legacy inline candidates or all exact version-1 references; reject mixed or unknown shapes. For references, derive ownership only from the function's `job_id` and each candidate's `cid`, load the durable payload, copy the candidate, and replace only its payload. Return `None` on any integrity failure.

- [ ] **Step 5: Resolve before migration validation or mutation**

At the beginning of `execute_migration`, resolve `chosen`. If resolution fails, set the existing safe “saved preview details” error and return before manifest verification, space checks, directory creation, copy, or move. Leave the remainder of the execution function operating on the legacy payload shape.

- [ ] **Step 6: Prove execution equivalence**

Extend the existing copy test to persist its sealed choice, execute through a compact reference, and assert copied bytes, retained originals, summary, and results manifest match the legacy test. Retain the original legacy-inline test unchanged.

- [ ] **Step 7: Run focused execution and restart tests and verify GREEN**

Run:

```bash
cd source
pytest -q tests/test_migrate.py tests/test_web.py -k 'migration and (execute or payload or restart or resolve)'
```

Expected: all selected tests pass.

- [ ] **Step 8: Diff checkpoint**

Inspect the diff and verify that all new resolution occurs before calls that can mutate the destination or source.

---

### Task 4: Full regression, realistic scale proof, and documentation

**Files:**
- Modify: `docs/troubleshooting.md`
- Test: `tests/test_migrate.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes all prior task interfaces.
- Produces verified behavior and operator documentation.

- [ ] **Step 1: Add a realistic scale test without multi-gigabyte allocation**

Generate thousands of track rows distributed across albums using repeated medium-size receipt data. Persist the compact job snapshot and assert its candidate JSON remains below an explicit small bound based on album count, the durable entry-row count matches the generated track count, and a selected album hydrates exactly. The test must reproduce the old growth pattern without allocating a 2 GB string.

- [ ] **Step 2: Run the scale test**

Run:

```bash
cd source
pytest -q tests/test_web.py -k migration_payload_scale
```

Expected: pass with bounded job JSON and complete durable rows.

- [ ] **Step 3: Document the fixed failure mode**

Add a troubleshooting note explaining that large previews use disk-backed review payloads, `/data/jobs.db` must remain writable and durable, and missing/corrupt payload rows intentionally require a rescan. Do not advise raising memory as a remedy for `INT_MAX` persistence failures.

- [ ] **Step 4: Run the complete relevant suites**

Run:

```bash
cd source
pytest -q tests/test_migrate.py tests/test_web.py
```

Expected: zero failures.

- [ ] **Step 5: Run the full project suite**

Run:

```bash
cd source
pytest -q
```

Expected: zero failures.

- [ ] **Step 6: Build the production image**

Run:

```bash
docker build -t dinkeyes/qobuz-librarian:latest source
```

Expected: exit status 0.

- [ ] **Step 7: Inspect the final diff and acceptance checklist**

Confirm the compact `jobs.candidates` invariant, restart hydration, legacy compatibility, fail-closed corruption behavior, cleanup, documentation, and unchanged migration-engine safety checks. If git metadata remains unavailable, report that commits could not be created and list all modified files explicitly.
