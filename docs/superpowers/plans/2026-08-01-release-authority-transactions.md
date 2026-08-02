# Release Authority Transactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace detached release-identity observations with live Linux filesystem authority that remains valid through manifest publication, completion acknowledgement, carrier retirement, and legacy adoption.

**Architecture:** Context-managed authority objects hold no-follow directory descriptors, deterministic inode write exclusions, and exact file versions for the full lifetime of the operation they authorize. Manifest publication uses an exclusive rename with crash-recoverable temporary evidence. Descriptor-bound adoption and one centralized Web disposition classifier close the remaining ABA and result-semantics gaps.

**Tech Stack:** Python 3.12+, Linux `fcntl` inode leases, descriptor-relative filesystem APIs, `renameat2(RENAME_NOREPLACE)`, pytest, FastAPI/Web job flows, existing durable journal and run-lock APIs.

## Global Constraints

- Safety-critical authority is supported only when the mounted music filesystem passes the Linux inode-write-exclusion probe; unsupported filesystems fail closed and retain evidence.
- Acquire file leases in stable bytewise relative-path order and release them in reverse order.
- Never follow symlinks for album directories, audio files, manifests, or transaction artifacts.
- Existing manifest JSON, friendly paths, collision suffixes, durable journal schema, and ordinary-album paths remain unchanged.
- A failed publication/finalization must not acknowledge completion, retire a carrier or repair backup, prune a review candidate, or delete the last known-good recovery artifact.
- Every production change requires a focused RED reproduction before implementation and a GREEN unprivileged Linux control afterward.

---

### Task 1: Add Lease-Bearing File and Album Authority

**Files:**

- Modify: `src/qobuz_librarian/file_exclusion.py`
- Create: `src/qobuz_librarian/library/release_authority.py`
- Create: `tests/test_release_authority.py`
- Modify: `tests/test_file_exclusion.py`

**Interfaces:**

- Consumes: `DirectoryPathReceipt`, `capture_directory_path_receipt()`, and `directory_path_receipt_matches()` from `library.release_identity`.
- Consumes: `RunLockLease` from the existing run-lock module.
- Produces: `FileVersion(device, inode, size, mtime_ns, ctime_ns)`.
- Produces: `HeldFileAuthority(relative: Path, descriptor: int, version: FileVersion, digest: str, exclusion: InodeWriteExclusion)`.
- Produces: `AlbumAuthorityUnavailable(OSError)`.
- Produces: `open_album_authority(path: Path, authority: RunLockLease, *, expected_path: DirectoryPathReceipt | None = None) -> AbstractContextManager[AlbumAuthority]`.
- Produces: `AlbumAuthority.open_file(relative: Path, *, expected_digest: str | None = None) -> HeldFileAuthority` and `AlbumAuthority.validate_namespace() -> None`.

- [ ] **Step 1: Write lease lifetime and ordering tests**

Add real Linux tests that open two audio files in reverse discovery order, then assert acquisition occurs in bytewise path order, a concurrent writable open is refused while the context is live, and all leases/descriptors are released after normal exit and exceptions.

```python
def test_album_authority_holds_write_exclusion_until_context_exit(
        tmp_path, run_lock):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "02.flac").write_bytes(b"two")
    (album / "01.flac").write_bytes(b"one")
    lease = run_lock.acquire()
    try:
        with open_album_authority(album, lease) as held:
            one = held.open_file(Path("01.flac"))
            two = held.open_file(Path("02.flac"))
            assert [one.relative, two.relative] == [
                Path("01.flac"), Path("02.flac")]
            assert _writable_open_is_refused(album / "01.flac")
        assert _writable_open_is_allowed(album / "01.flac")
    finally:
        lease.close()
```

- [ ] **Step 2: Run authority tests and verify RED**

Run:

```bash
pytest -q tests/test_release_authority.py tests/test_file_exclusion.py
```

Expected: collection fails because `library.release_authority` and its interfaces do not exist.

- [ ] **Step 3: Implement deterministic held authority**

Use no-follow descriptor-relative opens. Validate `RunLockLease` at entry and exit. Convert every `stat_result` with:

