# Durable Migration Review Payloads Design

## Problem

A migration preview for 44,216 files builds one review candidate per album. Each candidate currently embeds every selected track's source and destination safety receipts in `jobs.candidates`. Persisting the job serializes the entire candidate list into one JSON string and one SQLite field. At this scale the string exceeds Python/SQLite's `INT_MAX` boundary and raises `OverflowError: string longer than INT_MAX bytes`. The oversized in-memory serialization can subsequently exhaust the container's memory.

The fix must keep the current review UI and the migration engine's fail-closed receipt verification while bounding the size of the ordinary job snapshot.

## Goals

- Persist arbitrarily large migration reviews without placing all execution receipts in one JSON value.
- Preserve exact source, destination, manifest, companion, and root receipts across restarts.
- Keep candidate selection, pagination, filtering, approval, and execution behavior unchanged.
- Keep legacy inline migration candidates executable.
- Fail closed when referenced payload data is missing, malformed, inconsistent, or cannot be persisted.
- Remove durable payload records when their owning job is permanently deleted.

## Non-goals

- Changing migration planning, placement, copy, move, or filesystem verification behavior.
- Changing the review page's user-facing controls.
- Making the CSV preview manifest the source of execution authority.
- Migrating legacy inline payloads eagerly.
- Generalizing the payload store to non-migration job types in this change.

## Architecture

Migration review display metadata remains in `jobs.candidates`: candidate ID, sequence, kind, title, artist, detail, selection state, and a compact versioned reference. Large execution data moves into normalized SQLite rows owned by the job and candidate.

The durable store has two tables:

1. `migration_candidate_payloads` contains one row per candidate. It stores the shared source-root receipt, destination-root receipt, destination name semantics, manifest artifact, and other album-level metadata.
2. `migration_candidate_entries` contains ordered track or resume-entry rows. Each row stores its entry class, source path, destination-relative path, source receipt, and destination receipt. Companion receipts are stored in ordered rows using a distinct entry class so no unbounded JSON list remains in the parent row.

All structured fields use canonical JSON: UTF-8 text, sorted keys, compact separators, `allow_nan=False`, and no `default=str`. Invalid data is rejected rather than coerced.

The compact candidate payload is:

```json
{"migration_payload_ref":{"version":1}}
```

The owning job ID and candidate ID come from the candidate itself and its job; they are never accepted from the reference body. This prevents a candidate from redirecting execution to another job's payload.

## Data Flow

### Scan and persistence

For each album group, the scan creates the display candidate and assigns its final candidate ID. It then writes that candidate's album metadata and ordered entry rows to the durable payload store. The compact reference is attached to the candidate only after the payload transaction commits.

The job cannot transition to `awaiting_review` until every referenced migration payload and the compact job snapshot have both persisted successfully. A failure removes payload rows created for that incomplete scan where possible, sets a clear non-actionable job error, and exposes no approvable review.

Payload writes are idempotent for the exact `(job_id, candidate_id)` owner. Replacing a candidate payload occurs in one transaction: delete that candidate's old entry rows, upsert its parent row, insert its new rows, and commit.

### Review and restart

Review routes continue to load compact candidates from `jobs.candidates`; display operations do not hydrate track receipts. This keeps ordinary page rendering, filtering, selection, and candidate persistence bounded.

Startup restoration needs no bulk hydration. A restored migration review retains compact references and reconstructs payloads only for candidates selected for execution.

### Approval and execution

Immediately before `execute_migration`, a resolver loads each selected candidate's parent and ordered entry rows and reconstructs the legacy payload shape expected by the existing execution flow. The execution engine and all receipt comparisons remain unchanged.

Legacy candidates whose payloads contain inline `entries` and `manifest_artifact` continue directly through the existing path. A single approval may contain either all legacy inline candidates or all version-1 references. Mixed formats are rejected as inconsistent review state rather than silently combined.

## Integrity and Failure Handling

- Both tables use foreign ownership by job ID and candidate ID, with uniqueness constraints that prevent duplicate sequence rows.
- The resolver verifies the reference version, parent ownership, entry classes, contiguous ordering, JSON types, and required fields.
- Missing or malformed records produce the existing safe outcome style: nothing is copied or moved, the job explains that saved preview details are unavailable, and the user must rescan.
- Payload-store database errors are logged and returned as controlled scan or execution failures; they must not escape the worker loop.
- No fallback reconstructs authority from mutable filesystem paths or the human-readable CSV manifest.
- Candidate display data is not trusted as execution data.

## Lifecycle and Cleanup

Payload rows survive normal container restarts and remain while a review can still be approved. The existing permanent job-deletion path deletes the owning migration payload rows in the same locked database operation. SQLite foreign keys with `ON DELETE CASCADE` are used if the existing connection enables and tests foreign-key enforcement; otherwise deletion is explicit and tested.

Terminal history retention alone does not delete payloads if the current product still permits that review to be restored or inspected. Cleanup follows the same ownership boundary as job deletion, not container startup.

## Schema Evolution

Schema creation is additive and idempotent during job database initialization. Existing `jobs` rows require no rewrite. Older inline migration reviews remain readable and executable. New code writes references only for newly scanned migration reviews.

The schema version is encoded in each compact reference and in the resolver contract, allowing a future payload representation to be added without guessing row shape.

## Testing

Automated tests must cover:

- A migration candidate stores compact display JSON while its full entries round-trip through the durable store.
- A synthetic large review keeps `jobs.candidates` proportional to album count and independent of receipt size.
- A persisted review reloads after registry/database reinitialization and selected candidates hydrate in their original order.
- Hydrated version-1 payloads produce the same input shape as legacy inline payloads for `execute_migration`.
- Legacy inline candidates still execute.
- Missing parent rows, missing or duplicate sequence rows, unsupported versions, malformed JSON, cross-job lookup attempts, and mixed inline/reference selections fail closed before filesystem mutation.
- A payload write failure prevents transition to an actionable review.
- Permanent job deletion removes parent, entry, and companion rows.
- Existing migration, job persistence, restart restoration, selection, and execution tests remain green.

## Acceptance Criteria

- The 44,216-file class of preview no longer constructs or sends a multi-gigabyte `jobs.candidates` value to SQLite.
- Peak persistence memory is bounded by one candidate payload plus the compact candidate list, rather than the whole review's track receipts serialized at once.
- The review remains usable after a container restart.
- Approving selected albums supplies the existing migration engine with all original safety receipts and preserves its fail-closed checks.
- Corrupt or missing durable payload data cannot cause a copy or move.

## Shared Preview Artifact Addendum

Production scale testing exposed one remaining duplication boundary: every
candidate parent row stored the same manifest artifact, whose context contains
the complete preview-wide companion receipt set.  A 3,080-album preview
therefore repeated a 4.37 MB value 3,080 times and approval hydrated more than
13 GB before the first copy.

New previews store that artifact once in a job-owned
`migration_review_artifacts` row. Candidate parents store only an exact
versioned reference. Approval loads and validates the shared artifact once and
reuses it while hydrating the selected candidates. Existing version-1 parent
rows with inline artifacts remain readable; missing or malformed shared rows
fail closed before filesystem mutation. Deleting the job deletes the shared
row with its candidate payloads.
