# Stable Qobuz Release Identity and Edition-Safe Album Paths

## Problem

Qobuz Librarian currently relies on artist, album title, and year when it
predicts or discovers an album directory. It also strips decorations such as
"Deluxe Edition", "Anniversary Edition", and "Remastered" while comparing
album versions. Those heuristics are useful for finding friendly paths, but
they are not a stable release identity. Two distinct Qobuz releases can
therefore resolve to the same directory, and edition-level deduplication can
discard the distinction before placement.

The Qobuz album/release ID is already available in the catalogue and download
workflow. It must become the authoritative identity without forcing release
IDs into every ordinary album path.

## Goals

- Treat `(provider, release_id)` as the stable identity of a managed album.
- Preserve current friendly artist and album paths when no edition collision
  exists.
- Add a Qobuz ID suffix only when distinct releases would otherwise occupy the
  same friendly album path.
- Adopt existing unmarked folders automatically only when one Qobuz release is
  identified with high confidence and no competing release is equally
  plausible.
- Keep ambiguous legacy folders untouched and surface them for review.
- Store the authoritative release identity beside the album in a portable,
  machine-readable manifest.
- Ensure the manifest can never be indexed as audio or mistaken for a user
  companion file, including in code paths previously affected by AppleDouble
  `._*` files.
- Preserve the manifest deliberately through Qobuz Librarian-owned album
  moves, backups, exports, and restores.
- Indicate the exact Qobuz edition in both Missing Albums and Gap Fill when
  multiple releases could otherwise be confused, without adding noise to
  unambiguous album rows.

## Non-goals

- Renaming every existing album to include a release ID.
- Encoding edition identity solely in a display title, folder name, Beets
  database row, or audio tag.
- Guessing an identity for ambiguous legacy folders.
- Automatically combining different Qobuz release IDs because their track
  lists overlap.
- Changing Navidrome's scanner or requiring Navidrome to understand the
  manifest.
- Treating arbitrary `.nfo` or `.json` files as Qobuz Librarian metadata.

## Chosen Approach

Use a hidden per-album JSON manifest as the authority, together with a
collision-aware path resolver. A normal album keeps the friendly path produced
by the current naming policy. A distinct release that collides with an occupied
friendly path receives a deterministic `[qobuz-<release_id>]` suffix.

A database-only mapping was rejected because it would be separated from the
music during a copy or restore. Adding IDs to every directory was rejected
because it would create unnecessary path churn. A generic `.nfo` file was
rejected because its extension is commonly used by media managers and does not
give Qobuz Librarian an unambiguous reserved namespace.

## Identity Manifest

The reserved filename is:

```text
.qobuz-librarian-release.json
```

Version 1 contains exactly the identity fields required for placement:

```json
{"schema_version":1,"provider":"qobuz","release_id":"123456789"}
```

The release ID is normalized to a non-empty string so numeric and textual API
representations compare identically. `provider` is fixed to `qobuz` in version
1 but remains explicit so the identity model is not tied implicitly to a
filename.

The identity component owns all manifest access. Reads must reject a symlink,
non-regular file, oversized document, invalid JSON, unsupported schema,
unknown provider, missing or extra fields, and an empty or malformed release
ID. Writes use a same-directory temporary regular file, flush it, and publish
it atomically without following a destination symlink. A pre-existing manifest
may be reused only when it contains the expected identity; it is never silently
replaced with a different release ID.

The manifest is written only after Qobuz Librarian has durable evidence that
the target directory represents the release. A failed or incomplete placement
must not leave an authoritative identity on the wrong directory.

## Reserved-Artifact Policy

The exact basename `.qobuz-librarian-release.json` is a Qobuz Librarian
internal artifact. A central predicate defines that policy alongside the
existing AppleDouble `name.startswith("._")` rule where appropriate.

Generic enumeration must exclude the manifest before extension or content
classification. In particular, it must not enter:

- audio scanning or album-track inventories;
- the exact library census;
- repair candidate collection;
- migration audio or companion receipts;
- Beets import inputs or staging-media discovery;
- artwork, lyrics, playlist, or other companion handling;
- generic consolidation comparisons; or
- progress and track counts.

This is an exact-name exclusion, not a blanket exclusion of dotfiles or JSON
files. Direct identity reads bypass generic enumeration and address the
reserved filename explicitly.

Qobuz Librarian-owned operations that must retain identity do not depend on
generic companion copying. Album moves and renames carry the validated
manifest as part of the album transaction. Backups, exports, and restores
include it through an explicit internal-metadata channel, validate it at both
ends, and preserve its exact contents. Thus the file is invisible to indexing
but portable with the release it identifies.

## Catalogue and Matching Semantics

The Qobuz release ID must survive every catalogue, discovery, gap-analysis,
download, completion, and placement boundary. Edition-stripped title and year
remain search and ranking signals only.

