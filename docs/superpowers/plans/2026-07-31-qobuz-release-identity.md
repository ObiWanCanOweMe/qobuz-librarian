# Stable Qobuz Release Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every managed album a portable Qobuz release identity, keep ordinary friendly paths unchanged, route only real edition collisions to deterministic Qobuz-ID-suffixed directories, and visibly distinguish confusing editions in both Library review tabs.

**Architecture:** A focused release-identity module owns the reserved JSON manifest, a placement module resolves friendly versus suffixed destinations, and a pure edition-badge module labels only multi-release album families. Discovery uses a valid manifest first and adopts an unmarked legacy folder only when exactly one compatible Qobuz release remains; Beets receives a one-run path override for known collisions so editions never share a destination. Generic scanners exclude the manifest, migration and backup transfer it through explicit validated metadata handling, and the shared Library review row renders the saved badge payload on both tabs.

**Tech Stack:** Python 3.12+, `pathlib`, descriptor-relative POSIX filesystem APIs, JSON, existing Beets 2.12 integration, SQLite-backed Beets relocation, pytest, Ruff.

## Global Constraints

- Stable album identity is `(provider, release_id)` with provider `qobuz` and a non-empty normalized string release ID.
- The reserved manifest name is exactly `.qobuz-librarian-release.json`.
- Manifest schema version 1 has exactly the keys and values shown by `{"schema_version":1,"provider":"qobuz","release_id":"123456789"}`; missing and malformed manifests are different states.
- Ordinary albums retain the existing friendly path.
- Only a distinct release colliding with an occupied friendly path receives a suffix of the form `[qobuz-123456789]`, containing its complete release ID, on the album directory component.
- The manifest, not the path suffix, is authoritative.
- Different release IDs never merge automatically.
- Legacy folders are adopted only when one compatible Qobuz release remains; ambiguity never writes a manifest.
- Generic scanning, census, repair, Beets input collection, companion discovery, and consolidation never classify the reserved manifest as media or a user sidecar.
- Identity-aware moves, migration, backup, export, and restore preserve the validated manifest explicitly.
- Manifest reads and writes never follow symlinks; writes are same-directory, durable, atomic, and no-replace.
- A failed or cancelled import never publishes a manifest for an unverified album directory.
- Missing Albums and Gap Fill show an edition badge only when a trustworthy catalogue contains at least two distinct Qobuz release IDs in the same normalized artist, edition-stripped title, and original-release-year family.
- Every displayed edition badge contains a human label and the complete visible Qobuz release ID.
- Duplicate human labels use edition publication year, track count, then quality as ordered fallback differentiators.
- Edition badges are informational and never change selection, grouping, download scope, or the submitted release ID.
- No new runtime dependency is added.

---

## File Structure

- Create `src/qobuz_librarian/library/release_identity.py`: identity value object, strict manifest codec, safe no-follow reads, atomic no-replace publication, and reserved-artifact predicates.
- Create `src/qobuz_librarian/library/album_placement.py`: deterministic collision suffixing and filesystem placement decisions.
- Create `src/qobuz_librarian/library/edition_badges.py`: pure edition-family grouping, human-label extraction, and visible Qobuz-ID badge generation.
- Create `tests/test_release_identity.py`: manifest and reserved-artifact unit tests.
- Create `tests/test_album_placement.py`: ordinary, repeated, legacy, collision, and path-length placement tests.
- Create `tests/test_edition_badges.py`: multi-release grouping, label fallback, and single-release suppression tests.
- Modify `src/qobuz_librarian/library/scanner.py`, `census.py`, `migrate.py`, `repair_log.py`, `integrations/beets.py`, and `modes/consolidate.py`: enforce generic manifest exclusion at each non-identity enumeration boundary.
- Modify `src/qobuz_librarian/library/catalog.py` and `discovery.py`: retain distinct release IDs, prefer manifest identity, and require unique evidence for legacy adoption.
- Modify `src/qobuz_librarian/integrations/beets.py`: obtain effective path templates, append a literal collision suffix to the album component for one import, and refuse unsupported templates safely.
- Modify `src/qobuz_librarian/modes/process.py`, `queue/durable_album.py`, `queue/post_import_finalizer.py`, and `library/post_import_relocation.py`: carry the intended identity and placement through durable import completion and publish the manifest only after exact completion proof.
- Modify `src/qobuz_librarian/library/migrate.py` and `tests/test_migrate.py`: carry release identity separately from audio and companion receipts.
- Modify `src/qobuz_librarian/library/backup.py`, `modes/process.py`, and backup tests: retain or restore release identity through whole-album replacement without treating it as a companion.
- Modify `src/qobuz_librarian/web/flows.py`, `_review_group_items.html`, and discovery/web tests: attach edition badges to Missing Albums and Gap Fill candidates and expose stable, non-actionable identity-review cards.
- Modify `docs/existing-libraries.md` and `docs/troubleshooting.md`: document manifests, collision paths, and review recovery.

### Task 1: Implement the Strict Release Manifest

**Files:**

- Create: `src/qobuz_librarian/library/release_identity.py`
- Create: `tests/test_release_identity.py`

**Interfaces:**

- Produces: `ReleaseIdentity(provider: str, release_id: str)`.
- Produces: `ReleaseManifestError(OSError)`.
- Produces: `MANIFEST_NAME = ".qobuz-librarian-release.json"` and `MAX_MANIFEST_BYTES = 4096`.
- Produces: `normalise_release_id(value) -> str | None`.
- Produces: `identity_from_album(album: dict) -> ReleaseIdentity | None`.
- Produces: `is_release_manifest_name(name: str) -> bool`.
- Produces: `is_ignored_library_artifact(name: str) -> bool`, true only for `._*` and the exact reserved manifest.
- Produces: `read_release_identity(album_dir: Path) -> ReleaseIdentity | None`; missing returns `None`, invalid raises `ReleaseManifestError`.
- Produces: `publish_release_identity(album_dir: Path, identity: ReleaseIdentity, *, expected_directory: tuple[int, int] | None = None) -> bool`; `True` means created, `False` means the same identity already existed.

- [ ] **Step 1: Write failing codec and filesystem tests**

Create tests that exercise the public API, including exact schema, numeric ID normalization, same-identity idempotence, conflicting identity refusal, malformed/oversized JSON, manifest symlinks, and a swapped album directory:

```python
import json
import os

import pytest

from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseIdentity,
    ReleaseManifestError,
    identity_from_album,
    is_ignored_library_artifact,
    publish_release_identity,
    read_release_identity,
)


def test_manifest_round_trip_and_no_replace(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    identity = ReleaseIdentity("qobuz", "123")

    assert publish_release_identity(album, identity) is True
    assert read_release_identity(album) == identity
    assert publish_release_identity(album, identity) is False

    with pytest.raises(ReleaseManifestError, match="different release"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "456"))

    assert json.loads((album / MANIFEST_NAME).read_text()) == {
        "schema_version": 1,
        "provider": "qobuz",
        "release_id": "123",
    }


def test_manifest_rejects_links_unknown_fields_and_oversize(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version":1,"provider":"qobuz","release_id":"1"}')
    (album / MANIFEST_NAME).symlink_to(outside)
    with pytest.raises(ReleaseManifestError, match="regular file"):
        read_release_identity(album)

    (album / MANIFEST_NAME).unlink()
    (album / MANIFEST_NAME).write_text(
        '{"schema_version":1,"provider":"qobuz","release_id":"1","title":"x"}'
    )
    with pytest.raises(ReleaseManifestError, match="schema"):
        read_release_identity(album)

    (album / MANIFEST_NAME).write_bytes(b"x" * 4097)
    with pytest.raises(ReleaseManifestError, match="large"):
        read_release_identity(album)


def test_publish_rejects_a_replaced_album_directory(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    value = album.stat()
    expected = (value.st_dev, value.st_ino)
    album.rename(tmp_path / "moved")
    album.mkdir()

    with pytest.raises(ReleaseManifestError, match="directory changed"):
        publish_release_identity(
            album, ReleaseIdentity("qobuz", "123"),
            expected_directory=expected,
        )


def test_release_id_and_reserved_artifact_rules():
    assert identity_from_album({"id": 123}) == ReleaseIdentity("qobuz", "123")
    assert identity_from_album({"id": " 123 "}) == ReleaseIdentity("qobuz", "123")
    assert identity_from_album({"id": ""}) is None
    assert is_ignored_library_artifact("._track.flac") is True
    assert is_ignored_library_artifact(MANIFEST_NAME) is True
    assert is_ignored_library_artifact("album.json") is False
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/test_release_identity.py
```

