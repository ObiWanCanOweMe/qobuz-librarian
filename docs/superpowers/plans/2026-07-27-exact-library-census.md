# Exact Read-Only Library Census Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cache-row estimate with an exact, durable census of every supported audio file observed by a completed read-only library inventory.

**Architecture:** A focused `library.census` module will walk `MUSIC_ROOT` without following symlinked directories, aggregate quality data in memory, and atomically persist a versioned snapshot under `DATA_DIR`. `scan_library()` will refresh that snapshot independently of Qobuz matching, and the web view will prefer it while retaining the current cache-derived result as a pre-upgrade fallback.

**Tech Stack:** Python 3.12+, `pathlib`, `dataclasses`, JSON atomic replacement, existing Mutagen-backed scanner, pytest, Ruff.

## Global Constraints

- Never write, rename, retag, delete, or change timestamps beneath `MUSIC_ROOT`.
- Count every supported audio file observed by a completed inventory.
- Exclude every file whose basename starts with `._`.
- Count tagless and unparseable supported audio in the Unknown tier.
- Never follow symlinked directories.
- Never replace a known-good census snapshot with a cancelled, unreadable, or otherwise incomplete inventory.
- Never run a filesystem inventory during a page request.
- Keep the existing cache-derived census as a fallback until the first successful post-upgrade inventory.
- Do not change Qobuz artist or album matching.

---

### Task 1: Build and persist an exact read-only census

**Files:**
- Create: `src/qobuz_librarian/library/census.py`
- Create: `tests/test_library_census.py`
- Modify: `src/qobuz_librarian/config.py:261-276`
- Modify: `tests/conftest.py:30-45`

**Interfaces:**
- Consumes: `scanner.iter_tree_no_symlinks(root, errors)`, `scanner.read_audio_meta(path)`, `flac_cache.signature(path)`, `cfg.AUDIO_EXTS`, `cfg.MUSIC_ROOT`, and `cfg.LIBRARY_CENSUS_FILE`.
- Produces:
  - `InventoryResult(data: dict | None, complete: bool, processed: int, errors: list[str], cancelled: bool)`
  - `build(root: Path | None = None, *, cancel_check: Callable[[], bool] | None = None, on_file: Callable[[int, Path], None] | None = None) -> InventoryResult`
  - `save(data: dict) -> bool`
  - `load() -> dict | None`

- [ ] **Step 1: Add the failing inventory tests**

Create `tests/test_library_census.py` with real filesystem entries and a
controlled metadata-reader boundary:

```python
import json
from pathlib import Path


def _meta(bits, sample_rate, size):
    return {
        "bits": bits,
        "sample_rate": sample_rate,
        "size": size,
        "path": "",
    }


def _raw_snapshot(total):
    return {
        "version": 1,
        "tiers": {
            "cd": [total, total * 10],
            "hires96": [0, 0],
            "hires192": [0, 0],
            "unknown": [0, 0],
        },
        "total_tracks": total,
        "total_bytes": total * 10,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }


def test_build_counts_every_supported_file_and_excludes_appledouble_and_symlinks(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import census

    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    files = {
        "cd.flac": _meta(16, 44100, 100),
        "h96.flac": _meta(24, 96000, 1000),
        "h192.flac": _meta(24, 192000, 2000),
        "unknown.mp3": None,
    }
    for name in files:
        (album / name).write_bytes(b"audio")
    (album / "._ghost.flac").write_bytes(b"AppleDouble")
    (album / "cover.jpg").write_bytes(b"image")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.flac").write_bytes(b"audio")
    (album / "linked").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    monkeypatch.setattr(
        census.scanner,
        "read_audio_meta",
        lambda path: files[path.name],
    )

    result = census.build()

    assert result.complete is True
    assert result.processed == 4
    assert result.data["total_tracks"] == 4
    assert result.data["total_bytes"] == 3105
    assert result.data["tiers"] == {
        "cd": [1, 100],
        "hires96": [1, 1000],
        "hires192": [1, 2000],
        "unknown": [1, 5],
    }
    assert result.data["reclaim_bytes"] == 500 + 1500
    assert result.data["top_hires_artists"] == [("Artist", 3000)]


def test_build_reads_media_without_changing_content_or_mtime(tmp_path, monkeypatch):
    from qobuz_librarian.library import census

    root = tmp_path / "music"
    track = root / "Artist" / "Album" / "track.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"not-a-real-flac")
    before = (track.read_bytes(), track.stat().st_mtime_ns)
    monkeypatch.setattr(census.flac_cache, "_ensure", lambda: False)

    result = census.build(root)

    after = (track.read_bytes(), track.stat().st_mtime_ns)
    assert result.complete is True
    assert result.data["total_tracks"] == 1
    assert result.data["tiers"]["unknown"][0] == 1
    assert after == before
```