```python
def file_version(st):
    return FileVersion(
        device=st.st_dev, inode=st.st_ino, size=st.st_size,
        mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns)
```

Acquire `InodeWriteExclusion` immediately after opening each regular file and before hashing it. A missing lease, changed full version, link, non-regular entry, or path-receipt mismatch raises `AlbumAuthorityUnavailable`; context cleanup closes every exclusion and descriptor in reverse order.

- [ ] **Step 4: Run authority and existing exclusion tests**

```bash
pytest -q tests/test_release_authority.py tests/test_file_exclusion.py
ruff check src/qobuz_librarian/file_exclusion.py \
  src/qobuz_librarian/library/release_authority.py \
  tests/test_release_authority.py tests/test_file_exclusion.py
```

Expected: all pass on the unprivileged Linux filesystem; unsupported lease probes fail closed in their dedicated tests.

- [ ] **Step 5: Commit held authority**

```bash
git add src/qobuz_librarian/file_exclusion.py \
  src/qobuz_librarian/library/release_authority.py \
  tests/test_release_authority.py tests/test_file_exclusion.py
git commit -m "feat: add lease-bearing album authority"
```

### Task 2: Publish Manifests with Exclusive Rename

**Files:**

- Modify: `src/qobuz_librarian/library/release_identity.py`
- Modify: `src/qobuz_librarian/library/release_authority.py`
- Modify: `tests/test_release_identity.py`
- Modify: `tests/test_release_authority.py`

**Interfaces:**

- Consumes: `AlbumAuthority` from Task 1.
- Produces: `publish_release_identity_authorized(album: AlbumAuthority, identity: ReleaseIdentity) -> None`.
- Produces: `reconcile_release_manifest_transaction(album: AlbumAuthority) -> ReleaseIdentity | None`.
- Keeps: `publish_release_identity()` as a compatibility wrapper only for non-safety-critical callers; it must acquire the same live authority or fail closed.

- [ ] **Step 1: Write terminal-writer and crash-state tests**

Add real tests for a writer attempting a same-inode overwrite while the manifest authority is held; crashes before rename, after rename, and before directory fsync; final-name unlink/replacement; transaction-artifact replacement; and exception cleanup. Assert exactly one of these states remains: committed canonical final manifest or an exact reserved recovery artifact. Never allow zero evidence after the file was durably written.

```python
def test_exclusive_publication_never_deletes_last_evidence(
        tmp_path, run_lock, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    lease = run_lock.acquire()
    try:
        with open_album_authority(album, lease) as held:
            monkeypatch.setattr(
                release_identity, "_fsync_directory",
                _interrupt_after_exclusive_rename)
            with pytest.raises(KeyboardInterrupt):
                publish_release_identity_authorized(
                    held, ReleaseIdentity("qobuz", "123"))
        assert _canonical_final_or_exact_recovery_artifact(album, "123")
    finally:
        lease.close()
```

- [ ] **Step 2: Run publication tests and verify RED**

```bash
pytest -q tests/test_release_identity.py tests/test_release_authority.py \
  -k 'exclusive or terminal_writer or recovery_artifact or last_evidence'
```

Expected: failures show the two-link cleanup protocol and detached read cannot satisfy the new authority contract.

- [ ] **Step 3: Implement exclusive publication and reconciliation**

Reuse the repository's syscall adapter pattern for `renameat2(..., RENAME_NOREPLACE)`. Publication writes canonical bytes to a reserved temporary name, holds and validates that descriptor, fsyncs it, exclusively renames it to `MANIFEST_NAME`, validates the final descriptor/name/bytes, then fsyncs the held album directory. Do not unlink a transaction artifact through a pathname unless its held descriptor and full version still match.

Reserved artifact names use a fixed prefix such as `.qobuz-librarian-release.txn-` and must be added to the shared reserved-artifact predicate. Reconciliation runs only under `AlbumAuthority`, validates exact canonical bytes, and either completes the exclusive rename or retains the artifact for a later retry.

- [ ] **Step 4: Run manifest, generic-enumeration, migration, and backup tests**