Expected: FAIL because `release_identity` does not exist.

- [ ] **Step 3: Implement the strict codec and descriptor-safe publication**

Implement the value object and validation with these exact public definitions:

```python
MANIFEST_NAME = ".qobuz-librarian-release.json"
MAX_MANIFEST_BYTES = 4096
_FIELDS = frozenset({"schema_version", "provider", "release_id"})


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    provider: str
    release_id: str


class ReleaseManifestError(OSError):
    pass


def normalise_release_id(value):
    if isinstance(value, bool) or value is None:
        return None
    value = str(value).strip()
    return value if value and "\x00" not in value and "/" not in value else None


def identity_from_album(album):
    release_id = normalise_release_id((album or {}).get("id"))
    return ReleaseIdentity("qobuz", release_id) if release_id else None


def is_release_manifest_name(name):
    return isinstance(name, str) and name == MANIFEST_NAME


def is_ignored_library_artifact(name):
    return isinstance(name, str) and (name.startswith("._") or name == MANIFEST_NAME)
```

Use `O_DIRECTORY | O_NOFOLLOW` for the album, `O_NOFOLLOW` for the manifest,
`fstat()` to require a regular file and cap bytes before `json.loads()`, exact
field-set validation, and UTF-8 decoding. Publish canonical compact JSON plus a
newline through an `O_CREAT | O_EXCL` random temporary file, `fsync()` it,
hard-link it to `MANIFEST_NAME` with no replacement, unlink the temporary name,
and `fsync()` the album directory. If the final name already exists, validate
it and return `False` only for the same identity.

- [ ] **Step 4: Run focused tests and lint**

Run:

```bash
pytest -q tests/test_release_identity.py
ruff check src/qobuz_librarian/library/release_identity.py tests/test_release_identity.py
```

Expected: all tests PASS and Ruff reports no errors.

- [ ] **Step 5: Commit the manifest foundation**

```bash
git add src/qobuz_librarian/library/release_identity.py tests/test_release_identity.py
git commit -m "feat: add durable qobuz release manifests"
```

### Task 2: Exclude the Manifest From Every Generic Indexer

**Files:**

- Modify: `src/qobuz_librarian/library/scanner.py`
- Modify: `src/qobuz_librarian/library/census.py`
- Modify: `src/qobuz_librarian/library/migrate.py`
- Modify: `src/qobuz_librarian/repair_log.py`
- Modify: `src/qobuz_librarian/integrations/beets.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_library_census.py`
- Modify: `tests/test_migrate.py`
- Modify: `tests/test_ui_and_repair.py`
- Modify: `tests/test_integrations.py`

**Interfaces:**

- Consumes: `is_release_manifest_name()` where only the Qobuz manifest must be skipped.
- Consumes: `is_ignored_library_artifact()` where the boundary already excludes AppleDouble files.
- Produces: no generic track, companion, repair, census, or Beets-source record for the manifest.

- [ ] **Step 1: Add one manifest control to each existing enumeration regression test**

Extend the scanner, census, migration, repair, and Beets source-collection tests with a literal manifest file. Assertions must prove it is absent while `cover.json` remains eligible wherever arbitrary companions are supported:

```python
(album / ".qobuz-librarian-release.json").write_text(
    '{"schema_version":1,"provider":"qobuz","release_id":"123"}'
)
```

For migration's descriptor test, assert:

```python
assert ".qobuz-librarian-release.json" not in [
    receipt["relative"][-1] for receipt in companions
]
```

For Beets preparation, collect `source_files_out` and assert:

```python
assert all(receipt.path.name != ".qobuz-librarian-release.json"
           for receipt in prepared_sources)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest -q tests/test_scanner.py tests/test_library_census.py \
  tests/test_migrate.py tests/test_ui_and_repair.py tests/test_integrations.py
```

Expected: at least migration companion collection and Beets source capture include the manifest.

- [ ] **Step 3: Apply the exclusion at enumeration boundaries**

Import the predicate and filter before suffix classification or receipt creation. The migration branch becomes:

```python
elif stat.S_ISREG(value.st_mode):
    if is_ignored_library_artifact(name):
        continue
    suffix = Path(name).suffix.lower()
```

The Beets capture loop becomes:

```python
for path in candidates:
    if is_release_manifest_name(path.name):
        continue
    receipt = capture_file(path)
```

Make `iter_tree_no_symlinks()` omit only the exact manifest basename; retain
the operation-local AppleDouble behavior established by the existing tests.
Keep the census and repair explicit guards as defense in depth.

- [ ] **Step 4: Run focused tests and lint**

```bash
pytest -q tests/test_scanner.py tests/test_library_census.py \
  tests/test_migrate.py tests/test_ui_and_repair.py tests/test_integrations.py
ruff check src/qobuz_librarian tests
```

Expected: all tests PASS and existing `._*` controls remain excluded.

- [ ] **Step 5: Commit the reserved-artifact boundary**

```bash
git add src/qobuz_librarian/library/scanner.py \
  src/qobuz_librarian/library/census.py \
  src/qobuz_librarian/library/migrate.py \
  src/qobuz_librarian/repair_log.py \
  src/qobuz_librarian/integrations/beets.py \
  tests/test_scanner.py tests/test_library_census.py tests/test_migrate.py \
  tests/test_ui_and_repair.py tests/test_integrations.py
git commit -m "fix: reserve release manifests from generic indexing"
```

### Task 3: Preserve Distinct Release IDs in Catalog Discovery

**Files:**

- Modify: `src/qobuz_librarian/library/catalog.py`
- Modify: `tests/test_catalog.py`

**Interfaces:**

- Consumes: `normalise_release_id()` and `read_release_identity()`.
- Produces: `dedup_album_versions()` collapses duplicate API rows only when their normalized release IDs match; ID-less fallback rows retain the old title/year grouping.
- Produces: `find_qobuz_album_candidates_for_dir(album_dir, artist_name, token, *, prefer_hires=False, catalog=None, target_dir=None) -> list[dict]` in ranked order with full track lists and unique release IDs.
- Keeps: `find_qobuz_album_for_dir(album_dir: Path, artist_name: str, token, prefer_hires: bool = False, catalog: list[dict] | None = None, target_dir: Path | None = None) -> dict | None` as the first-candidate compatibility wrapper.

- [ ] **Step 1: Replace the old edition-collapse expectation with identity tests**

Add IDs to the helper data and assert distinct editions survive while duplicate rows for the same ID collapse:

```python
def test_dedup_album_versions_preserves_distinct_qobuz_releases():
    standard = _qalbum("Album", 2020, album_id="100")
    deluxe = _qalbum("Album (Deluxe Edition)", 2020, album_id="200")
    duplicate = dict(standard, maximum_bit_depth=24)

    result = dedup_album_versions([standard, deluxe, duplicate], prefer_hires=True)

    assert [str(album["id"]) for album, _count in result] == ["100", "200"]
    assert result[0][1] == 2
    assert result[0][0]["maximum_bit_depth"] == 24
```