- [ ] **Step 2: Run the inventory tests to verify RED**

Run:

```bash
pytest -q tests/test_library_census.py
```

Expected: FAIL during import because `qobuz_librarian.library.census` does not
exist.

- [ ] **Step 3: Add the census snapshot path**

Add beside the other `DATA_DIR`-derived state paths in `config.py`:

```python
LIBRARY_CENSUS_FILE = DATA_DIR / ".qobuz_library_census.json"
```

Re-derive it in the session isolation fixture:

```python
cfg.LIBRARY_CENSUS_FILE = tmp_root / ".qobuz_library_census.json"
```

- [ ] **Step 4: Implement the minimal inventory and aggregation module**

Create `src/qobuz_librarian/library/census.py`. Use the existing quality rules
exactly: missing bit depth/sample rate is Unknown; `bits <= 16` is CD;
otherwise `sample_rate <= 96000` is hi-res up to 96 kHz; higher rates are
hi-res up to 192 kHz.

```python
"""Exact, read-only library census persisted as a small derived snapshot."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Callable

from qobuz_librarian import config as cfg
from qobuz_librarian.library import flac_cache, scanner


STATE_VERSION = 1


@dataclass
class InventoryResult:
    data: dict | None
    complete: bool
    processed: int
    errors: list[str]
    cancelled: bool = False


def _empty():
    return {
        "version": STATE_VERSION,
        "tiers": {
            "cd": [0, 0],
            "hires96": [0, 0],
            "hires192": [0, 0],
            "unknown": [0, 0],
        },
        "total_tracks": 0,
        "total_bytes": 0,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }


def _tier(meta):
    bits = int((meta or {}).get("bits") or 0)
    sample_rate = int((meta or {}).get("sample_rate") or 0)
    if not bits or not sample_rate:
        return "unknown", bits, sample_rate
    if bits <= 16:
        return "cd", bits, sample_rate
    if sample_rate <= 96000:
        return "hires96", bits, sample_rate
    return "hires192", bits, sample_rate


def build(
    root: Path | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_file: Callable[[int, Path], None] | None = None,
) -> InventoryResult:
    root = Path(root or cfg.MUSIC_ROOT)
    if not root.is_dir():
        return InventoryResult(None, False, 0, [f"{root} is not a readable directory"])
    data = _empty()
    errors = []
    hires_by_artist = {}
    processed = 0
    exts = set(cfg.AUDIO_EXTS)

    try:
        entries = scanner.iter_tree_no_symlinks(root, errors=errors)
        for path in entries:
            if cancel_check is not None and cancel_check():
                return InventoryResult(
                    None, False, processed, [str(error) for error in errors], True)
            if path.name.startswith("._") or path.suffix.lower() not in exts:
                continue
            try:
                if not path.is_file():
                    continue
                sig = flac_cache.signature(path)
                if sig is None:
                    errors.append(f"could not stat {path}")
                    continue
                meta = scanner.read_audio_meta(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue

            processed += 1
            tier, _bits, sample_rate = _tier(meta)
            size = int((meta or {}).get("size") or sig[1] or 0)
            data["tiers"][tier][0] += 1
            data["tiers"][tier][1] += size
            data["total_tracks"] += 1
            data["total_bytes"] += size
            if tier in ("hires96", "hires192"):
                target = 44100 if sample_rate % 44100 == 0 else 48000
                if sample_rate > target:
                    data["reclaim_bytes"] += int(
                        size * (1 - target / sample_rate))
                try:
                    artist = path.relative_to(root).parts[0]
                except (ValueError, IndexError):
                    artist = ""
                if artist:
                    hires_by_artist[artist] = hires_by_artist.get(artist, 0) + size
            if on_file is not None:
                on_file(processed, path)
    except OSError as exc:
        errors.append(f"{root}: {exc}")

    data["top_hires_artists"] = sorted(
        hires_by_artist.items(), key=lambda item: -item[1])[:5]
    return InventoryResult(
        data if not errors else None,
        not errors,
        processed,
        [str(error) for error in errors],
    )
```