```bash
pytest -q tests/test_release_identity.py tests/test_release_authority.py \
  tests/test_catalog.py tests/test_integrations.py tests/test_migrate.py \
  tests/test_backup_and_catalog_helpers.py
ruff check src/qobuz_librarian/library/release_identity.py \
  src/qobuz_librarian/library/release_authority.py
```

Expected: transaction artifacts never enter generic catalog, migration companion, or backup companion channels; all safety tests pass except the established Beets/SQLite baseline when the complete integrations file runs on the host filesystem.

- [ ] **Step 5: Commit exclusive publication**

```bash
git add src/qobuz_librarian/library/release_identity.py \
  src/qobuz_librarian/library/release_authority.py \
  tests/test_release_identity.py tests/test_release_authority.py
git commit -m "fix: publish release manifests under live authority"
```

### Task 3: Hold Verified Inventory Through Final Retirement

**Files:**

- Modify: `src/qobuz_librarian/modes/process.py`
- Modify: `src/qobuz_librarian/queue/post_import_finalizer.py`
- Modify: `src/qobuz_librarian/queue/durable_runner.py`
- Modify: `tests/test_process.py`
- Modify: `tests/test_durable_post_import_action.py`
- Modify: `tests/test_queue_retirement_action.py`
- Modify: `tests/test_durable_runner.py`

**Interfaces:**

- Consumes: `AlbumAuthority` and `publish_release_identity_authorized()` from Tasks 1-2.
- Produces: `open_verified_album_inventory(path, authority, expected_receipt, expected_audio) -> AbstractContextManager[VerifiedAlbumInventory]`.
- `VerifiedAlbumInventory.publish(identity) -> None` keeps all audio exclusions live.
- `VerifiedAlbumInventory.validate_namespace() -> None` performs descriptor-relative namespace validation without releasing file authority.

- [ ] **Step 1: Write held-inventory retirement tests**

Add synchronous and durable tests that start a competing writer after the final ordinary audit point. Assert the writer is refused until publication, acknowledgement, and carrier retirement finish. Add lease-acquisition failure, post-publish namespace replacement, cancellation, exception, and crash tests; every failure must retain completion/action/carrier evidence.

```python
def test_retirement_holds_audio_exclusion_through_acknowledgement(
        durable_album_fixture):
    events = []
    result = durable_album_fixture.finalize(
        on_before_ack=lambda audio: events.append(
            _writable_open_is_refused(audio)))
    assert result is True
    assert events == [True]
```

- [ ] **Step 2: Run finalization tests and verify RED**

```bash
pytest -q tests/test_process.py tests/test_durable_post_import_action.py \
  tests/test_queue_retirement_action.py tests/test_durable_runner.py \
  -k 'held_inventory or writer or publication or retirement or cancellation'
```

Expected: competing writers remain possible after detached audits, or the new context interfaces are missing.

- [ ] **Step 3: Replace detached audits with one live transaction**

Open `VerifiedAlbumInventory` before the final synchronous inventory proof and before durable retirement publication. Keep the context alive through publication and the existing `identity -> release -> acknowledge -> carrier` order. Durable upgrade rebind must continue to require the exact committed backup transition; it may open a new authority only after proving that owned transition.

No caller may convert `VerifiedAlbumInventory` to a detached boolean and then acknowledge later. On any exception, close leases but leave durable journal/completion/action/carrier records unchanged.

- [ ] **Step 4: Run affected finalization and startup recovery suites**

```bash
pytest -q tests/test_process.py tests/test_completion.py \
  tests/test_durable_post_import_action.py tests/test_queue_retirement_action.py \
  tests/test_durable_runner.py tests/test_startup_recovery.py
ruff check src/qobuz_librarian/modes/process.py \
  src/qobuz_librarian/queue/post_import_finalizer.py \
  src/qobuz_librarian/queue/durable_runner.py
```

Expected: all feature controls pass; the two documented branch-start filesystem/order baselines remain the only complete-suite failures.

- [ ] **Step 5: Commit verified inventory lifetime**