Extend `_qalbum()` with `album_id=None` and include `"id": album_id` in its
returned dictionary so the test exercises the production ID path.

Add a candidate test where standard and deluxe both resolve to one legacy
folder and assert both fully materialized IDs are returned in stable rank order.

```python
def test_album_candidates_keep_standard_and_deluxe_release_ids(tmp_path, monkeypatch):
    folder = tmp_path / "Artist" / "Album (2020)"
    folder.mkdir(parents=True)
    standard = _qalbum("Album", 2020, album_id="100")
    deluxe = _qalbum("Album (Deluxe Edition)", 2020, album_id="200")
    full = {
        "100": {**standard, "tracks": {"items": [_qt("one", isrc="A")]}},
        "200": {**deluxe, "tracks": {"items": [
            _qt("one", isrc="A"), _qt("bonus", isrc="B")]}},
    }
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(catalog, "get_album", lambda album_id, _token: full[str(album_id)])
    monkeypatch.setattr(
        catalog, "find_album_dir_filesystem", lambda _album: folder)

    candidates = catalog.find_qobuz_album_candidates_for_dir(
        folder, "Artist", "tok", catalog=[standard, deluxe], target_dir=folder)

    assert [str(album["id"]) for album in candidates] == ["100", "200"]
    assert all((album.get("tracks") or {}).get("items") for album in candidates)
```

- [ ] **Step 2: Run catalog tests and verify RED**

```bash
pytest -q tests/test_catalog.py
```

Expected: the existing title/year grouping returns one edition instead of two.

- [ ] **Step 3: Key catalogue deduplication by release identity**

Use this grouping rule:

```python
release_id = normalise_release_id(a.get("id"))
if release_id is not None:
    key = ("qobuz", release_id)
else:
    key = ("legacy", key_title, album_year_int(a))
groups.setdefault(key, []).append(a)
```

Extract the current catalog and search ranking into
`find_qobuz_album_candidates_for_dir()`. Deduplicate search results by
normalized ID, apply the existing lossless/artist/title/year/path gates, call
`get_album()` for each passing candidate, and omit candidates that cannot be
materialized. Make `find_qobuz_album_for_dir()` return the first result so
non-discovery callers retain their current contract.

- [ ] **Step 4: Run catalog and discovery compatibility tests**

```bash
pytest -q tests/test_catalog.py tests/test_discovery.py
ruff check src/qobuz_librarian/library/catalog.py tests/test_catalog.py
```

Expected: all tests PASS with deterministic ID order.

- [ ] **Step 5: Commit release-aware catalog matching**

```bash
git add src/qobuz_librarian/library/catalog.py tests/test_catalog.py
git commit -m "feat: retain qobuz editions by release id"
```

### Task 4: Adopt Only Unambiguous Legacy Folders

**Files:**

- Modify: `src/qobuz_librarian/library/discovery.py`
- Modify: `src/qobuz_librarian/library/catalog.py`
- Modify: `tests/test_discovery.py`

**Interfaces:**

- Produces: `LegacyReleaseEvidence(album: dict, present: list, missing: list, extras: list)`.
- Produces: `select_legacy_release(existing: list[dict], candidates: list[dict]) -> tuple[LegacyReleaseEvidence | None, list[LegacyReleaseEvidence]]`.
- Extends: `DirMatch` with `candidate_releases: list[dict]`.
- Adds statuses: `identity_ambiguous` and `identity_invalid`.

- [ ] **Step 1: Write discovery tests for authoritative, unique, and ambiguous identity**

Add these three tests using the existing real temp-library helper and fake API:

```python
def test_manifest_selects_exact_release_even_when_title_rank_prefers_another(
        monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020, [_qt("one", "A")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("bonus", "B")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A"), _et("bonus", "B")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    publish_release_identity(folder, ReleaseIdentity("qobuz", "200"))
    FakeQobuz(artists=[], catalog=[standard, deluxe]).install(monkeypatch)

    match = discovery.match_album_dir(
        folder, "Artist", "tok",
        catalog=[_catalog_entry(standard), _catalog_entry(deluxe)],
        prefer_hires=False,
    )

    assert match.qobuz_album["id"] == "200"
    assert match.status == "complete"


def test_unique_partial_legacy_match_publishes_manifest(monkeypatch, tmp_path):
    wanted = _album("100", "Album", "Artist", 2020,
                    [_qt("one", "A"), _qt("two", "B")])
    wrong = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                   [_qt("other", "Z")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    FakeQobuz(artists=[], catalog=[wanted, wrong]).install(monkeypatch)

    match = discovery.match_album_dir(
        folder, "Artist", "tok",
        catalog=[_catalog_entry(wanted), _catalog_entry(wrong)],
        prefer_hires=False,
    )

    assert match.status == "partial"
    assert read_release_identity(folder) == ReleaseIdentity("qobuz", "100")


def test_shared_standard_tracks_leave_legacy_folder_ambiguous(monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020,
                      [_qt("one", "A"), _qt("two", "B")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("one", "A"), _qt("two", "B"), _qt("bonus", "C")])
    _library(monkeypatch, tmp_path,
             {"Artist": {"Album (2020)": [_et("one", "A")]}})
    folder = tmp_path / "Artist" / "Album (2020)"
    FakeQobuz(artists=[], catalog=[standard, deluxe]).install(monkeypatch)

    match = discovery.match_album_dir(
        folder, "Artist", "tok",
        catalog=[_catalog_entry(standard), _catalog_entry(deluxe)],
        prefer_hires=False,
    )

    assert match.status == "identity_ambiguous"
    assert [str(album["id"]) for album in match.candidate_releases] == ["100", "200"]
    assert not (folder / MANIFEST_NAME).exists()
```

Import `MANIFEST_NAME`, `ReleaseIdentity`, `publish_release_identity`, and
`read_release_identity` from `library.release_identity` at the top of the test.

- [ ] **Step 2: Run discovery tests and verify RED**

```bash
pytest -q tests/test_discovery.py
```

Expected: manifest identity is ignored and the current first-ranked candidate is selected.

- [ ] **Step 3: Implement exact and legacy selection paths**

For a valid manifest, fetch only its release ID and run existing track
comparison. For an unmarked folder, build evidence as follows:

```python
def select_legacy_release(existing, candidates):
    compatible = []
    for album in candidates:
        tracks = (album.get("tracks") or {}).get("items") or []
        if not tracks:
            continue
        missing, present = compute_missing(tracks, existing)
        extras = find_extras_in_existing(tracks, existing)
        if present and not extras:
            compatible.append(LegacyReleaseEvidence(
                album, list(present), list(missing), list(extras)))
    return (compatible[0] if len(compatible) == 1 else None, compatible)
```

Before publishing an adopted manifest, capture the album directory `(st_dev,
st_ino)` and a stable signature of every reviewed audio file, then pass the
directory identity to `publish_release_identity()` and re-read the album. A
changed inventory becomes `identity_invalid`; two or more compatible release
IDs become `identity_ambiguous`. `classify_owned_match()` adds both to
`result.skipped` with candidate IDs and titles.

- [ ] **Step 4: Run discovery, catalog, and scan tests**

```bash
pytest -q tests/test_discovery.py tests/test_catalog.py tests/test_scan_unification.py
ruff check src/qobuz_librarian/library/discovery.py tests/test_discovery.py
```

Expected: all tests PASS; ambiguous folders remain byte-for-byte unchanged.

- [ ] **Step 5: Commit conservative legacy adoption**

```bash
git add src/qobuz_librarian/library/discovery.py \
  src/qobuz_librarian/library/catalog.py tests/test_discovery.py
git commit -m "feat: adopt only unambiguous qobuz releases"
```

### Task 5: Resolve Friendly and Collision Album Paths

