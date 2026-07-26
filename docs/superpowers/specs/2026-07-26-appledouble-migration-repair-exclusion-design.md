# AppleDouble Exclusion for Migration and Repair

## Goal

Library migration and repair must ignore files whose basename begins with
`._`. macOS creates these AppleDouble sidecar files on filesystems that cannot
store all metadata natively; they are not music or useful album companions.

## Scope

- Migration must exclude every regular file whose basename begins with `._`
  before classifying it as audio or a companion.
- Repair must exclude every FLAC file whose basename begins with `._` before
  opening or diagnosing it.
- The rule applies at every directory depth below the selected source or album.
- The rule is based only on the file basename. It does not exclude ordinary
  dotfiles or descendents merely because an ancestor directory begins with
  `._`.
- Other library scans retain their current behavior.

## Design

Apply the exclusion at each operation's file-enumeration boundary:

1. In migration's descriptor-based source walk, skip regular files when
   `name.startswith("._")`, before extension classification, progress
   reporting, metadata reads, and receipt creation.
2. In repair's FLAC path collection, omit paths when
   `path.name.startswith("._")`, before opening or diagnostic work.

This targeted placement ensures ignored files never enter the migration plan,
companion receipts, or repair report. A global change to the shared
no-symlink tree iterator is intentionally avoided because it would alter
unrelated library features.

## Error Handling

Ignoring a matching file is normal selection behavior and produces no warning
or error. Existing filesystem error handling remains unchanged for all other
entries.

## Testing

- Add a migration regression test with valid-looking `._` audio and companion
  names and assert neither is collected.
- Add a repair regression test with a `._*.flac` file and assert repair does
  not try to open or diagnose it.
- Keep a normally named control file in each test to prove enumeration still
  processes eligible files.
- Run the focused migration and repair test modules, then the full test suite.
