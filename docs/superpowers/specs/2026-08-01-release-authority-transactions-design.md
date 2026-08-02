# Release Authority Transactions Design

## Goal

Close the remaining release-identity race windows by replacing detached
filesystem observations with live, lease-bearing authority that remains valid
through the mutation, acknowledgement, or retirement it authorizes.

## Scope and threat model

The authoritative deployment target is Linux. Safety-critical finalization
must fail closed when the music filesystem cannot provide the required
write-exclusion semantics. macOS development and Docker-on-mac may run pure
logic and rendering tests, but destructive authority tests are authoritative
only after probing the mounted Linux filesystem.

The design protects against:

- cooperating Qobuz Librarian processes and threads;
- other same-UID processes that open or modify authoritative audio or manifest
  inodes while a transaction is active;
- pathname replacement, rename, unlink, same-inode overwrite, and ABA races;
- crashes between durable publication phases.

Host root or an administrator can always defeat process-level filesystem
controls. That is outside this boundary. Every unsupported or ambiguous case
retains journal, backup, completion, and review evidence rather than granting
success.

## Architecture

### 1. Lease-bearing filesystem authority

Safety-critical APIs return context-managed authority objects, not detached
booleans or identities. An authority owns:

- a no-follow, root-to-leaf directory descriptor chain;
- exact directory and file versions;
- held audio or manifest descriptors;
- Linux inode write exclusion for every content-bearing file;
- the active Qobuz Librarian run-lock lease;
- deterministic cleanup that releases leases and descriptors in reverse order.

`read_release_identity()` remains available for display, fingerprints, and
other point-in-time observations. Placement, adoption, merge, publication,
completion acknowledgement, and carrier retirement must consume a live
authority object instead.

Audio descriptors and leases are acquired in stable bytewise relative-path
order to avoid deadlock. A lease failure, writable mapping, changed namespace,
or unsupported filesystem raises an attention/unavailable result before the
authorized mutation or leaves durable evidence unacknowledged.

### 2. Verified inventory transaction

Durable and synchronous finalization use one `VerifiedAlbumInventory`
transaction. It opens the album through the frozen path receipt, walks through
held directory descriptors, opens every expected audio file without following
links, acquires write exclusion, validates full versions and hashes, and keeps
those resources live through:

1. manifest publication;
2. the post-publication namespace check;
3. completion acknowledgement;
4. carrier retirement.

There is no terminal check followed by a resource-free success interval. If a
transaction cannot retain authority until the final state transition, it does
not acknowledge completion.

### 3. Exclusive manifest publication

Manifest publication no longer creates two hard-link names and then removes
one. It:

1. creates a reserved temporary file in the held album directory;
2. writes canonical bytes, verifies the held descriptor, and fsyncs the file;
3. installs it at the final manifest name with an exclusive no-replace rename;
4. validates the final name against the held descriptor and canonical bytes;
5. fsyncs the directory before reporting committed publication.

Linux uses `renameat2(RENAME_NOREPLACE)`. The adapter may use an equivalent
exclusive primitive on another platform, but safety-critical publication fails
closed where no exact primitive is available.

Crash recovery treats a surviving reserved temporary entry as explicit
recovery evidence. Generic discovery, census, migration, backup, and Navidrome
enumeration continue to ignore reserved transaction artifacts. Reconciliation
may remove a temporary entry only while holding and validating its exact file
descriptor and album authority; it never deletes the last known-good manifest
evidence speculatively.

### 4. Descriptor-bound legacy adoption

Legacy adoption uses a `LegacyAdoptionScan` transaction. The same held album
root and audio descriptors provide tag data, inventory receipts, candidate
selection evidence, and the placement/adoption decision. Discovery and Beets
must not capture a receipt and then rescan through the public pathname.

The transaction keeps the exact reviewed evidence live until placement is
published or refused. A pathname swap, link, unsupported file, or lease loss
returns identity attention before Beets or filesystem mutation.

### 5. Central Web result classification

All Web execution surfaces consume one `ProcessDisposition` classifier. Result
kind takes precedence over `imported`.

For `identity_attention` with `imported=true`:

- the library may have been partially mutated;
- verified success is false;
- no success, upgraded, or repaired count is incremented;
- no candidate is pruned or review retired;
- no derived refresh, capped-quality mark, unmark, or reconciliation runs;
- no repair backup is retired;
- status and summary explicitly report identity attention and retained review.

The classifier is required in Missing Albums, direct album downloads,
upgrades, damaged-album redownload, and repair execution.

## Compatibility and recovery

- Existing manifest JSON and friendly/collision paths do not change.
- Existing durable journals remain readable. Recovery reacquires live authority
  from their frozen paths and receipts before acting.
- Ordinary albums without edition collisions retain their existing paths.
- Legacy receipts remain restore-only unless they contain the exact identity
  binding already required by the release-identity plan.
- Web persistence schema does not need a new result value; the classifier
  normalizes existing process results at execution boundaries.
- Native macOS safety-critical finalization is explicitly unavailable unless
  an equivalent write-exclusion backend is implemented and tested.

## Testing strategy

Every production change follows RED/GREEN TDD under an unprivileged Linux
container. Required controls include:

- a writer attempting a same-inode manifest overwrite after the last ordinary
  validation point while authority remains live;
- exclusive publication crashes before and after rename and directory fsync;
- final-name unlink/replacement and temporary-entry replacement;
- audio overwrite attempts during acknowledgement and carrier retirement;
- deterministic multi-file lease ordering and partial-acquisition cleanup;
- unsupported lease/filesystem behavior retaining durable evidence;
- real A-to-B-to-A legacy-adoption swaps during tag scanning;
- `identity_attention, imported=true` across all five Web execution surfaces;
- full affected suites, the identity matrix, full Linux suite, Ruff, and diff
  checks compared only with the two established branch-start baselines.

## Non-goals

- A dedicated writer daemon or deployment-wide permission redesign.
- Full private album snapshots or reflink-based publication.
- Protection from a privileged administrator intentionally modifying the
  library.
- Changing album naming, manifest schema, or Qobuz edition-badge semantics.