Catalogue deduplication may collapse repeated representations of the same
normalized Qobuz release ID. It must not collapse two different release IDs
merely because their normalized titles, artists, years, or track lists match.
When catalogue results contain multiple such releases, each remains a distinct
candidate until a manifest or a high-confidence legacy match selects one.

An existing valid manifest is authoritative. Discovery first reads it and
requests or selects that exact Qobuz release. Friendly path heuristics must not
substitute another edition if the manifest's release is unavailable; that
condition is reported instead.

## Legacy Folder Adoption

A legacy album directory has audio but no release manifest. It can be adopted
without changing its path only when all of these conditions hold:

1. normal discovery considers the folder a match for a Qobuz release;
2. every readable local audio track used as evidence maps consistently to that
   release by authoritative track identity where available, otherwise by the
   existing disc/track/title/duration evidence;
3. no local evidence contradicts the release;
4. no different Qobuz release is an equally plausible match for the observed
   tracks; and
5. the directory is still the same sealed directory with the same reviewed
   audio inventory when the manifest is published.

A partial album may be adopted when the observed subset uniquely identifies
one release. Shared core tracks between a standard and deluxe edition do not
qualify when both releases remain plausible. A successful new download/import
already supplies a specific release ID and does not need legacy inference.

If confidence is insufficient, the scan does not write a manifest, merge
files, or choose an edition. It records an edition-identity review item that
shows the folder, candidate release IDs and titles, and the evidence that made
the result ambiguous.

## Path Resolution

Path calculation has two separate outputs:

- a friendly base path from the current Beets-compatible naming rules; and
- an identity-resolved destination path.

For release `R`, resolution follows this order:

1. If the friendly base directory does not exist, reserve it for `R`. After
   successful placement, write `R`'s manifest there.
2. If it has a valid manifest for `R`, reuse it.
3. If it has no manifest and qualifies for high-confidence adoption as `R`,
   adopt and reuse it.
4. If it belongs to a different release, use the sibling path whose album
   component ends in `[qobuz-R]`.
5. If that suffixed directory has a valid manifest for `R`, reuse it.
6. Any invalid manifest, mismatched suffixed manifest, unadoptable unmarked
   directory, or namespace conflict stops automatic placement and creates a
   review item.

For example:

```text
Artist/Album (2020)/
Artist/Album (2020) [qobuz-987654321]/
```

The first release to occupy a previously empty friendly namespace retains the
ordinary name. The suffix is a deterministic collision disambiguator and a
useful visual clue, but the manifest remains authoritative. The suffix uses
the complete normalized release ID. Existing component-length limits are
applied while preserving the complete suffix; the friendly title portion is
truncated further when necessary.

Reservation and finalization must revalidate the directory identity,
manifest, audio inventory, and Beets/post-import result so concurrent imports
cannot assign the same namespace to different releases.

## Import, Completion, and Rollback

The planned release identity accompanies the staged download and the existing
completion evidence. Beets receives audio paths only; it never receives the
manifest as importable input. The post-import finalizer resolves the actual
album directory, verifies that imported tracks correspond to the planned
release and destination reservation, and then atomically publishes the
manifest.

If Beets selects the unsuffixed friendly directory already owned by another
release, finalization must not merge the results. The identity-aware relocation
step places the imported album in its reserved suffixed directory, updates the
corresponding Beets paths through the existing sealed database transaction,
and publishes the manifest only after both filesystem and database state are
consistent.

Rollback restores the prior filesystem and Beets state and removes only a
manifest created by that failed transaction. It never removes or rewrites a
pre-existing validated manifest. Completion is successful only when every
managed album directory has the expected manifest and no downloaded track was
placed under a different release identity.

## Moves, Consolidation, Migration, Backup, and Restore

- An identity-aware album rename or move validates and carries the manifest in
  the same reviewed operation as its audio.
- Consolidation may combine directories only when both lack identity and are
  otherwise proven equivalent, or when both manifests contain the same
  identity. Different release IDs are a hard non-merge boundary even if the
  audio overlaps.
- Generic migration source discovery excludes the manifest from audio and
  companion receipts. When the selected source album has a valid manifest,
  migration records it separately as internal metadata and transfers it only
  to a destination representing that same release.
- Backup/export records the validated manifest explicitly. Restore validates
  the record and refuses to overwrite a different destination identity.
- Invalid or ambiguous identity metadata makes the affected album review-only;
  unrelated albums may continue.

## User-Visible Behavior

Ordinary scans and imports retain their current paths. Logs and review output
refer to the friendly album label and show the Qobuz release ID where editions
must be distinguished.

### Library edition badges

Missing Albums and Gap Fill use the same edition-family calculation. An
edition family is keyed by normalized album artist, edition-stripped album
title, and original-release year. It contains only distinct normalized Qobuz
release IDs. This keeps unrelated same-titled albums from being grouped while
recognizing standard, deluxe, anniversary, and remastered releases of the same
work.