**Files:**

- Create: `src/qobuz_librarian/library/album_placement.py`
- Create: `tests/test_album_placement.py`
- Modify: `src/qobuz_librarian/library/catalog.py`

**Interfaces:**

- Produces: `PlacementDisposition` values `NEW`, `SAME_RELEASE`, `ADOPTED`, and `COLLISION`.
- Produces: `AlbumPlacement(identity, friendly_path, destination, disposition, suffix)`.
- Produces: `AlbumPlacementAttention(OSError)`.
- Produces: `qobuz_collision_suffix(identity: ReleaseIdentity) -> str`.
- Produces: `collision_album_path(friendly_path: Path, identity: ReleaseIdentity) -> Path`.
- Produces: `resolve_album_placement(friendly_path: Path, identity: ReleaseIdentity, *, adopted_identity: ReleaseIdentity | None = None) -> AlbumPlacement`.

- [ ] **Step 1: Write path-resolution tests**

```python
def test_new_and_same_release_keep_friendly_path(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    identity = ReleaseIdentity("qobuz", "100")
    assert resolve_album_placement(friendly, identity).destination == friendly
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, identity)
    assert resolve_album_placement(friendly, identity).destination == friendly


def test_different_release_gets_complete_id_suffix(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))

    placement = resolve_album_placement(
        friendly, ReleaseIdentity("qobuz", "987654321"))

    assert placement.destination.name == "Album (2020) [qobuz-987654321]"
    assert placement.disposition is PlacementDisposition.COLLISION


def test_unmarked_occupied_friendly_path_requires_attention(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    with pytest.raises(AlbumPlacementAttention, match="unmarked"):
        resolve_album_placement(friendly, ReleaseIdentity("qobuz", "200"))


def test_conflicting_suffixed_path_requires_attention(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    collision = friendly.with_name("Album (2020) [qobuz-200]")
    collision.mkdir()
    publish_release_identity(collision, ReleaseIdentity("qobuz", "300"))

    with pytest.raises(AlbumPlacementAttention, match="occupied"):
        resolve_album_placement(friendly, ReleaseIdentity("qobuz", "200"))
```

Add a `NAME_MAX` test that monkeypatches the component limit, proves only the
friendly stem is truncated, and keeps the complete suffix.

```python
def test_collision_truncates_stem_but_preserves_complete_suffix(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / ("A" * 80)
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    monkeypatch.setattr(album_placement, "_component_name_max", lambda _path: 48)

    result = resolve_album_placement(
        friendly, ReleaseIdentity("qobuz", "987654321"))

    assert result.destination.name.endswith(" [qobuz-987654321]")
    assert len(os.fsencode(result.destination.name)) <= 48
    assert result.destination.name.startswith("A")
```

Import `os` and the module itself as `album_placement` for the private limit
boundary used by this test.

- [ ] **Step 2: Run placement tests and verify RED**

```bash
pytest -q tests/test_album_placement.py
```

Expected: FAIL because `album_placement` does not exist.

- [ ] **Step 3: Implement deterministic placement**

Use exact suffix construction:

```python
def qobuz_collision_suffix(identity):
    if identity.provider != "qobuz":
        raise ValueError("collision suffix requires a Qobuz identity")
    return f" [qobuz-{identity.release_id}]"
```

Read the friendly manifest first. Missing + nonexistent is `NEW`; same is
`SAME_RELEASE`; missing + `adopted_identity == identity` is `ADOPTED`; missing
without an adoption proof raises attention; different identity resolves the
suffixed sibling. Reuse an existing suffix only when its manifest matches.
Truncate by encoded filesystem bytes with the existing Beets-compatible helper
while reserving all suffix bytes.

- [ ] **Step 4: Run placement and catalog tests**

```bash
pytest -q tests/test_album_placement.py tests/test_catalog.py
ruff check src/qobuz_librarian/library/album_placement.py tests/test_album_placement.py
```

Expected: all tests PASS.

- [ ] **Step 5: Commit collision-aware placement**

```bash
git add src/qobuz_librarian/library/album_placement.py \
  src/qobuz_librarian/library/catalog.py tests/test_album_placement.py
git commit -m "feat: resolve edition collision paths"
```

### Task 6: Route Collision Imports Before Beets Moves Audio

**Files:**

- Modify: `src/qobuz_librarian/integrations/beets.py`
- Modify: `src/qobuz_librarian/modes/process.py`
- Modify: `src/qobuz_librarian/queue/executor.py`
- Modify: `tests/test_integrations.py`
- Modify: `tests/test_process.py`
- Modify: `tests/test_queue.py`

**Interfaces:**

- Produces: `append_album_path_suffix(template: str, suffix: str) -> str`; raises `ValueError` unless exactly one path component contains `$album`.
- Produces: `_render_beets_override(plugin_config: dict, *, album_path_suffix: str = "") -> str` as the pure YAML-generation boundary used by tests.
- Extends: `_configured_beets_plugins()` result with effective `paths.default`, `paths.comp`, and `paths.singleton` strings.
- Extends: `_prepare_for_beets_run(roots=None, ownership_out=None, source_files_out=None, album_path_suffix: str = "")`.
- Extends: `beets_import_paths(consolidate: bool = True, *, source_files_out=None, album_dirs=None, album_path_suffix: str = "")`.
- Extends: `beets_import_albums(album_dirs, *, ownership_out=None, album_path_suffix: str = "")`.
- Consumes: `AlbumPlacement.suffix`, empty for non-collisions.

- [ ] **Step 1: Add path-template and command-generation tests**

```python
def test_append_album_path_suffix_changes_only_album_component():
    assert append_album_path_suffix(
        "$albumartist/$album ($year)/$track - $title",
        " [qobuz-200]",
    ) == "$albumartist/$album ($year) [qobuz-200]/$track - $title"


@pytest.mark.parametrize("template", [
    "$artist/$track - $title",
    "$album/$album/$track - $title",
])
def test_append_album_path_suffix_refuses_ambiguous_templates(template):
    with pytest.raises(ValueError, match="album path component"):
        append_album_path_suffix(template, " [qobuz-200]")
```

Add an integration test that captures the one-shot YAML and asserts all
effective album templates include `[qobuz-200]`, while an empty suffix leaves
the existing override byte-for-byte unchanged.

```python
def test_collision_suffix_is_written_to_each_effective_album_path():
    configured = {
        "plugins": [],
        "disabled": [],
        "musicbrainz_enabled": False,
        "plugin_paths": [],
        "paths": {
            "default": "$albumartist/$album ($year)/$track - $title",
            "comp": "Compilations/$album ($year)/$track - $title",
            "singleton": "Singletons/$artist - $title",
        },
    }

    plain = _render_beets_override(configured)
    collision = _render_beets_override(
        configured, album_path_suffix=" [qobuz-200]")

    assert "$album ($year) [qobuz-200]/$track" in collision
    assert "Compilations/$album ($year) [qobuz-200]/$track" in collision
    assert "Singletons/$artist - $title" in collision
    assert "[qobuz-200]" not in plain
```

- [ ] **Step 2: Run integration/process/queue tests and verify RED**

```bash
pytest -q tests/test_integrations.py tests/test_process.py tests/test_queue.py
```

Expected: the Beets functions reject the new keyword or generate unsuffixed paths.

- [ ] **Step 3: Generate a collision-only path override**

Extend the supported Beets-runtime probe to serialize the three effective path
templates. When `album_path_suffix` is non-empty, transform every configured
template that can file albums and emit the transformed `paths:` values in the
one-shot YAML. Do not mutate the user's config file. Return `None` from setup
and log an identity-review reason if a template cannot be transformed safely.

