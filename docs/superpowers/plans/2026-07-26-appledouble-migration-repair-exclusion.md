# AppleDouble Migration and Repair Exclusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make library migration and repair ignore every regular file whose basename begins with `._`.

**Architecture:** Add an operation-local basename guard at each file-enumeration boundary. Migration filters before extension classification and receipt creation; repair filters before opening FLACs for diagnosis, leaving the shared tree walker and unrelated library features unchanged.

**Tech Stack:** Python 3, `pathlib`, descriptor-relative filesystem APIs, pytest.

## Global Constraints

- Migration must exclude every regular file whose basename begins with `._` before classifying it as audio or a companion.
- Repair must exclude every FLAC file whose basename begins with `._` before opening or diagnosing it.
- The rule applies at every directory depth below the selected source or album.
- The rule is based only on the file basename. It does not exclude ordinary dotfiles or descendents merely because an ancestor directory begins with `._`.
- Other library scans retain their current behavior.
- Ignoring a matching file produces no warning or error.

---

## File Structure

- Modify `src/qobuz_librarian/library/migrate.py`: enforce the migration-specific exclusion in the descriptor-based source enumerator.
- Modify `tests/test_migrate.py`: cover audio and companion selection with literal `._` and normal control filenames.
- Modify `src/qobuz_librarian/repair_log.py`: enforce the repair-specific exclusion in FLAC path collection.
- Modify `tests/test_ui_and_repair.py`: cover repair's observable scan result with one normal FLAC and one ignored AppleDouble FLAC.

### Task 1: Exclude AppleDouble Files From Migration

**Files:**

- Modify: `tests/test_migrate.py`
- Modify: `src/qobuz_librarian/library/migrate.py:2296-2299`

**Interfaces:**

- Consumes: `_enumerate_source_descriptors(binding, *, cancel_check=None, progress=None) -> tuple[list, list]`
- Produces: The same return type, with no audio tuple or companion receipt for a regular file whose basename starts with `._`.

- [ ] **Step 1: Write the failing migration enumeration test**

Add a test that exercises the real descriptor walk while replacing only the
receipt/tag-sealing boundary that requires platform-specific writer leases:

```python
def test_migration_enumeration_ignores_appledouble_audio_and_companions(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("track.flac", "._track.flac", "cover.jpg", "._cover.jpg"):
        (source / name).write_bytes(name.encode())

    class Binding:
        path = source
        root_fd = m.os.open(source, m.os.O_RDONLY | m.os.O_DIRECTORY)

        def matches_public(self):
            return True

    def seal(_binding, _parent_fd, _parents, relative, *, read_tags=False):
        path = source.joinpath(*relative)
        receipt = {"relative": list(relative)}
        return path, _meta() if read_tags else None, receipt

    binding = Binding()
    monkeypatch.setattr(m, "_scan_file_from_descriptor", seal)
    monkeypatch.setattr(
        m, "_sealed_directory_chain_matches", lambda *_args: True)
    try:
        audio, companions = m._enumerate_source_descriptors(binding)
    finally:
        m.os.close(binding.root_fd)

    assert [path.name for path, _meta_value, _receipt in audio] == [
        "track.flac"]
    assert [receipt["relative"][-1] for receipt in companions] == ["cover.jpg"]
```

This test catches removal or misplacement of the basename guard: without it,
the result contains `._track.flac` and `._cover.jpg`.

- [ ] **Step 2: Run the migration test and verify RED**

Run:

```bash
pytest -q tests/test_migrate.py::test_migration_enumeration_ignores_appledouble_audio_and_companions
```

Expected: FAIL because the audio names include `._track.flac` and the companion
names include `._cover.jpg`.

- [ ] **Step 3: Implement the minimal migration guard**

In the `stat.S_ISREG(value.st_mode)` branch of
`_enumerate_source_descriptors`, skip the basename before computing its suffix:

```python
elif stat.S_ISREG(value.st_mode):
    if name.startswith("._"):
        continue
    suffix = Path(name).suffix.lower()
```

- [ ] **Step 4: Run focused migration tests and verify GREEN**

Run:

```bash
pytest -q tests/test_migrate.py
```

Expected: all migration tests PASS.

- [ ] **Step 5: Commit the migration change**

```bash
git add tests/test_migrate.py src/qobuz_librarian/library/migrate.py
git commit -m "fix: ignore AppleDouble files during migration"
```

### Task 2: Exclude AppleDouble FLACs From Repair

**Files:**

- Modify: `tests/test_ui_and_repair.py`
- Modify: `src/qobuz_librarian/repair_log.py:385-390`

**Interfaces:**

- Consumes: `_repair_flac_paths(album_dir) -> list[Path]`
- Produces: A sorted FLAC path list with no path whose basename starts with `._`; `scan_dir_for_isrc_repairs(...)` therefore never opens or diagnoses those files.

- [ ] **Step 1: Write the failing repair scan test**

Add a test beside the existing truncation-gate tests. The mocks isolate Qobuz
and decode details while the real path enumeration, held-file opening, and
report aggregation remain under test:

```python
def test_repair_scan_ignores_appledouble_flacs(tmp_path):
    source = tmp_path / "track.flac"
    source.write_bytes(b"held source")
    (tmp_path / "._track.flac").write_bytes(b"AppleDouble sidecar")
    track = _track(length=10.0, path=str(source))
    qobuz_track = {
        "duration": 0,
        "title": "Track",
        "track_number": 1,
    }

    with patch(
            "qobuz_librarian.repair_log._read_held_audio_meta",
            return_value=track), patch(
            "qobuz_librarian.repair_log._qobuz_track_by_isrc",
            return_value=qobuz_track), patch(
            "qobuz_librarian.repair_log._flac_decode_ok",
            return_value=True):
        report = scan_dir_for_isrc_repairs(tmp_path, "token")

    assert report["verified_ok"] == 1
    assert report["unverified"] == 0
```

This test catches removal or misplacement of the guard: without it, both
regular files are diagnosed and `verified_ok` is `2`.

- [ ] **Step 2: Run the repair test and verify RED**

Run:

```bash
pytest -q tests/test_ui_and_repair.py::test_repair_scan_ignores_appledouble_flacs
```

Expected: FAIL with `assert 2 == 1` for `report["verified_ok"]`.

- [ ] **Step 3: Implement the minimal repair guard**

Change `_repair_flac_paths` to require both a FLAC suffix and a basename that
does not start with `._`:

```python
for path in iter_tree_no_symlinks(Path(album_dir)):
    if not path.name.startswith("._") and path.suffix.lower() == ".flac":
        paths.append(path)
```

- [ ] **Step 4: Run focused repair tests and verify GREEN**

Run:

```bash
pytest -q tests/test_ui_and_repair.py tests/test_repair_accuracy.py
```

Expected: all repair tests PASS.

- [ ] **Step 5: Commit the repair change**

```bash
git add tests/test_ui_and_repair.py src/qobuz_librarian/repair_log.py
git commit -m "fix: ignore AppleDouble files during repair"
```

### Task 3: Verify the Integrated Change

**Files:**

- Verify only; no planned file modifications.

**Interfaces:**

- Consumes: migration and repair behavior completed in Tasks 1 and 2.
- Produces: evidence that both focused regressions and the full project remain green.

- [ ] **Step 1: Run both regression tests together**

Run:

```bash
pytest -q \
  tests/test_migrate.py::test_migration_enumeration_ignores_appledouble_audio_and_companions \
  tests/test_ui_and_repair.py::test_repair_scan_ignores_appledouble_flacs
```

Expected: `2 passed`.

- [ ] **Step 2: Run the full Python test suite**

Run:

```bash
pytest -q
```

Expected: all tests PASS with no failures or errors.

- [ ] **Step 3: Check the final diff and worktree**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` emits no output. The implementation files are
clean after their task commits; only the implementation-plan document may
remain uncommitted.

- [ ] **Step 4: Commit the implementation plan**

```bash
git add docs/superpowers/plans/2026-07-26-appledouble-migration-repair-exclusion.md
git commit -m "docs: plan AppleDouble scan exclusions"
```