```bash
git add src/qobuz_librarian/modes/process.py \
  src/qobuz_librarian/queue/post_import_finalizer.py \
  src/qobuz_librarian/queue/durable_runner.py \
  tests/test_process.py tests/test_durable_post_import_action.py \
  tests/test_queue_retirement_action.py tests/test_durable_runner.py
git commit -m "fix: retain album authority through retirement"
```

### Task 4: Make Legacy Adoption Descriptor-Bound

**Files:**

- Modify: `src/qobuz_librarian/library/album_placement.py`
- Modify: `src/qobuz_librarian/library/discovery.py`
- Modify: `src/qobuz_librarian/integrations/beets.py`
- Modify: `tests/test_album_placement.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_integrations.py`

**Interfaces:**

- Consumes: `AlbumAuthority` from Task 1.
- Produces: `LegacyAdoptionScan`, a context-managed descriptor-backed album source.
- Produces: `LegacyAdoptionProof(identity, path_receipt, audio_receipts, authority_generation)` that is valid only while its scan/placement authority remains live.
- Extends tag/inventory scanning with descriptor-relative input; it must not reopen the public album pathname.

- [ ] **Step 1: Write A-to-B-to-A scanning tests**

Use real A and B albums with different audio hashes, tags, ISRCs, and candidate release IDs. Swap A to B while scanning and restore A before selection completes. Assert the scanner consumes held A only, never selects B's ID for A, and either adopts the correct A identity or returns attention before mutation.

- [ ] **Step 2: Run adoption tests and verify RED**

```bash
pytest -q tests/test_album_placement.py tests/test_discovery.py \
  tests/test_integrations.py -k 'adoption or legacy or swap or aba'
```

Expected: current pathname-backed scanners can consume B while the receipt later revalidates A.

- [ ] **Step 3: Implement descriptor-backed scanning**

`LegacyAdoptionScan` opens the root once, acquires deterministic audio exclusions, and passes descriptor-relative file handles or `/proc/self/fd/<fd>` read-only paths to existing tag readers. Candidate selection, receipt construction, and placement publication consume the same held files. `resolve_album_placement(..., adoption_proof=...)` rejects detached or closed proofs.

- [ ] **Step 4: Run adoption, catalog, and process suites**

```bash
pytest -q tests/test_album_placement.py tests/test_discovery.py \
  tests/test_integrations.py tests/test_process.py tests/test_catalog.py
ruff check src/qobuz_librarian/library/album_placement.py \
  src/qobuz_librarian/library/discovery.py \
  src/qobuz_librarian/integrations/beets.py
```

Expected: A-to-B-to-A controls and ordinary adoption pass; links and unsupported leases fail closed.

- [ ] **Step 5: Commit descriptor-bound adoption**

```bash
git add src/qobuz_librarian/library/album_placement.py \
  src/qobuz_librarian/library/discovery.py \
  src/qobuz_librarian/integrations/beets.py \
  tests/test_album_placement.py tests/test_discovery.py tests/test_integrations.py
git commit -m "fix: bind legacy adoption to held album evidence"
```

### Task 5: Centralize Web Process Disposition

**Files:**

- Create: `src/qobuz_librarian/web/process_disposition.py`
- Modify: `src/qobuz_librarian/web/flows.py`
- Modify: `src/qobuz_librarian/web/app.py`
- Modify: `tests/test_web.py`

**Interfaces:**

- Produces: immutable `ProcessDisposition(mutated_library, verified_success, publish_derived_state, retire_backup, consume_candidate, attention)`.
- Produces: `classify_process_result(result: dict | None) -> ProcessDisposition`.
- `result == "identity_attention"` takes precedence over `imported` at every call site.

- [ ] **Step 1: Write one parameterized five-surface test**

Parameterize Missing Albums, direct album, upgrade, damaged-album redownload, and repair execution with `{result: "identity_attention", imported: True}`. Assert no success count, refresh, prune, quality mark, unmark, review retirement, reconciliation, or repair-backup retirement; status and summary report attention and retain the candidate/evidence.

- [ ] **Step 2: Run Web tests and verify RED**