Before import, `process_album()` obtains the intended identity, examines the
resolved existing friendly directory, calls `resolve_album_placement()`, and
passes only `placement.suffix` to Beets. Queue execution does the same per
album; do not batch two albums with different non-empty suffixes into one Beets
invocation. If the friendly directory is occupied but unmarked, call the Task
4 legacy selector first and pass its identity as `adopted_identity`; an
ambiguous result stops before Beets and emits `identity_ambiguous`.

Use this call shape in both paths:

```python
imported = beets_import_paths(
    consolidate=album_dir is not None,
    album_dirs=staged_dirs_for_import,
    album_path_suffix=placement.suffix,
)
```

- [ ] **Step 4: Prove collision and ordinary imports choose separate paths**

```bash
pytest -q tests/test_integrations.py tests/test_process.py tests/test_queue.py \
  tests/test_durable_album.py
ruff check src/qobuz_librarian/integrations/beets.py \
  src/qobuz_librarian/modes/process.py src/qobuz_librarian/queue/executor.py
```

Expected: collision imports use the suffixed template before any move; ordinary import command and YAML snapshots remain unchanged.

- [ ] **Step 5: Commit pre-import collision routing**

```bash
git add src/qobuz_librarian/integrations/beets.py \
  src/qobuz_librarian/modes/process.py src/qobuz_librarian/queue/executor.py \
  tests/test_integrations.py tests/test_process.py tests/test_queue.py
git commit -m "feat: route qobuz edition collisions during import"
```

### Task 7: Publish Identity Only After Durable Import Completion

**Files:**

- Modify: `src/qobuz_librarian/modes/process.py`
- Modify: `src/qobuz_librarian/queue/durable_album.py`
- Modify: `src/qobuz_librarian/queue/post_import_finalizer.py`
- Modify: `src/qobuz_librarian/completion.py`
- Modify: `tests/test_process.py`
- Modify: `tests/test_durable_post_import_action.py`
- Modify: `tests/test_completion.py`

**Interfaces:**

- Extends durable planned album state with canonical `release_identity` and `placement_destination` records.
- Produces: `finalize_release_identity(album: dict, final_dir: Path, *, expected_destination: Path) -> bool` in `modes/process.py` for the synchronous path.
- Queue finalization publishes through `publish_release_identity()` after relocation handoff and completion proof, before retirement acknowledgement.

- [ ] **Step 1: Add completion-order and rollback tests**

Add tests that checkpoint each boundary and assert:

```python
assert not (final_dir / MANIFEST_NAME).exists()  # download complete
assert not (final_dir / MANIFEST_NAME).exists()  # Beets failed
assert not (final_dir / MANIFEST_NAME).exists()  # relocation unacknowledged
assert read_release_identity(final_dir) == ReleaseIdentity("qobuz", "200")
```

Add a conflict test where finalization finds release `100` at a destination
planned for `200`; assert the queue item remains recoverable and neither the
manifest nor Beets rows are changed.

```python
def test_finalize_release_identity_rejects_wrong_destination_identity(tmp_path):
    final_dir = tmp_path / "Artist" / "Album (2020) [qobuz-200]"
    final_dir.mkdir(parents=True)
    publish_release_identity(final_dir, ReleaseIdentity("qobuz", "100"))
    before = (final_dir / MANIFEST_NAME).read_bytes()

    with pytest.raises(ReleaseManifestError, match="different release"):
        finalize_release_identity(
            {"id": "200", "tracks": {"items": [{"title": "one"}]}},
            final_dir,
            expected_destination=final_dir,
        )

    assert (final_dir / MANIFEST_NAME).read_bytes() == before
```

In `test_durable_post_import_action.py`, monkeypatch `_settle_action`, the new
`_publish_retirement_identity`, `_acknowledge_completion`, and
`process_carrier_retirement` to append their names to one `events` list; invoke
`finalize_carrier_retirement()` with the file's existing journal fixture and
assert `events == ["settle", "identity", "acknowledge", "carrier"]`.

- [ ] **Step 2: Run completion tests and verify RED**

```bash
pytest -q tests/test_process.py tests/test_durable_post_import_action.py \
  tests/test_completion.py
```

Expected: no manifest is published because finalization has no identity step.

- [ ] **Step 3: Carry and publish the canonical identity**

Serialize identity as:

```python
{"provider": identity.provider, "release_id": identity.release_id}
```

Validate that exact field set when reading queue state. In synchronous
processing, locate the imported directory by managed signatures, require it to
equal `placement.destination`, and verify existing completion evidence for
every requested slot. A full-album request must satisfy full-album coverage; a
deliberately partial scope must prove its imported subset and have no
contradictory local tracks. Then publish. In durable finalization,
publish only after `_settle_action()` has produced its exact final path and
before `_acknowledge_completion()` retires evidence. If publication raises,
leave journal evidence and relocation completion retained for startup recovery.

- [ ] **Step 4: Run completion and startup recovery tests**

```bash
pytest -q tests/test_process.py tests/test_durable_post_import_action.py \
  tests/test_completion.py tests/test_startup_recovery.py
ruff check src/qobuz_librarian/modes/process.py \
  src/qobuz_librarian/queue/durable_album.py \
  src/qobuz_librarian/queue/post_import_finalizer.py \
  src/qobuz_librarian/completion.py
```

Expected: all tests PASS and interruption leaves no false identity.

- [ ] **Step 5: Commit durable manifest finalization**

```bash
git add src/qobuz_librarian/modes/process.py \
  src/qobuz_librarian/queue/durable_album.py \
  src/qobuz_librarian/queue/post_import_finalizer.py \
  src/qobuz_librarian/completion.py tests/test_process.py \
  tests/test_durable_post_import_action.py tests/test_completion.py
git commit -m "feat: finalize release identity after import proof"
```

### Task 8: Make Moves and Consolidation Respect Release Boundaries

**Files:**

- Modify: `src/qobuz_librarian/library/post_import_relocation.py`
- Modify: `src/qobuz_librarian/modes/consolidate.py`
- Modify: `src/qobuz_librarian/library/catalog.py`
- Modify: `tests/test_post_import_relocation.py`
- Modify: `tests/test_catalog.py`

**Interfaces:**

- Produces: `release_merge_allowed(left: Path, right: Path) -> bool`; true only when both manifests are absent or both contain the same identity.
- Relocation snapshots include the reserved manifest as validated internal metadata, not as an audio unit.
- Whole-album moves carry the exact manifest; split-gap-fill refuses different identities.

- [ ] **Step 1: Add same/different/malformed identity move tests**

Create destination/source fixtures with manifests and assert:

```python
assert release_merge_allowed(unmarked_a, unmarked_b) is True
assert release_merge_allowed(same_100_a, same_100_b) is True
assert release_merge_allowed(release_100, release_200) is False
assert release_merge_allowed(release_100, malformed) is False
```

Add a relocation test proving a whole-album move retains the manifest and a
split-gap-fill with different IDs returns an attention error before copying.

```python
def test_whole_album_relocation_carries_release_manifest(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))
    publish_release_identity(source, ReleaseIdentity("qobuz", "100"))
    authority = run_lock.acquire()
    try:
        result = relocation.relocate_post_import_album(
            source, destination, kind=relocation.RelocationKind.WHOLE_ALBUM,
            authority=authority)
    finally:
        authority.close()
    assert result.changed is True
    assert read_release_identity(destination).release_id == "100"


def test_split_gap_fill_refuses_different_release_ids(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    track = source / "01.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))
    publish_release_identity(source, ReleaseIdentity("qobuz", "100"))
    publish_release_identity(destination, ReleaseIdentity("qobuz", "200"))
    authority = run_lock.acquire()
    try:
        with pytest.raises(
                relocation.PostImportRelocationUnavailable, match="release"):
            relocation.relocate_post_import_album(
                source, destination,
                kind=relocation.RelocationKind.SPLIT_GAP_FILL,
                authority=authority)
    finally:
        authority.close()
    assert source.exists() and destination.exists()
```