Keep `build()` free of persistence so an incomplete result cannot accidentally
overwrite state.

- [ ] **Step 5: Run the inventory tests to verify GREEN**

Run:

```bash
pytest -q tests/test_library_census.py
```

Expected: 2 passed.

- [ ] **Step 6: Add failing persistence and incomplete-inventory tests**

Append:

```python
def test_save_load_is_versioned_and_atomic(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import census

    path = tmp_path / "data" / "census.json"
    monkeypatch.setattr(cfg, "LIBRARY_CENSUS_FILE", path)
    payload = {
        "version": census.STATE_VERSION,
        "tiers": {"cd": [1, 10], "hires96": [0, 0],
                  "hires192": [0, 0], "unknown": [0, 0]},
        "total_tracks": 1,
        "total_bytes": 10,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }

    assert census.save(payload) is True
    assert census.load() == payload
    path.write_text(json.dumps({"version": 999}), encoding="utf-8")
    assert census.load() is None


def test_cancel_and_walk_error_return_no_publishable_data(tmp_path, monkeypatch):
    from qobuz_librarian.library import census

    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "one.flac").write_bytes(b"audio")

    cancelled = census.build(root, cancel_check=lambda: True)
    assert cancelled.cancelled is True
    assert cancelled.complete is False
    assert cancelled.data is None

    def broken_walk(_root, errors=None):
        errors.append(OSError("EIO"))
        return iter(())

    monkeypatch.setattr(census.scanner, "iter_tree_no_symlinks", broken_walk)
    failed = census.build(root)
    assert failed.complete is False
    assert failed.data is None
    assert "EIO" in str(failed.errors[0])


def test_second_completed_snapshot_drops_removed_file(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import census

    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    keep = album / "keep.flac"
    remove = album / "remove.flac"
    keep.write_bytes(b"keep")
    remove.write_bytes(b"remove")
    monkeypatch.setattr(cfg, "LIBRARY_CENSUS_FILE", tmp_path / "census.json")
    monkeypatch.setattr(census.flac_cache, "_ensure", lambda: False)

    first = census.build(root)
    assert first.complete and census.save(first.data)
    assert census.load()["total_tracks"] == 2

    remove.unlink()
    second = census.build(root)
    assert second.complete and census.save(second.data)
    assert census.load()["total_tracks"] == 1


def test_failed_snapshot_replace_preserves_previous_snapshot(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import census

    path = tmp_path / "census.json"
    monkeypatch.setattr(cfg, "LIBRARY_CENSUS_FILE", path)
    original = _raw_snapshot(1)
    replacement = _raw_snapshot(2)
    assert census.save(original)
    monkeypatch.setattr(
        census.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("full")))

    assert census.save(replacement) is False
    assert json.loads(path.read_text(encoding="utf-8"))["total_tracks"] == 1
```

- [ ] **Step 7: Run the new tests to verify RED**

Run:

```bash
pytest -q tests/test_library_census.py
```

Expected: FAIL because `save()` and `load()` are not defined.

- [ ] **Step 8: Implement atomic snapshot persistence and validation**

Add `save()` and `load()` to `library/census.py`. `save()` must create the data
directory, write UTF-8 JSON to a temporary file in that same directory, flush
and close it, then `os.replace()` the configured snapshot. It returns `False`
on `OSError` and removes a leftover temporary file. `load()` returns `None` for
a missing file, invalid JSON, a non-dict payload, the wrong version, missing
tier keys, or malformed aggregate fields.

Use this write shape:

```python
def save(data):
    path = cfg.LIBRARY_CENSUS_FILE
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".qobuz_library_census.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.replace(tmp, path)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
```

Implement validation directly in `load()`:

```python
def load():
    try:
        data = json.loads(
            cfg.LIBRARY_CENSUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    tiers = data.get("tiers")
    tier_names = ("cd", "hires96", "hires192", "unknown")
    if not isinstance(tiers, dict) or set(tiers) != set(tier_names):
        return None
    if any(
        not isinstance(tiers[name], list)
        or len(tiers[name]) != 2
        or any(not isinstance(value, int) or value < 0 for value in tiers[name])
        for name in tier_names
    ):
        return None
    numeric = ("total_tracks", "total_bytes", "reclaim_bytes")
    if any(not isinstance(data.get(name), int) or data[name] < 0
           for name in numeric):
        return None
    top = data.get("top_hires_artists")
    if not isinstance(top, list) or any(
        not isinstance(row, list)
        or len(row) != 2
        or not isinstance(row[0], str)
        or not isinstance(row[1], int)
        or row[1] < 0
        for row in top
    ):
        return None
    data["top_hires_artists"] = [(name, size) for name, size in top]
    return data
```

This normalization keeps `load()["top_hires_artists"]` compatible with the
existing `flac_cache.census()` API.

- [ ] **Step 9: Run focused tests and lint**

Run:

```bash
pytest -q tests/test_library_census.py tests/test_scanner_cache.py
ruff check src/qobuz_librarian/library/census.py tests/test_library_census.py src/qobuz_librarian/config.py tests/conftest.py
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 10: Commit Task 1**

```bash
git add src/qobuz_librarian/library/census.py src/qobuz_librarian/config.py tests/conftest.py tests/test_library_census.py
git commit -m "feat: add exact read-only library census"
```

---

### Task 2: Refresh the census during whole-library scans

**Files:**
- Modify: `src/qobuz_librarian/web/flows.py:772-862`
- Modify: `tests/test_scan_unification.py`

**Interfaces:**
- Consumes: `census.build(cancel_check=..., on_file=...) -> InventoryResult` and `census.save(data) -> bool` from Task 1.
- Produces: `_refresh_library_census(job) -> InventoryResult`, called once by every `scan_library()` invocation before Qobuz artist matching.

- [ ] **Step 1: Add failing scan-integration tests**

Append to `tests/test_scan_unification.py`:

```python
def test_refresh_library_census_publishes_only_complete_inventory(monkeypatch):
    from qobuz_librarian.library import census
    from qobuz_librarian.web import flows

    payload = {
        "version": 1,
        "tiers": {"cd": [1, 10], "hires96": [0, 0],
                  "hires192": [0, 0], "unknown": [0, 0]},
        "total_tracks": 1,
        "total_bytes": 10,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }
    saved = []
    monkeypatch.setattr(
        flows.library_census,
        "build",
        lambda **kwargs: census.InventoryResult(payload, True, 1, []),
    )
    monkeypatch.setattr(
        flows.library_census, "save", lambda data: saved.append(data) or True)

    result = flows._refresh_library_census(jm.Job(title="scan"))

    assert result.complete is True
    assert saved == [payload]


def test_refresh_library_census_preserves_snapshot_on_cancel(monkeypatch):
    from qobuz_librarian.library import census
    from qobuz_librarian.web import flows

    job = jm.Job(title="scan")
    job.cancel_requested = True
    monkeypatch.setattr(
        flows.library_census,
        "build",
        lambda **kwargs: census.InventoryResult(None, False, 0, [], True),
    )
    monkeypatch.setattr(
        flows.library_census,
        "save",
        lambda _data: (_ for _ in ()).throw(
            AssertionError("cancelled census must not be saved")),
    )

    result = flows._refresh_library_census(job)

    assert result.cancelled is True
```

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
pytest -q \
  tests/test_scan_unification.py::test_refresh_library_census_publishes_only_complete_inventory \
  tests/test_scan_unification.py::test_refresh_library_census_preserves_snapshot_on_cancel
```

Expected: FAIL because `flows.library_census` and
`_refresh_library_census()` do not exist.

- [ ] **Step 3: Implement the refresh helper**

Import the module with the other library-state modules:

```python
from qobuz_librarian.library import census as library_census
```

Add a helper immediately above `scan_library()`:

```python
def _refresh_library_census(job):
    last_tick = {"processed": 0}

    def on_file(processed, path):
        if processed - last_tick["processed"] >= 250:
            last_tick["processed"] = processed
            job.push_progress(
                "Inventorying audio files",
                processed,
                0,
                path.name,
                unit="track",
            )

    result = library_census.build(
        cancel_check=lambda: bool(job.cancel_requested),
        on_file=on_file,
    )
    if result.complete and result.data is not None:
        if not library_census.save(result.data):
            log.info("  Exact library census could not be saved; keeping the previous snapshot.")
    elif result.errors:
        log.info(
            f"  Exact library census incomplete after {result.processed} files; "
            "keeping the previous snapshot.")
    return result
```

Log only the aggregate incomplete message shown above; do not print one normal
activity-feed line per unreadable file.

- [ ] **Step 4: Run helper tests to verify GREEN**

Run the two focused tests from Step 2.

Expected: 2 passed.

- [ ] **Step 5: Add a failing call-order test**

Add a test proving the exact inventory is independent of Qobuz matching:

```python
def test_scan_library_refreshes_census_before_qobuz_artist_scan(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist = tmp_path / "Artist"
    artist.mkdir()
    order = []
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(
        flows,
        "_refresh_library_census",
        lambda _job: order.append("census"),
    )
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult(
            [], ["Artist"], {}, True),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, *_a, **_k: (
            order.append("qobuz") or
            (ad.name, ad.name, [], "artist-id", [])
        ),
    )

    flows.scan_library(jm.Job(title="scan"), "token")

    assert order[:2] == ["census", "qobuz"]
```

- [ ] **Step 6: Run the call-order test to verify RED**

Run:

```bash
pytest -q tests/test_scan_unification.py::test_scan_library_refreshes_census_before_qobuz_artist_scan
```

Expected: FAIL because `scan_library()` does not call
`_refresh_library_census()`.

- [ ] **Step 7: Integrate the refresh into `scan_library()`**

Call the helper after the artist-list empty check and before downsample,
upgrade, fingerprint, or Qobuz work:

```python
census_result = _refresh_library_census(job)
if job.cancel_requested:
    job.summary = "Stopped during the read-only library inventory."
    return
```

Do not make an inventory error abort Qobuz discovery; the previous census
snapshot remains valid. Cancellation follows the existing job cancellation
semantics.

- [ ] **Step 8: Run scan tests and lint**

Run:

```bash
pytest -q tests/test_scan_unification.py
ruff check src/qobuz_librarian/web/flows.py tests/test_scan_unification.py
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/qobuz_librarian/web/flows.py tests/test_scan_unification.py
git commit -m "fix: inventory every track during library scans"
```

---

### Task 3: Make the web census prefer the exact snapshot

**Files:**
- Modify: `src/qobuz_librarian/web/app.py:5779-5828`
- Modify: `src/qobuz_librarian/web/templates/_census.html:25`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `library.census.load() -> dict | None` from Task 1 and legacy `flac_cache.census() -> dict | None`.
- Produces: `_census_view()` with the unchanged template-view shape; exact snapshot data takes precedence.

- [ ] **Step 1: Add failing source-selection tests**

Add to `tests/test_web.py`:

```python
def _raw_census(total):
    return {
        "version": 1,
        "tiers": {
            "cd": [total, total * 10],
            "hires96": [0, 0],
            "hires192": [0, 0],
            "unknown": [0, 0],
        },
        "total_tracks": total,
        "total_bytes": total * 10,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }


def test_census_view_prefers_exact_snapshot(monkeypatch):
    from qobuz_librarian.library import census as library_census
    from qobuz_librarian.library import flac_cache
    from qobuz_librarian.web import app as webapp

    webapp._census_cache = None
    monkeypatch.setattr(library_census, "load", lambda: _raw_census(44477))
    monkeypatch.setattr(
        flac_cache,
        "census",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy cache fallback must not run")),
    )

    view = webapp._census_view()

    assert view["total"].startswith("44,477 tracks")


def test_census_view_falls_back_to_tag_cache_before_first_snapshot(monkeypatch):
    from qobuz_librarian.library import census as library_census
    from qobuz_librarian.library import flac_cache
    from qobuz_librarian.web import app as webapp

    webapp._census_cache = None
    monkeypatch.setattr(library_census, "load", lambda: None)
    monkeypatch.setattr(flac_cache, "census", lambda: _raw_census(28116))

    view = webapp._census_view()

    assert view["total"].startswith("28,116 tracks")
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
pytest -q \
  tests/test_web.py::test_census_view_prefers_exact_snapshot \
  tests/test_web.py::test_census_view_falls_back_to_tag_cache_before_first_snapshot
```

