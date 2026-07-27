# Exact Read-Only Library Census

## Problem

The Library page labels its census as “What’s on disk,” but the current total
comes from positive rows in `flac_cache.db`. Those rows are created only when a
code path parses a file. A library scan can finish without parsing every local
album—for example, when Qobuz artist resolution or album matching stops
early—so valid supported audio files can be absent from the displayed total.
Cached negative rows are also excluded, which drops tagless or unparseable
audio even though the scanner can still represent those tracks from filenames.

The production diagnostic found 44,477 supported audio files but only 28,116
positive cache rows. The missing files were readable, were not behind symlinked
directories, and had no recorded parse failures. This confirms that the tag
cache is not a reliable inventory.

## Goals

- Make the census total equal every supported audio file found in the completed
  library inventory, excluding files whose basename starts with `._`.
- Classify parseable files into the existing CD, hi-res, and unknown tiers.
- Count tagless or unparseable supported audio as Unknown.
- Keep the census fast to render.
- Never write, rename, retag, delete, or change timestamps beneath
  `MUSIC_ROOT`.
- Publish a new census only after a complete inventory, so cancellation or a
  read error cannot replace a known-good snapshot with a partial count.

## Non-goals

- Changing Qobuz artist or album matching.
- Repairing media tags.
- Following symlinked directories.
- Counting unsupported files such as artwork, lyrics, or sidecars.
- Running a fresh filesystem walk on each page request.

## Design

### Inventory pass

Add a small library-census component that performs a read-only walk beneath
`MUSIC_ROOT` using the scanner’s no-symlink traversal rules. It accepts files
whose lowercase suffix is in `AUDIO_EXTS` and rejects any file whose basename
starts with `._`.

For each accepted file, the inventory obtains metadata through
`read_audio_meta()`. This may populate the derived SQLite cache under
`DATA_DIR`, but opens the media only for reading. A positive metadata result is
classified with the current census tier rules. A negative result is counted in
Unknown using the file signature’s size. The component accumulates the existing
census fields in memory: per-tier track counts and bytes, total tracks and
bytes, hi-res bytes by top-level artist, and estimated downsample reclaim.

The inventory exposes progress by file count where practical and honors the
library job’s cancellation flag. Traversal, stat, or media-read errors make the
inventory incomplete.

### Durable snapshot

On successful completion, atomically write the accumulated census to a
versioned JSON snapshot under `DATA_DIR`. The snapshot represents exactly the
set observed by that completed inventory, so stale metadata-cache rows cannot
inflate it and unmatched albums cannot disappear from it.

If the inventory is cancelled or encounters an error, do not replace the
previous snapshot. Report the incomplete inventory in the activity log and let
the rest of the scan use its existing error behavior.

The Library page reads this snapshot. For backward compatibility after upgrade,
it may fall back to the existing cache-derived census until the first successful
post-upgrade library inventory creates a snapshot.

### Scan integration

Run the inventory once during a full Library scan’s existing “Reading albums on
disk” phase, before Qobuz artist resolution and matching. It is independent of
whether an artist or album can be resolved on Qobuz.

The pass must not run as part of a page request. Cheap scans may reuse the most
recent completed snapshot when the filesystem fingerprints establish that the
library is unchanged; a forced full scan always rebuilds it.

### Census semantics

- Supported, parseable audio: classify as CD, hi-res up to 96 kHz, hi-res above
  96 kHz, or Unknown using the current rules.
- Supported, tagless or unparseable audio: count as Unknown.
- `._*` files: ignore entirely, even when their suffix appears supported.
- Symlinked directories: do not traverse.
- Removed or renamed files: disappear from the next completed snapshot.
- Files added during a scan have normal snapshot semantics: they appear only if
  the traversal observes them. A later scan reconciles concurrent changes.

## Error handling

- A missing or unreadable `MUSIC_ROOT` cannot publish an empty census.
- Any traversal or file-read error preserves the prior snapshot.
- Failure to write the snapshot is logged and preserves the prior snapshot.
- A corrupt or unsupported snapshot version is ignored; the UI uses the legacy
  cache fallback until a new snapshot is saved.
- Metadata cache failures do not prevent counting a readable supported file as
  Unknown.

## Testing

Tests will prove that:

1. Supported files absent from `flac_cache.db` are included after inventory.
2. Positive cached metadata retains the expected quality tier.
3. Negative, tagless, or unparseable audio counts as Unknown.
4. Files beginning with `._` are excluded.
5. Unsupported files and symlinked-directory contents are excluded.
6. A cancelled or errored inventory does not replace the last good snapshot.
7. A completed second inventory removes files no longer present from the
   census.
8. The inventory opens no media path for writing and changes no media content
   or timestamps.
9. The web census consumes the durable snapshot and retains the legacy fallback
   before a snapshot exists.

## Deployment behavior

Deploying the code does not touch the music library and does not interrupt an
active repair. The currently displayed cache-derived total remains available
until a subsequent completed Library scan creates the first exact census
snapshot.