Import the identity helpers beside the existing `relocation` and `run_lock`
imports in `tests/test_post_import_relocation.py`.

- [ ] **Step 2: Run relocation/consolidation tests and verify RED**

```bash
pytest -q tests/test_post_import_relocation.py tests/test_catalog.py
```

Expected: current merge rules do not inspect release identity.

- [ ] **Step 3: Add identity gates before every merge plan**

Implement:

```python
def release_merge_allowed(left, right):
    try:
        left_identity = read_release_identity(left)
        right_identity = read_release_identity(right)
    except ReleaseManifestError:
        return False
    return (left_identity is None and right_identity is None
            or left_identity is not None and left_identity == right_identity)
```

Call it before `_is_split_album_merge()`, consolidation group planning, and
post-import relocation plan construction. Keep the manifest in sealed tree
snapshots so a concurrent identity change invalidates the reviewed operation.
For a whole-album move, require the destination manifest to be missing or the
same before publication.

- [ ] **Step 4: Run relocation, consolidation, and process tests**

```bash
pytest -q tests/test_post_import_relocation.py tests/test_catalog.py \
  tests/test_process.py
ruff check src/qobuz_librarian/library/post_import_relocation.py \
  src/qobuz_librarian/modes/consolidate.py src/qobuz_librarian/library/catalog.py
```

Expected: all tests PASS and different IDs never enter a merge plan.

- [ ] **Step 5: Commit identity-aware move guards**

```bash
git add src/qobuz_librarian/library/post_import_relocation.py \
  src/qobuz_librarian/modes/consolidate.py \
  src/qobuz_librarian/library/catalog.py \
  tests/test_post_import_relocation.py tests/test_catalog.py
git commit -m "fix: prevent cross-release album merges"
```

### Task 9: Transfer Identity Explicitly During Migration

**Files:**

- Modify: `src/qobuz_librarian/library/migrate.py`
- Modify: `tests/test_migrate.py`

**Interfaces:**

- Extends: `MigrationPlan` with `release_identities: list[dict]` records containing source folder receipt, destination folder, and canonical identity.
- Generic `companion_receipts` never contains the manifest.
- Execution publishes a destination manifest only after every mapped audio placement for that album succeeds or verifies as an identical resume.

- [ ] **Step 1: Write migration plan and execution tests**

Add a source album with release `100`, one audio file, artwork, and the
manifest. Assert preview separates the records:

```python
assert [r["identity"]["release_id"] for r in plan.release_identities] == ["100"]
assert all(r["relative"][-1] != MANIFEST_NAME for r in plan.companion_receipts)
```

Execute and assert the destination manifest is `100`. Add a destination
manifest `200` case and assert migration reports a collision without copying
audio or replacing either manifest.

- [ ] **Step 2: Run migration tests and verify RED**

```bash
pytest -q tests/test_migrate.py
```

Expected: preview has no release-identity channel and execution cannot publish it.

- [ ] **Step 3: Add sealed internal metadata receipts**

During descriptor enumeration, address `MANIFEST_NAME` directly per mapped
album folder, validate with the identity codec, and store its file receipt plus
canonical identity outside companions. Include these records in plan JSON
schema validation and durable result evidence. At execution, verify the source
receipt still matches, verify every album audio outcome, then call
`publish_release_identity(destination_folder, identity)`. A different or
malformed destination manifest marks that album failed and leaves source and
destination unchanged.

- [ ] **Step 4: Run migration and durable payload tests**

```bash
pytest -q tests/test_migrate.py tests/test_web.py
ruff check src/qobuz_librarian/library/migrate.py tests/test_migrate.py
```

Expected: all tests PASS and migration summaries do not count the manifest as a companion.

- [ ] **Step 5: Commit migration identity transfer**

```bash
git add src/qobuz_librarian/library/migrate.py tests/test_migrate.py
git commit -m "feat: preserve release identity during migration"
```

### Task 10: Preserve Identity Through Backup and Restore

**Files:**

- Modify: `src/qobuz_librarian/library/backup.py`
- Modify: `src/qobuz_librarian/modes/process.py`
- Modify: `tests/test_backup_and_catalog_helpers.py`
- Modify: `tests/test_capped_durability.py`

**Interfaces:**

- Whole-album backup receipts add optional canonical `release_identity` bound to the exact manifest file receipt.
- Produces: `carry_backup_release_identity(backup: BackupResult, replacement: Path, expected_identity: ReleaseIdentity) -> dict | None`, returning an updated replacement receipt on success.
- Full restore retains the original manifest naturally and validates it before replacing a partial album.
- Gap-fill backup continues to carry selected audio only; the live album manifest never enters its payload.

- [ ] **Step 1: Add backup, replacement, and restore identity tests**

Add these whole-album receipt and restore assertions to a test using the
existing real filesystem fixtures:

```python
backup = backup_album_dir(album_with_release_100)
assert backup.receipt["release_identity"] == {
    "provider": "qobuz", "release_id": "100"
}

assert restore_upgrade_backup(backup, original_path) is True
assert read_release_identity(original_path).release_id == "100"
```

Add the following explicit assertions in separate tests:

```python
replacement.mkdir(parents=True)
(replacement / "01.flac").write_bytes(b"replacement")
updated = carry_backup_release_identity(
    backup, replacement, ReleaseIdentity("qobuz", "100"))
assert updated is not None
assert read_release_identity(replacement).release_id == "100"

publish_release_identity(conflicting_replacement, ReleaseIdentity("qobuz", "200"))
assert carry_backup_release_identity(
    backup, conflicting_replacement, ReleaseIdentity("qobuz", "100")) is None
assert backup.path.exists()
assert read_release_identity(conflicting_replacement).release_id == "200"

gap_backup = backup_gap_fill_files([track], album)
assert MANIFEST_NAME not in gap_backup.receipt["tree"]["files"]
```

- [ ] **Step 2: Run backup tests and verify RED**

```bash
pytest -q tests/test_backup_and_catalog_helpers.py tests/test_capped_durability.py
```

Expected: receipts lack `release_identity`, and companion carry ignores the JSON manifest.

- [ ] **Step 3: Bind identity to whole-album backup receipts**

At `backup_album_dir()` preflight, read and validate the source identity while
the source directory is sealed. Add canonical identity plus the exact manifest
snapshot to the receipt schema. Whole-tree backup/restore continues moving or
copying the file, but restore must validate its content against the receipt
before publication.

Implement `carry_backup_release_identity()` separately from
`carry_backup_companions()`: validate the backup receipt identity equals the
expected Qobuz release, require the replacement manifest to be missing or the
same, publish it through `publish_release_identity()`, and return a fresh
`capture_album_source_receipt()`. In `_carry_non_audio_from_backup()`, run this
identity step before generic companions and pass its updated receipt forward.

- [ ] **Step 4: Run backup, process, and recovery tests**

```bash
pytest -q tests/test_backup_and_catalog_helpers.py tests/test_capped_durability.py \
  tests/test_process.py tests/test_startup_recovery.py
ruff check src/qobuz_librarian/library/backup.py \
  src/qobuz_librarian/modes/process.py
```

Expected: all tests PASS; cross-release restore/carry leaves the backup authoritative.

- [ ] **Step 5: Commit identity-aware backup handling**

```bash
git add src/qobuz_librarian/library/backup.py \
  src/qobuz_librarian/modes/process.py \
  tests/test_backup_and_catalog_helpers.py tests/test_capped_durability.py
git commit -m "feat: preserve release identity in album backups"
```

### Task 11: Compute Edition Badges for Confusable Release Families

**Files:**

- Create: `src/qobuz_librarian/library/edition_badges.py`
- Create: `tests/test_edition_badges.py`