When the fetched artist catalogue contains two or more releases in one family,
every displayed candidate from that family receives a compact visible edition
badge. An album with no competing known release receives no badge. A partial
Gap Fill still receives the badge when its selected release belongs to a
multi-release family, even if the other releases are not themselves Gap Fill
candidates.

The badge always includes the complete Qobuz release ID. Its human label is
derived from the title decoration when possible, such as `Deluxe Edition`,
`20th Anniversary`, or `2011 Remaster`. The undecorated member is labeled
`Standard Edition`. If two releases produce the same human label, the minimum
additional differentiators needed to make the badges distinct are appended in
this order: original-release year, track count, then quality. The release ID
remains visible regardless of whether those fallbacks are needed.

Examples include:

```text
Standard Edition · Qobuz 100
Deluxe Edition · Qobuz 200
2011 Remaster · Qobuz 300
Standard Edition · 24-bit/96kHz · Qobuz 400
```

Discovery attaches the final display string as `edition_badge` in the saved
review-candidate payload. The shared review-row template renders that payload
the same way on the Missing Albums and Gap Fill tabs. The badge is purely
informational: it does not alter selection, grouping, download scope, or the
release ID submitted for execution. Saved reviews and scan checkpoints retain
the badge so it does not disappear after a restart.

An incomplete or failed catalogue fetch never invents competing editions. A
badge is shown only from the distinct releases actually validated in a
trustworthy catalogue response. Existing identity-attention cards remain
separate and non-actionable when Qobuz Librarian cannot select an edition at
all.

Identity review reasons are stable and explanatory, including:

- legacy folder matches multiple Qobuz releases;
- manifest is invalid or unsupported;
- manifest identity conflicts with the requested release;
- suffixed collision path is occupied by another identity; and
- identity changed between review and execution.

No automatic workflow offers to overwrite an identity conflict. A future
manual resolution feature may let the user select a candidate, but this design
does not make ambiguous adoption automatic.

## Error Handling and Safety Invariants

- A missing manifest means legacy/unmanaged, never "same release."
- A malformed manifest is an error, never equivalent to a missing manifest.
- A release ID match cannot override contradictory sealed filesystem evidence.
- A path-name match cannot override a different manifest identity.
- Different release IDs never merge automatically.
- Generic scanners never process the manifest, regardless of its contents.
- Internal metadata transfer never follows symlinks or trusts an unsealed path.
- Cancellation or failure cannot publish a manifest before durable album
  placement, and cannot leave Beets and filesystem identity disagreeing.

## Testing

Tests will prove that:

1. A first release keeps the existing friendly path and receives a valid
   manifest after successful import.
2. A repeat import of the same release reuses that directory.
3. A distinct release with the same friendly path receives the deterministic
   Qobuz-ID suffix.
4. Distinct release IDs survive catalogue deduplication even when edition
   decorations normalize to the same title.
5. A uniquely matched complete or partial legacy folder is adopted without a
   rename.
6. A legacy folder compatible with standard and deluxe releases remains
   unmodified and produces a review item.
7. Missing, corrupt, oversized, unsupported, symlinked, and conflicting
   manifests all fail safely.
8. The scanner, census, repair, migration companion collector, consolidation,
   and Beets input builder never classify the reserved manifest as media or a
   generic sidecar.
9. Existing `._*` AppleDouble exclusions continue to work for both audio-like
   and companion-like filenames.
10. Album moves, migration internal metadata, backup/export, and restore retain
    the manifest deliberately and reject cross-release overwrites.
11. A Beets placement collision is relocated without merging editions, with
    filesystem and Beets rows updated consistently.
12. Concurrent namespace changes, interruption, or finalization failure leave
    no false manifest and restore the prior state.
13. Long friendly names are truncated without truncating or changing the ID
    suffix.
14. The full test suite passes with unchanged paths for non-colliding albums.
15. Missing Albums shows a visible human edition label and complete Qobuz ID
    for every member of a multi-release edition family.
16. Gap Fill shows the same badge when the owned release has competing
    catalogue editions, even if only that release has missing tracks.
17. A single-release family shows no edition badge, and unrelated same-titled
    albums from different original-release years are not grouped.
18. Duplicate human labels gain only the year, track-count, or quality detail
    required to distinguish them, while retaining the complete release ID.
19. Saved review and checkpoint round trips preserve `edition_badge`, and the
    shared row template renders it on both Library tabs without changing the
    selected album ID.

## Deployment and Compatibility

Deployment does not require an eager library rewrite. Existing folders remain
valid legacy folders and acquire manifests lazily only through a successful
identity-bearing import or high-confidence scan adoption. Ambiguous folders
remain untouched for review.

Navidrome sees the same supported audio paths for ordinary albums and ignores
the hidden JSON as a non-audio file. Colliding editions appear as separate
album directories, while their audio tags remain available to Navidrome's own
album grouping rules. Rolling back the application leaves harmless hidden
manifests in place; older Qobuz Librarian versions do not classify `.json` as
audio, though they will not enforce the new identity boundary.
