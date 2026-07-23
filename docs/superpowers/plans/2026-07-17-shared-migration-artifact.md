# Shared Migration Artifact Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent migration approval from expanding a preview-wide manifest artifact once per selected album.

**Architecture:** Persist the shared artifact once per job and retain compact versioned references in candidate parent rows. Resolve the artifact once per approval while retaining read compatibility with existing inline rows.

**Tech Stack:** Python 3.14, SQLite WAL, pytest, Docker Compose.

## Global Constraints

- Preserve the migration engine's fail-closed filesystem receipts.
- Existing inline version-1 payload rows remain readable.
- Missing or corrupt shared artifacts stop before filesystem mutation.
- Do not raise the memory limit as the remedy.

---

### Task 1: Shared artifact persistence and resolution

**Files:**
- Modify: `src/qobuz_librarian/web/job_persistence.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Candidate parent `manifest_artifact` stores `{"migration_artifact_ref":{"version":1}}`.
- `migration_review_artifacts(job_id, manifest_artifact)` owns the full value.

- [ ] Write a failing test proving two candidates store one full artifact and resolve to one shared in-memory object.
- [ ] Run the focused test and confirm it fails on the current duplicated representation.
- [ ] Add schema version 6, strict shared-row persistence, compatible loading, and job cleanup.
- [ ] Run focused persistence, corruption, lifecycle, and scale tests.

### Task 2: Build and deploy

**Files:**
- Modify only if tests require it: `src/qobuz_librarian/web/flows.py`

**Interfaces:**
- Existing scan and execution functions continue using the legacy payload shape.

- [ ] Run `pytest -q tests/test_web.py tests/test_migrate.py`.
- [ ] Run the complete project suite.
- [ ] Build `dinkeyes/qobuz-librarian:latest` from `source/`.
- [ ] Recreate the Compose service and verify health, image source hashes, schema version, and bounded persisted artifact shape with a synthetic database test.