**Interfaces:**

- Consumes: `normalise_release_id(value) -> str | None` from Task 1.
- Consumes: `normalize()`, `strip_album_decorations()`, and `album_quality_label()` from existing library helpers.
- Produces: `edition_family_key(album: dict) -> tuple[str, str, str] | None`.
- Produces: `edition_label(album: dict) -> str`, returning a human marker or `Standard Edition`.
- Produces: `build_edition_badges(albums: list[dict]) -> dict[str, str]`, keyed by normalized Qobuz release ID.

- [ ] **Step 1: Write the failing pure badge tests**

Create `tests/test_edition_badges.py` with deterministic catalogue records:

```python
from datetime import datetime, timezone

from qobuz_librarian.library.edition_badges import build_edition_badges


def _released(year):
    return datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()


def _album(album_id, title, original_year, *, published=2020, tracks=10,
           bits=16, sample_rate=44.1, artist="Artist"):
    return {
        "id": album_id,
        "title": title,
        "artist": {"name": artist},
        "release_date_original": f"{original_year}-01-01",
        "released_at": _released(published),
        "tracks_count": tracks,
        "maximum_bit_depth": bits,
        "maximum_sampling_rate": sample_rate,
    }


def test_standard_and_deluxe_family_get_visible_qobuz_ids():
    badges = build_edition_badges([
        _album("100", "Album", 2020),
        _album("200", "Album (Deluxe Edition)", 2020, tracks=14),
    ])

    assert badges == {
        "100": "Standard Edition · Qobuz 100",
        "200": "Deluxe Edition · Qobuz 200",
    }


def test_single_release_and_different_original_years_are_not_badged():
    assert build_edition_badges([_album("100", "Album", 2020)]) == {}
    assert build_edition_badges([
        _album("100", "Artist", 1990),
        _album("200", "Artist", 2020),
    ]) == {}


def test_duplicate_labels_use_ordered_minimum_differentiators():
    badges = build_edition_badges([
        _album("100", "Album (Deluxe Edition)", 2020, published=2021),
        _album("200", "Album (Deluxe Edition)", 2020, published=2022),
    ])
    assert badges["100"] == "Deluxe Edition · 2021 · Qobuz 100"
    assert badges["200"] == "Deluxe Edition · 2022 · Qobuz 200"

    track_badges = build_edition_badges([
        _album("300", "Other (Remastered)", 2020, published=2022, tracks=10),
        _album("400", "Other (Remastered)", 2020, published=2022, tracks=12),
    ])
    assert track_badges["300"] == "Remastered · 10 tracks · Qobuz 300"
    assert track_badges["400"] == "Remastered · 12 tracks · Qobuz 400"

    quality_badges = build_edition_badges([
        _album("500", "Third (Expanded Edition)", 2020, published=2022,
               tracks=12, bits=16, sample_rate=44.1),
        _album("600", "Third (Expanded Edition)", 2020, published=2022,
               tracks=12, bits=24, sample_rate=96),
    ])
    assert quality_badges["500"] == "Expanded Edition · 16-bit/44.1kHz · Qobuz 500"
    assert quality_badges["600"] == "Expanded Edition · 24-bit/96kHz · Qobuz 600"
```

- [ ] **Step 2: Run the badge tests and verify RED**

```bash
pytest -q tests/test_edition_badges.py
```

Expected: FAIL because `library.edition_badges` does not exist.

- [ ] **Step 3: Implement family grouping and ordered labels**

Use these public boundaries and marker patterns:

```python
_EDITION_MARKER = re.compile(
    r"(?ix)\b("
    r"(?:\d{4}\s+)?remaster(?:ed)?|"
    r"deluxe(?:\s+edition)?|expanded(?:\s+edition)?|"
    r"special(?:\s+edition)?|collector(?:'s)?(?:\s+edition)?|"
    r"(?:\d{1,3}(?:st|nd|rd|th)\s+)?anniversary(?:\s+edition)?"
    r")\b"
)


def edition_family_key(album):
    artist = normalize(((album.get("artist") or {}).get("name") or ""))
    title = normalize(strip_album_decorations(album.get("title") or ""))
    original_year = _original_release_year(album)
    return (artist, title, original_year) if artist and title and original_year else None


def edition_label(album):
    title = album.get("title") or ""
    match = _EDITION_MARKER.search(title)
    return " ".join(match.group(1).split()) if match else "Standard Edition"
```

`build_edition_badges()` normalizes IDs, removes duplicate API rows for the
same ID, groups by `edition_family_key()`, and skips groups with fewer than two
IDs. Within each repeated human label, append the first publication-year value
that makes labels distinct; if it does not, append track count; if that still
does not, append `album_quality_label()`. Finish every badge with
` · Qobuz ` followed by the complete normalized release ID. Missing fallback metadata is skipped rather than
displayed as `?`, and the Qobuz ID remains the final unique differentiator.

- [ ] **Step 4: Run badge and tag/catalog tests**

```bash
pytest -q tests/test_edition_badges.py tests/test_tags.py tests/test_catalog.py
ruff check src/qobuz_librarian/library/edition_badges.py \
  tests/test_edition_badges.py
```

Expected: all tests PASS and unrelated title normalization remains unchanged.

- [ ] **Step 5: Commit the pure badge component**

```bash
git add src/qobuz_librarian/library/edition_badges.py tests/test_edition_badges.py
git commit -m "feat: label confusable qobuz editions"
```

### Task 12: Attach Edition Badges and Surface Identity Review Cards

**Files:**

- Modify: `src/qobuz_librarian/web/flows.py`
- Modify: `src/qobuz_librarian/web/templates/_review_group_items.html`
- Modify: `src/qobuz_librarian/web/static/src/app.css`
- Modify: `src/qobuz_librarian/library/discovery.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_web.py`
- Modify: `docs/existing-libraries.md`
- Modify: `docs/troubleshooting.md`

**Interfaces:**

- Consumes: `build_edition_badges(albums: list[dict]) -> dict[str, str]` from Task 11.
- Extends: `AlbumGap` with `edition_badge: str = ""`.
- Adds `payload.edition_badge` to both Missing Albums and Gap Fill review candidates when non-empty.
- Produces: `_identity_attention_spec(item: dict, artist_name: str, artist_key: str) -> dict`, a persisted review candidate with `kind="identity_attention"`, `selected=False`, and `payload.non_actionable=True`.
- Extends `_scan_library_artist()` to return identity-related `DiscoveryResult.skipped` entries alongside gaps.
- The review template renders these entries without a checkbox, so they inform but can never authorize an overwrite or download.
- Documents the reserved manifest, collision suffix, lazy adoption, and manual-review safety behavior.

- [ ] **Step 1: Add discovery, persistence, rendering, and identity-attention tests**

