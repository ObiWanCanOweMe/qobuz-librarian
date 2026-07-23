# Statx Directory Identity Mask Fix

## Problem

Migration preview captures a filesystem identity for each relevant directory so
later execution can prove that it is operating on the reviewed directory
incarnation. `_statx_directory_identity()` currently requests and requires
`STATX_BASIC_STATS | STATX_BTIME | STATX_MNT_ID`.

`STATX_BASIC_STATS` includes `STATX_ATIME`, although access time is not used in
the identity. A ZFS dataset with access-time tracking disabled can therefore
return every field needed for the proof while omitting `STATX_ATIME`; the current
complete-mask comparison rejects that valid result.

## Scope

This change is limited to the `statx` field-mask contract and direct regression
tests. It does not change migration receipts, the web or CLI flows, macOS
behavior, user-facing documentation, or the changelog.

## Design

Define the individual `statx` mask constants for the fields consumed by the
directory identity and combine them into one internal required mask:

- file type (`STATX_TYPE`), used to require a directory;
- permission and special mode bits (`STATX_MODE`), stored in the identity;
- inode number (`STATX_INO`), stored in the identity;
- birth time (`STATX_BTIME`), used to distinguish inode incarnations; and
- mount ID (`STATX_MNT_ID`), used to distinguish mounted views.

Use this same mask as the syscall request and as the returned-mask requirement.
The `statx` API has no separate returned-mask bit for `stx_dev_major` and
`stx_dev_minor`; those fields remain read from the successful result and are
cross-checked against `fstat` for descriptor-based identities as they are today.

The function remains fail-closed when any consumed field is unavailable, when
the birth-time nanosecond value is invalid, or when the reported object is not a
directory. No receipt format changes are needed because the returned identity
list is unchanged.

## Testing

Add focused tests around `_statx_directory_identity()` using a fake `statx`
callable that populates the real ctypes `_Statx` structure. The tests will:

1. verify that a directory result containing all consumed fields but no
   `STATX_ATIME` is accepted;
2. verify that the syscall receives exactly the explicit consumed-field mask;
3. verify that omitting `STATX_BTIME` is rejected; and
4. verify that omitting `STATX_MNT_ID` is rejected.

These tests exercise the syscall boundary without requiring Linux or a ZFS
fixture, so they remain deterministic on the local macOS checkout and in Linux
CI. Existing migration behavior outside the mask contract is unchanged.