```bash
pytest -q tests/test_web.py -k 'identity_attention and (upgrade or repair or direct or album)'
```

Expected: upgrade and repair paths still count/refresh/retire, and the classifier module is missing.

- [ ] **Step 3: Implement and route the classifier**

Implement one exhaustive classifier. Apply it immediately after every `process_album()` result, before any backup retirement or success-derived side effect. Remove caller-specific `imported_ok` conditions.

```python
if result_kind == "identity_attention":
    return ProcessDisposition(
        mutated_library=bool(result.get("imported")),
        verified_success=False,
        publish_derived_state=False,
        retire_backup=False,
        consume_candidate=False,
        attention="identity",
    )
```

- [ ] **Step 4: Run full Web tests and lint**

```bash
pytest -q tests/test_web.py
ruff check src/qobuz_librarian/web/process_disposition.py \
  src/qobuz_librarian/web/flows.py src/qobuz_librarian/web/app.py \
  tests/test_web.py
```

Expected: all Web tests pass and every surface has the same attention semantics.

- [ ] **Step 5: Commit centralized disposition**

```bash
git add src/qobuz_librarian/web/process_disposition.py \
  src/qobuz_librarian/web/flows.py src/qobuz_librarian/web/app.py \
  tests/test_web.py
git commit -m "fix: centralize web process result semantics"
```

### Task 6: End-to-End Linux Authority Verification

**Files:**

- Modify only if a production-shaped stale fixture must be corrected: `tests/`
- Append evidence: `.superpowers/sdd/2026-07-31-qobuz-release-identity/task-13-report.md`

**Interfaces:**

- Consumes every interface from Tasks 1-5.
- Produces integration-ready verification evidence with no feature-introduced failures, skips, or xfails.

- [ ] **Step 1: Run the authority-focused matrix**

```bash
pytest -q tests/test_release_authority.py tests/test_release_identity.py \
  tests/test_file_exclusion.py tests/test_album_placement.py \
  tests/test_discovery.py tests/test_integrations.py tests/test_process.py \
  tests/test_durable_runner.py tests/test_durable_post_import_action.py \
  tests/test_queue_retirement_action.py tests/test_startup_recovery.py \
  tests/test_web.py
```

Expected: all feature controls pass; only the established Beets/SQLite baseline may fail when its complete file is included.

- [ ] **Step 2: Run the prescribed identity matrix and complete suite**

Use the unprivileged Linux Python 3.12 container, read-only source mount, and `/dev/shm` pytest root:

```bash
pytest -q tests/test_release_identity.py tests/test_album_placement.py \
  tests/test_edition_badges.py tests/test_catalog.py tests/test_discovery.py \
  tests/test_integrations.py tests/test_post_import_relocation.py \
  tests/test_migrate.py tests/test_backup_and_catalog_helpers.py \
  tests/test_capped_durability.py
pytest -q
```

Expected: no feature-introduced failures. Exact allowed baseline failures are:

- `tests/test_integrations.py::test_beets_direct_detects_silent_skip_by_unmoved_audio`
- `tests/test_ui_and_repair.py::test_repair_album_isrc_counts_never_opens_appledouble_flacs`

- [ ] **Step 3: Run lint, diff, skip, and artifact checks**

```bash
ruff check src/qobuz_librarian tests
git diff --check
git diff --check d0d6711..HEAD
git diff d0d6711..HEAD -- tests | rg 'pytest.mark.(skip|xfail)' || true
git status --short
```

Expected: lint/diff clean, no new skip/xfail, no generated artifact, and a clean tracked worktree.

- [ ] **Step 4: Audit and record invariants**

Append exact RED/GREEN commands and counts to the Task 13 report. Confirm live authority spans publication/acknowledgement/retirement, manifest rename never deletes last evidence, adoption uses held scan evidence, all Web surfaces use the classifier, and ordinary album paths remain unchanged.

- [ ] **Step 5: Commit any necessary production-shaped fixture correction**

If no fixture change is required, do not create a verification-only commit. If one is required, commit only the exact test file with:

```bash
git add tests/<exact-file>.py
git commit -m "test: align authority fixture with production contract"
```