Expected: the preference test FAILS because `_census_view()` reads only
`flac_cache.census()`.

- [ ] **Step 3: Prefer the exact snapshot without a filesystem walk**

Change `_census_view()` to load the small JSON snapshot before the legacy
cache. Preserve memoization only for the expensive legacy SQLite fallback; a
snapshot load is a small file read and lets a newly completed scan appear on
the next calm Library-page request:

```python
from qobuz_librarian.library import census as library_census
from qobuz_librarian.library import flac_cache

raw = library_census.load()
using_legacy = raw is None
if using_legacy:
    now = time.time()
    if _census_cache is not None and now - _census_cache[0] < _CENSUS_TTL:
        return _census_cache[1]
    raw = flac_cache.census()
```

Only assign `_census_cache = (now, view)` in the legacy branch. When a valid
snapshot exists, clear any legacy memoized value after shaping the snapshot.
Keep the existing row labels, size formatting, bar math, artist list, and
reclaim threshold, except rename the Unknown row from `"Other formats"` to
`"Other / unknown"` so tagless and unparseable audio is described truthfully.

- [ ] **Step 4: Update the census provenance copy**

Change the template footer to:

```html
<p class="ql-census-foot">Counts are from your last completed library inventory.</p>
```

- [ ] **Step 5: Run focused web tests to verify GREEN**

Run:

```bash
pytest -q \
  tests/test_web.py::test_census_view_prefers_exact_snapshot \
  tests/test_web.py::test_census_view_falls_back_to_tag_cache_before_first_snapshot
```

Expected: 2 passed.

- [ ] **Step 6: Run broader web and template checks**

Run:

```bash
pytest -q tests/test_web.py
ruff check src/qobuz_librarian/web/app.py tests/test_web.py
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/qobuz_librarian/web/app.py src/qobuz_librarian/web/templates/_census.html tests/test_web.py
git commit -m "fix: show exact completed library census"
```

---

### Task 4: Verify the complete behavior and safety contract

**Files:**
- Verify only; modify production or test files only if a verification failure exposes a defect covered by the approved spec.

**Interfaces:**
- Consumes: all interfaces from Tasks 1-3.
- Produces: a clean full-suite verification record and a reviewable final diff.

- [ ] **Step 1: Run the focused regression suite**

```bash
pytest -q \
  tests/test_library_census.py \
  tests/test_scanner_cache.py \
  tests/test_scan_unification.py \
  tests/test_web.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the full test suite**

```bash
pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Run repository lint**

```bash
ruff check .
```

Expected: Ruff exits 0 with no errors.

- [ ] **Step 4: Inspect the final diff for prohibited media mutations**

```bash
git diff --check
git diff --stat HEAD~3..HEAD
rg -n "write_bytes|write_text|os\\.replace|unlink|rename|touch|utime" \
  src/qobuz_librarian/library/census.py \
  src/qobuz_librarian/web/flows.py
```

Expected: `git diff --check` is clean. Any write primitive in
`library/census.py` targets only `cfg.LIBRARY_CENSUS_FILE` or its temporary
file under `DATA_DIR`; there are no write operations against discovered music
paths.

- [ ] **Step 5: Review the exact-count acceptance checklist**

Confirm from tests and diff:

- A supported file absent from `flac_cache.db` is counted.
- A negative/tagless/unparseable supported file is counted as Unknown.
- `._*`, unsupported files, and symlinked-directory contents are excluded.
- Cancellation and I/O errors preserve the prior snapshot.
- Removed files vanish from the next completed snapshot because every snapshot
  is rebuilt from the observed walk rather than accumulated cache rows.
- Page rendering loads JSON or the legacy SQLite fallback and never walks
  `MUSIC_ROOT`.
- No code opens a discovered media path for writing.