```python
def test_missing_and_gap_fill_receive_badges_from_catalog_family(
        monkeypatch, tmp_path):
    standard = _album("100", "Album", "Artist", 2020,
                      [_qt("one", "A"), _qt("two", "B")])
    deluxe = _album("200", "Album (Deluxe Edition)", "Artist", 2020,
                    [_qt("remixed", "Z"), _qt("bonus", "C")])
    missing_standard = _album("300", "Other", "Artist", 2021,
                              [_qt("x", "X")])
    missing_deluxe = _album("400", "Other (Deluxe Edition)", "Artist", 2021,
                            [_qt("x", "X"), _qt("y", "Y")])
    result = _run(
        monkeypatch,
        tmp_path,
        query="Artist",
        artist_folder="Artist",
        layout={"Artist": {"Album (2020)": [_et("one", "A")]}},
        catalog=[standard, deluxe, missing_standard, missing_deluxe],
        artists=[{"name": "Artist", "id": "artist", "albums_count": 4}],
    )

    gap_fill = next(g for g in result.gaps if g.on_disk_dir is not None)
    missing = next(g for g in result.gaps if g.qobuz_album["id"] == "300")
    assert gap_fill.edition_badge == "Standard Edition · Qobuz 100"
    assert missing.edition_badge == "Standard Edition · Qobuz 300"


def test_unambiguous_catalog_album_has_no_badge(monkeypatch, tmp_path):
    only = _album("100", "Only Album", "Artist", 2020, [_qt("one", "A")])
    result = _run(
        monkeypatch, tmp_path, query="Artist", artist_folder="Artist",
        layout={"Artist": {}}, catalog=[only],
        artists=[{"name": "Artist", "id": "artist", "albums_count": 1}],
    )
    assert result.gaps[0].edition_badge == ""


def test_identity_attention_spec_is_non_actionable():
    spec = flows._identity_attention_spec(
        {
            "dir": Path("/music/Artist/Album (2020)"),
            "reason": "identity_ambiguous",
            "candidate_releases": [
                {"id": "100", "title": "Album"},
                {"id": "200", "title": "Album (Deluxe Edition)"},
            ],
        },
        "Artist",
        "Artist",
    )

    assert spec["kind"] == "identity_attention"
    assert spec["selected"] is False
    assert spec["payload"]["non_actionable"] is True
    assert spec["payload"]["candidate_ids"] == ["100", "200"]
    assert "Multiple Qobuz editions match" in spec["detail"]


def test_identity_attention_review_card_has_no_checkbox(client):
    job = jm.Job(title="Library scan")
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate(
        "identity_attention",
        "Album (2020)",
        "Artist",
        "Multiple Qobuz editions match · Qobuz IDs 100, 200",
        {"non_actionable": True, "candidate_ids": ["100", "200"]},
        selected=False,
    )
    jm.registry.add(job)
    try:
        response = client.get(f"/jobs/{job.id}/review")
        card = response.text.split("data-identity-attention", 1)[1].split("</div>", 1)[0]
        assert "Qobuz IDs 100, 200" in card
        assert 'name="cid"' not in card
    finally:
        _remove_job(job)


def test_library_review_renders_and_persists_edition_badges(client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    job = jm.Job(title="Library scan")
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    for gap_fill, album_id, title, badge in (
        (False, "100", "Album", "Standard Edition · Qobuz 100"),
        (True, "200", "Album (Deluxe Edition)",
         "Deluxe Edition · Qobuz 200"),
    ):
        job.add_candidate(
            "album", title, "Artist", "2020 · 16-bit/44.1kHz · 10 tracks",
            {"album_id": album_id, "gap_fill": 1 if gap_fill else 0,
             "edition_badge": badge},
            selected=False,
        )
    jm.registry.add(job)
    try:
        job_persistence.persist(job)
        restored = job_persistence.load_one(job.id)
        assert restored is not None
        assert [c["payload"]["edition_badge"] for c in restored["candidates"]] == [
            "Standard Edition · Qobuz 100",
            "Deluxe Edition · Qobuz 200",
        ]
        assert [c["payload"]["album_id"] for c in restored["candidates"]] == [
            "100", "200"]

        for tab, expected in (
            ("missing", "Standard Edition · Qobuz 100"),
            ("gaps", "Deluxe Edition · Qobuz 200"),
        ):
            response = client.get(f"/jobs/{job.id}/review", params={"tab": tab})
            assert expected in response.text
    finally:
        _remove_job(job)
```

The setup matches the existing persistence tests in that file. The separate
`album_id` assertion proves badge text does not replace the release ID carried
into execution.

- [ ] **Step 2: Run web review tests and verify RED**

```bash
pytest -q tests/test_web.py
```

Expected: `AlbumGap` has no `edition_badge`, `_identity_attention_spec` is missing, and the template renders no edition badge.

- [ ] **Step 3: Add non-actionable review cards and documentation**

In `find_missing_for_artist()`, call `build_edition_badges(catalog)` only when
`result.catalog_incomplete` is false, then assign the string matching each
gap's normalized release ID to `gap.edition_badge`. `_gap_candidate_spec()`
copies a non-empty value into `payload["edition_badge"]`; it does not recompute
catalogue grouping in the web layer.

Map `identity_ambiguous`, `identity_invalid`, `identity_conflict`,
`identity_path_occupied`, and `identity_changed` to stable detail strings in
`_identity_attention_spec()`. Return identity skips from `_scan_library_artist`,
append the non-actionable specs on the single writer thread, and persist them in
the existing scan checkpoint candidate list. In `_review_group_items.html`,
render a `<div class="ql-review-candidate">` instead of a checkbox `<label>`
when `c.payload.non_actionable` is true, and stamp it with the
`data-identity-attention` attribute used by the rendering test.

For ordinary album candidates, render `c.payload.edition_badge` beside the
title with existing badge primitives and a `ql-badge-edition` class. In
`app.css`, allow that class to wrap and never truncate its Qobuz ID:

```css
.ql-badge-edition {
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
}
```

In `existing-libraries.md`, document that manifests are created lazily after a
proven match and that ambiguous standard/deluxe folders remain unchanged. Also
document that Missing Albums and Gap Fill show a human edition label plus the
complete Qobuz ID only when a trustworthy catalogue exposes multiple releases
in the same family. In `troubleshooting.md`, document how to inspect the
manifest, why `[qobuz-ID]` appears only on collisions, and that users must not
edit or copy a manifest between releases.

- [ ] **Step 4: Run documentation-adjacent tests and lint**

```bash
pytest -q tests/test_edition_badges.py tests/test_web.py tests/test_discovery.py
ruff check src/qobuz_librarian/library/discovery.py \
  src/qobuz_librarian/web/flows.py tests/test_discovery.py tests/test_web.py
```

Expected: all tests PASS.

- [ ] **Step 5: Commit review UX and docs**

```bash
git add src/qobuz_librarian/web/flows.py \
  src/qobuz_librarian/web/templates/_review_group_items.html \
  src/qobuz_librarian/web/static/src/app.css \
  src/qobuz_librarian/library/discovery.py \
  tests/test_discovery.py tests/test_web.py \
  docs/existing-libraries.md docs/troubleshooting.md
git commit -m "feat: show qobuz editions in library reviews"
```

### Task 13: Run End-to-End Regression and Safety Verification

**Files:**

- No planned file changes; this task verifies the committed implementation.

**Interfaces:**

- Consumes every prior task's committed interface.
- Produces a clean full test and lint run with no ordinary-album path changes.

- [ ] **Step 1: Run the identity-focused matrix**

```bash
pytest -q tests/test_release_identity.py tests/test_album_placement.py \
  tests/test_edition_badges.py tests/test_catalog.py tests/test_discovery.py \
  tests/test_integrations.py \
  tests/test_post_import_relocation.py tests/test_migrate.py \
  tests/test_backup_and_catalog_helpers.py tests/test_capped_durability.py
```

Expected: all tests PASS.

- [ ] **Step 2: Run the complete test suite**

```bash
pytest -q
```

Expected: all tests PASS with no skips newly introduced by this feature.

- [ ] **Step 3: Run project lint**

```bash
ruff check src/qobuz_librarian tests
```

Expected: Ruff reports no errors.

- [ ] **Step 4: Inspect the final change set and safety invariants**

```bash
git status --short
git diff --check main~12..HEAD
git log --oneline --decorate -13
```

Verify from the test names and diff that ordinary paths are unchanged, the
complete Qobuz ID suffix appears only on collisions, manifests are absent from
generic enumerations, migration and backup retain them explicitly, and no
different-release merge path remains. Verify edition badges appear on both
Library tabs only for multi-ID release families, contain the complete Qobuz ID,
survive persistence, and never replace the `album_id` used for execution.
