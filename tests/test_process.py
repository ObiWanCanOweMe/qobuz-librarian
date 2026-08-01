from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.library.album_placement import (
    resolve_album_placement,
)
from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseIdentity,
    ReleaseManifestError,
    publish_release_identity,
    read_release_identity,
)


def _patch_download_receipts(monkeypatch, download, added, run_root):
    from qobuz_librarian.integrations.staging import StagedFile, StagingRun

    value = run_root.stat()
    run = StagingRun(run_root, (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    ))

    def sealed_added(_snapshot):
        receipts = []
        for item in added:
            value = item.stat()
            receipts.append(StagedFile(item, (
                value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns,
            )))
        return receipts

    monkeypatch.setattr(download, "files_added_since", sealed_added)
    monkeypatch.setattr(download, "create_staging_run", lambda: run)


def _adopted_placement(album_dir, release_id="ALB"):
    identity = ReleaseIdentity("qobuz", release_id)
    return resolve_album_placement(
        album_dir,
        identity,
        adopted_identity=identity,
    )


def _args(**over):
    base = dict(force=False, yes=True, no_import=False, dry_run=False,
                verbose=False, consolidate=False, no_upgrade=False,
                no_downsample=True,
                auto_upgrade=False, prefer_hires=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def authority(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "process-run.lock")
    lease = run_lock.acquire()
    try:
        yield lease
    finally:
        lease.close()


def test_finalize_release_identity_rejects_wrong_destination_identity(
    tmp_path, authority
):
    from qobuz_librarian.modes.process import finalize_release_identity

    final_dir = tmp_path / "Artist" / "Album (2020) [qobuz-200]"
    final_dir.mkdir(parents=True)
    publish_release_identity(final_dir, ReleaseIdentity("qobuz", "100"))
    before = (final_dir / MANIFEST_NAME).read_bytes()

    with pytest.raises(ReleaseManifestError, match="different release"):
        finalize_release_identity(
            {"id": "200", "tracks": {"items": [{"title": "one"}]}},
            final_dir,
            expected_destination=final_dir,
            authority=authority,
        )
    assert (final_dir / MANIFEST_NAME).read_bytes() == before


def test_non_audio_carry_publishes_identity_before_generic_companions(
        tmp_path, monkeypatch):
    from qobuz_librarian.modes import process as proc

    replacement = tmp_path / "music" / "Artist" / "Album"
    replacement.mkdir(parents=True)
    backup = SimpleNamespace(path=tmp_path / "backups" / "Album")
    identity_receipt = {"receipt": "after-identity"}
    final_receipt = {"receipt": "after-companions"}
    calls = []

    def carry_identity(actual_backup, path, identity):
        calls.append(("identity", actual_backup, path, identity))
        return identity_receipt

    def carry_companions(actual_backup, path, *, expected_replacement_receipt):
        calls.append((
            "companions",
            actual_backup,
            path,
            expected_replacement_receipt,
        ))
        return final_receipt

    monkeypatch.setattr(
        proc, "carry_backup_release_identity", carry_identity, raising=False)
    monkeypatch.setattr(proc, "carry_backup_companions", carry_companions)

    carried = proc._carry_non_audio_from_backup(
        {"id": "100"},
        replacement,
        backup,
        replacement_dir=replacement,
    )

    assert carried == (replacement, final_receipt)
    assert calls == [
        (
            "identity",
            backup,
            replacement,
            ReleaseIdentity("qobuz", "100"),
        ),
        ("companions", backup, replacement, identity_receipt),
    ]

def test_finalize_release_identity_requires_the_exact_planned_destination(
    tmp_path, authority,
):
    from qobuz_librarian.modes.process import finalize_release_identity

    final_dir = tmp_path / "Artist" / "Album"
    other = tmp_path / "Artist" / "Other"
    final_dir.mkdir(parents=True)
    other.mkdir()

    with pytest.raises(ReleaseManifestError, match="planned destination"):
        finalize_release_identity(
            {"id": "200", "tracks": {"items": [{"title": "one"}]}},
            final_dir,
            expected_destination=other,
            authority=authority,
        )

    assert read_release_identity(final_dir) is None


def test_finalize_release_identity_refuses_lost_authority(tmp_path, authority):
    from qobuz_librarian.modes.process import finalize_release_identity

    final_dir = tmp_path / "Artist" / "Album"
    final_dir.mkdir(parents=True)
    authority.close()
    with pytest.raises(OSError, match="run lock"):
        finalize_release_identity(
            {"id": "200", "tracks": {"items": [{"title": "one"}]}},
            final_dir,
            expected_destination=final_dir,
            authority=authority,
        )
    assert read_release_identity(final_dir) is None


def test_final_slot_proof_rejects_same_count_substitution(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    final_dir = tmp_path / "Album"
    final_dir.mkdir()
    first = final_dir / "01.flac"
    second = final_dir / "02.flac"
    first.write_bytes(b"first")
    second.write_bytes(b"substitute")
    expected = {
        "qobuz:1": ("artist", "album", 1, 1, "one"),
        "qobuz:2": ("artist", "album", 1, 2, "two"),
    }
    signatures = {
        first: expected["qobuz:1"],
        second: ("artist", "album", 1, 2, "wrong recording"),
    }
    monkeypatch.setattr(proc, "_flac_signature", lambda path: signatures[path])

    assert not proc._final_album_matches_frozen_slots(final_dir, expected)


def test_slot_proof_freezes_requested_and_reviewed_baseline(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    staged = tmp_path / "staged.flac"
    baseline = tmp_path / "baseline.flac"
    staged.write_bytes(b"requested")
    baseline.write_bytes(b"baseline")
    requested = {
        "id": "1", "title": "One", "media_number": 1,
        "track_number": 1, "duration": 100,
    }
    present = {
        "id": "2", "title": "Two", "media_number": 1,
        "track_number": 2, "duration": 100,
    }
    album = {"tracks": {"items": [requested, present]}}
    existing = [{
        "title": "Two", "discnumber": 1, "tracknumber": 2,
        "length": 100, "path": str(baseline),
    }]
    signatures = {
        staged: ("artist", "album", 1, 1, "one"),
        baseline: ("artist", "album", 1, 2, "two"),
    }
    monkeypatch.setattr(proc, "_flac_signature", lambda path: signatures[path])
    monkeypatch.setattr(proc, "refresh_staged_track_bindings", lambda _result: ())
    monkeypatch.setattr(
        proc,
        "exact_download_coverage",
        lambda *_args: SimpleNamespace(bindings=(
            SimpleNamespace(slot="qobuz:1", path=str(staged)),
        )),
    )

    assert proc._freeze_release_slot_signatures(
        album, [requested], [present], existing, {}
    ) == {
        "qobuz:1": signatures[staged],
        "qobuz:2": signatures[baseline],
    }


def test_synchronous_partial_split_is_not_identity_authority(tmp_path):
    from qobuz_librarian.modes import process as proc

    source = tmp_path / "Other" / "Album"
    destination = tmp_path / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "01.flac").write_bytes(b"source recording")
    (destination / "01.flac").write_bytes(b"contradictory recording")
    (destination / "02.flac").write_bytes(b"moved recording")
    result = SimpleNamespace(
        changed=True,
        published_files=1,
        destination=destination,
        reason="kept 1 existing destination name(s)",
    )

    assert not proc._split_relocation_allows_identity(result)
    assert (source / "01.flac").exists()
    assert not (destination / MANIFEST_NAME).exists()


@pytest.mark.parametrize(
    ("beets_succeeds", "coverage_proven", "extra_audio", "late_cancel",
     "split_noop"),
    [(False, True, False, False, False), (True, False, False, False, False),
     (True, True, False, False, False), (True, True, True, False, False),
     (True, True, False, True, False), (True, True, False, False, True)],
)
def test_process_publishes_identity_only_after_exact_import_proof(
    monkeypatch,
    tmp_path,
    beets_succeeds,
    coverage_proven,
    extra_audio,
    late_cancel,
    split_noop,
    authority,
):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import post_import_relocation
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging" / "Artist" / "Album"
    staging.mkdir(parents=True)
    final_dir = tmp_path / "music" / "Artist" / "Album"
    final_dir.mkdir(parents=True)
    split_source = tmp_path / "music" / "Other" / "Album"
    if split_noop:
        split_source.mkdir(parents=True)
        (split_source / "01.flac").write_bytes(b"source recording")
    (final_dir / "01.flac").write_bytes(b"one")
    if extra_audio:
        (final_dir / "99.flac").write_bytes(b"contradictory")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging.parent.parent)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    tracks = [{"id": "10", "title": "One", "track_number": 1}]
    album = {
        "id": "200",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96.0,
        "tracks": {"items": tracks},
    }

    monkeypatch.setattr(proc, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        proc,
        "find_existing_tracks",
        lambda _album: ([], split_source if split_noop else None),
    )
    monkeypatch.setattr(proc, "compute_missing", lambda wanted, _have: (wanted, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _album: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    state = {"imported": False, "placement_revalidated": False}
    monkeypatch.setattr(
        proc,
        "is_cancel_requested",
        lambda: bool(late_cancel and state["imported"]),
    )
    monkeypatch.setattr(proc, "validated_staged_album_dirs", lambda _result: [staging])
    monkeypatch.setattr(
        proc,
        "_pre_import_staging_hooks",
        lambda _args, _dirs=None: ([], 0),
    )
    monkeypatch.setattr(proc, "track_signatures_for_album_dirs", lambda _dirs: ["sig"])
    monkeypatch.setattr(
        proc,
        "find_album_dir_by_track_signatures",
        lambda signatures: final_dir if signatures == ["sig"] else None,
    )
    monkeypatch.setattr(
        proc,
        "folder_holds_all_tracks",
        lambda *_args, **_kwargs: coverage_proven,
    )
    monkeypatch.setattr(
        proc,
        "_freeze_release_slot_signatures",
        lambda *_args, **_kwargs: {"qobuz:10": ("a", "b", 1, 1, "one")},
    )
    monkeypatch.setattr(
        proc,
        "_final_album_matches_frozen_slots",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        proc,
        "resolve_album_import_placement",
        lambda _album, _token: _adopted_placement(final_dir, "200"),
    )
    monkeypatch.setattr(
        proc,
        "require_album_placement_current",
        lambda _placement: state.update(placement_revalidated=True),
        raising=False,
    )
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _dirs: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _event: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *_args: None)
    monkeypatch.setattr(proc, "_is_split_album_merge", lambda *_args: split_noop)
    monkeypatch.setattr(
        post_import_relocation,
        "relocate_post_import_album",
        lambda *_args, **_kwargs: SimpleNamespace(
            changed=False,
            published_files=0,
            destination=final_dir,
            reason="all destination names already exist",
        ),
    )
    monkeypatch.setattr(
        proc,
        "verify_and_recover",
        lambda *_args, **_kwargs: {
            "under": False,
            "recovered": False,
            "retried": False,
            "staged_dirs": [staging],
        },
    )

    def download(**kwargs):
        kwargs["result"].update(
            n_ok=1,
            n_fail=0,
            n_lossy=0,
            failed_tracks=[],
            lossy_tracks=[],
            elapsed=0.0,
            gap_fill_backup_path=None,
        )

    def import_album(*_args, **_kwargs):
        assert not (final_dir / MANIFEST_NAME).exists()
        assert state["placement_revalidated"] is True
        state["imported"] = beets_succeeds
        return beets_succeeds

    monkeypatch.setattr(proc, "run_album_download", download)
    monkeypatch.setattr(proc, "beets_import_paths", import_album)

    result = proc.process_album(album, _args(), token="token")

    if (
        beets_succeeds and coverage_proven and not extra_audio
        and not late_cancel and not split_noop
    ):
        assert read_release_identity(final_dir) == ReleaseIdentity("qobuz", "200")
    elif not beets_succeeds:
        assert result["result"] == "not_imported"
        assert not (final_dir / MANIFEST_NAME).exists()
    else:
        assert result["imported"] is True
        assert not (final_dir / MANIFEST_NAME).exists()


def test_force_stops_after_an_interrupted_backup(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"original")
    retained = SimpleNamespace(
        complete=False,
        path=tmp_path / "backups" / "Album",
    )
    recovered = []

    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _album: album_dir)
    monkeypatch.setattr(proc, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(proc, "backup_album_dir", lambda _path: retained)
    monkeypatch.setattr(
        proc,
        "_recover_incomplete_upgrade_backup",
        lambda backup, original, **kwargs: recovered.append(
            (backup, original, kwargs["operation"])),
    )

    outcome = proc.force_cleanup_preflight({}, _args(force=True))

    assert outcome is None
    assert recovered == [
        (retained, album_dir, "forced re-download backup"),
    ]


def test_partial_retention_moves_only_the_recorded_download_run(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.download import retain_download_staging
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    owned = staging_tx.create_staging_run()
    concurrent = staging_tx.create_staging_run()
    owned_file = owned.path / "Artist" / "Album" / "01.flac"
    concurrent_file = concurrent.path / "Other" / "Album" / "01.flac"
    owned_file.parent.mkdir(parents=True)
    concurrent_file.parent.mkdir(parents=True)
    owned_file.write_bytes(b"owned partial")
    concurrent_file.write_bytes(b"concurrent run")
    result = {"_staging_run": owned.to_record()}

    assert retain_download_staging(result, label="incomplete-replacement")
    assert not owned.path.exists()
    assert concurrent_file.read_bytes() == b"concurrent run"
    groups = staging_tx.list_groups(kind="interrupted")
    assert len(groups) == 1
    assert list(groups[0].trees[0].path.rglob("*.flac"))[0].read_bytes() == (
        b"owned partial"
    )


def test_completed_run_retirement_reclaims_nested_empty_directories(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.download import retire_empty_download_staging
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    run = staging_tx.create_staging_run()
    (run.path / "Artist" / "Album" / "Disc 2").mkdir(parents=True)

    assert retire_empty_download_staging({"_staging_run": run.to_record()})
    assert not run.path.exists()
    assert staging_tx.list_groups(kind="interrupted") == []


def test_cancel_after_download_skips_beets_import(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1},
              {"id": 2, "title": "B", "track_number": 2}]
    album = {"id": "X", "title": "Alb", "artist": {"name": "Ar"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: True)

    def fake_download(**kw):
        kw["result"].update(n_ok=2, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)
    monkeypatch.setattr(proc, "validated_staged_album_dirs", lambda _result: [staging])

    beets_runs = []
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: beets_runs.append(1) or True)

    result = proc.process_album(album, _args(), token="tok")

    assert result["result"] == "cancelled"
    assert result["imported"] is False
    assert beets_runs == []


def test_treat_as_new_downloads_an_owned_album_as_a_separate_edition(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1},
              {"id": 2, "title": "B", "track_number": 2}]
    album = {"id": "ED99", "title": "Abbey Road", "artist": {"name": "The Beatles"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    owned_dir = tmp_path / "The Beatles" / "Abbey Road (1969)"

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (list(tracks), owned_dir))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: owned_dir)
    monkeypatch.setattr(proc, "compute_missing",
                        lambda q, e: ([], list(q)) if e else (list(q), []))
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)

    def fake_download(**kw):
        kw["result"].update(n_ok=2, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0, gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    consolidate_seen = []
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staging])
    monkeypatch.setattr(
        proc,
        "resolve_album_import_placement",
        lambda _album, _token: SimpleNamespace(suffix=" [qobuz-ED99]"),
    )
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: consolidate_seen.append((
                            k.get("consolidate"), k.get("album_path_suffix"))) or True)

    assert proc.process_album(album, _args(), token="tok")["result"] == "already_complete"
    assert consolidate_seen == []

    result = proc.process_album(album, _args(), token="tok", treat_as_new=True)
    assert result.get("imported") is True
    assert consolidate_seen == [(False, " [qobuz-ED99]")]

    def ambiguous(_album, _token):
        raise proc.AlbumImportIdentityAmbiguous(
            "identity_ambiguous: friendly path matches multiple Qobuz releases")

    monkeypatch.setattr(proc, "resolve_album_import_placement", ambiguous)
    result = proc.process_album(album, _args(), token="tok", treat_as_new=True)
    assert result["result"] == "identity_ambiguous"
    assert result["imported"] is False
    assert consolidate_seen == [(False, " [qobuz-ED99]")]


def test_auto_downsample_marks_imported_folder_when_album_resolver_misses(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staged_album = staging / "Bill Evans" / "Waltz For Debby"
    staged_album.mkdir(parents=True)
    final_dir = tmp_path / "music" / "Bill Evans" / "Waltz For Debby (2023)"
    final_dir.mkdir(parents=True)
    (final_dir / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "My Foolish Heart", "track_number": 1}]
    album = {"id": "q8m2", "title": "Waltz For Debby",
             "artist": {"name": "Bill Evans Trio"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staged_album])
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 1))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "verify_and_recover",
                        lambda *a, **k: {"under": False, "recovered": False,
                                         "retried": False, "staged_dirs": [staged_album]})
    monkeypatch.setattr(proc, "track_signatures_for_album_dirs",
                        lambda _dirs: ["sig"], raising=False)
    monkeypatch.setattr(proc, "find_album_dir_by_track_signatures",
                        lambda _sigs: final_dir, raising=False)
    marked = []
    monkeypatch.setattr(proc, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    def fake_download(**kw):
        kw["result"].update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    result = proc.process_album(album, _args(), token="tok")

    assert result["imported"] is True
    assert marked == [final_dir]


def test_self_heal_retry_signatures_use_fresh_staged_dirs(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    first_staged = staging / "first" / "Album"
    retry_staged = staging / "retry" / "Album"
    first_staged.mkdir(parents=True)
    retry_staged.mkdir(parents=True)
    old_dir = tmp_path / "music" / "Artist" / "Album"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old")
    final_dir = tmp_path / "music" / "Artist" / "Album (2026)"
    final_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1}]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: old_dir)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [first_staged])
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 1))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "verify_and_recover",
                        lambda *a, **k: {"under": False, "recovered": True,
                                         "retried": True,
                                         "staged_dirs": [retry_staged]})

    signature_dirs = []

    def fake_signatures(dirs):
        signature_dirs.append(list(dirs))
        return ["sig"] if list(dirs) == [retry_staged] else []

    monkeypatch.setattr(proc, "track_signatures_for_album_dirs", fake_signatures,
                        raising=False)
    monkeypatch.setattr(proc, "find_album_dir_by_track_signatures",
                        lambda _sigs: final_dir, raising=False)
    marked = []
    monkeypatch.setattr(proc, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    def fake_download(**kw):
        kw["result"].update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    result = proc.process_album(album, _args(), token="tok")

    assert result["imported"] is True
    assert signature_dirs == [[retry_staged]]
    assert marked == [final_dir]


def test_self_heal_retry_no_files_imports_first_staged_rip(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    first_staged = staging / "Artist" / "Album"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": [{"id": 1, "title": "A", "track_number": 1}]}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "track_signatures_for_album_dirs", lambda _d: [])
    monkeypatch.setattr(
        proc, "validated_staged_album_dirs",
        lambda _result: [first_staged] if first_staged.exists() else [],
    )

    imported = []
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: imported.append(
                            first_staged / "01.flac") or True)

    def fake_download(**kw):
        result = kw["result"]
        if kw.get("quality") == 4:
            result.update(n_ok=0, n_fail=1, n_lossy=0, failed_tracks=["A"],
                          lossy_tracks=[], broken_tracks=[], elapsed=0.0,
                          gap_fill_backup_path=None)
        else:
            first_staged.mkdir(parents=True, exist_ok=True)
            (first_staged / "01.flac").write_bytes(b"first")
            result.update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                          lossy_tracks=[], broken_tracks=[], elapsed=0.0,
                          gap_fill_backup_path=None)
        return result

    def fake_verify(_album, _staged_dirs, *, redownload_at_max, **_kw):
        dirs = redownload_at_max()
        return {"under": True, "recovered": False, "retried": True,
                "n_below": 1, "served": (16, 44100),
                "target": (24, 96000), "staged_dirs": dirs}

    monkeypatch.setattr(proc, "run_album_download", fake_download)
    monkeypatch.setattr(proc, "verify_and_recover", fake_verify)

    result = proc.process_album(album, _args(), token="tok")

    assert result["imported"] is True
    assert imported == [first_staged / "01.flac"]
    assert (first_staged / "01.flac").read_bytes() == b"first"


def test_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian import download as dl
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - T1.flac"
    owned.write_bytes(b"the-owned-original")
    existing = [{"path": str(owned), "title": "T1", "tracknumber": 1, "discnumber": 1}]

    staging_root = tmp_path / "staging"
    staging = staging_root / ".qobuz-run-000000000000000000000000"
    staging.mkdir(parents=True)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging_root)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 6)]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    missing = tracks[1:]
    new_flacs = [staging / f"0{i} - T{i}.flac" for i in range(2, 6)]
    for f in new_flacs:
        f.write_bytes(b"\x00" * 1000)
    lossy_rip = staging / "01 - T1.mp3"
    lossy_rip.write_bytes(b"lossy")

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (existing, album_dir))
    monkeypatch.setattr(proc, "compute_missing", lambda _q, _e: (missing, [tracks[0]]))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    def fake_import(*_a, **_k):
        assert not owned.exists()
        for file in new_flacs:
            (album_dir / file.name).write_bytes(file.read_bytes())
            file.unlink()
        return True

    monkeypatch.setattr(proc, "beets_import_paths", fake_import)
    monkeypatch.setattr(
        proc,
        "resolve_album_import_placement",
        lambda _album, _token: _adopted_placement(album_dir),
    )
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staging])

    monkeypatch.setattr(dl, "rip_url", lambda *a, **k: (0, ""))
    _patch_download_receipts(
        monkeypatch, dl, [*new_flacs, lossy_rip], staging)
    monkeypatch.setattr(
        dl, "cleanup_lossy",
        lambda files: (
            [file for file in files if file.suffix == ".flac"],
            [file for file in files if file.suffix == ".mp3"],
            [],
        ),
    )
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    proc.process_album(album, _args(), token="tok")

    assert owned.exists()
    assert owned.read_bytes() == b"the-owned-original"


def test_staging_overflow_under_yes_exits_general_not_auth(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets as beets_mod
    from qobuz_librarian.integrations import rip as rip_mod
    from qobuz_librarian.ui_cli.errors import EXIT_GENERAL

    staging = tmp_path / "staging"
    staging.mkdir()
    for i in range(3):
        (staging / f"leftover{i}.flac").write_bytes(b"x")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "LEFTOVER_WARN_LIMIT", 1)
    monkeypatch.setattr(rip_mod, "cleanup_staging_residue", lambda: 0)

    with pytest.raises(SystemExit) as exc:
        beets_mod.staging_preflight(SimpleNamespace(yes=True))
    assert exc.value.code == EXIT_GENERAL


def test_upgrade_verification_keeps_backup_when_replacement_is_short(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "T1", "length": 200.0, "bits": 16,
         "sample_rate": 44100, "channels": 2,
         "discnumber": 1, "tracknumber": 1},
        {"title": "T2", "length": 180.0, "bits": 16,
         "sample_rate": 44100, "channels": 2,
         "discnumber": 1, "tracknumber": 2},
        {"title": "T3", "length": 240.0, "bits": 16,
         "sample_rate": 44100, "channels": 2,
         "discnumber": 1, "tracknumber": 3},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)

    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else original[:2])
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False
    truncated = [
        dict(original[0]),
        {**original[1], "length": 20.0},
        dict(original[2]),
    ]
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else truncated)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    complete = tmp_path / "Complete"
    complete.mkdir()
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: complete)
    monkeypatch.setattr(proc, "read_album_dir", lambda _f, walk_errors=None: list(original))
    assert proc._upgrade_replacement_verified({"id": "x"}, complete, backup) is True


def test_upgrade_verification_rejects_a_masked_per_track_downgrade(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [{"title": "T1", "length": 200.0,
                 "bits": 24, "sample_rate": 48000, "channels": 2,
                 "discnumber": 1, "tracknumber": 1},
                {"title": "T2", "length": 180.0,
                 "bits": 24, "sample_rate": 48000, "channels": 2,
                 "discnumber": 1, "tracknumber": 2}]
    replacement = [{"title": "T1", "length": 200.0,
                    "bits": 24, "sample_rate": 96000, "channels": 2,
                    "discnumber": 1, "tracknumber": 1},
                   {"title": "T2", "length": 180.0,
                    "bits": 16, "sample_rate": 44100, "channels": 2,
                    "discnumber": 1, "tracknumber": 2}]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    fixed = [{"title": "T1", "length": 200.0,
              "bits": 24, "sample_rate": 96000, "channels": 2,
              "discnumber": 1, "tracknumber": 1},
             {"title": "T2", "length": 180.0,
              "bits": 24, "sample_rate": 48000, "channels": 2,
              "discnumber": 1, "tracknumber": 2}]
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else fixed)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is True

    def verifies(old_tracks, new_tracks):
        old_tracks = [
            {"discnumber": 1, "tracknumber": index, **track}
            for index, track in enumerate(old_tracks, 1)
        ]
        new_tracks = [
            {"discnumber": 1, "tracknumber": index, **track}
            for index, track in enumerate(new_tracks, 1)
        ]
        monkeypatch.setattr(
            proc,
            "read_album_dir",
            lambda f, walk_errors=None: (
                old_tracks if f == backup else new_tracks
            ),
        )
        return proc._upgrade_replacement_verified(
            {"id": "x"}, post, backup)

    stereo = {"channels": 2, "length": 100.0}
    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 192000, **stereo}],
        [{"title": "T1", "bits": 24, "sample_rate": 96000, **stereo}],
    ) is False
    assert verifies(
        [{"title": "T1", "bits": 24, "sample_rate": 96000,
          "channels": 6, "length": 100.0}],
        [{"title": "T1", "bits": 24, "sample_rate": 96000, **stereo}],
    ) is False
    assert verifies(
        [
            {"title": "T1", "bits": 16, "sample_rate": 44100, **stereo},
            {"title": "T2", "bits": 16, "sample_rate": 44100, **stereo},
        ],
        [
            {"title": "T1", "bits": 24, "sample_rate": 96000,
             "channels": 2, "length": 50.0},
            {"title": "T2", "bits": 24, "sample_rate": 96000,
             "channels": 2, "length": 150.0},
        ],
    ) is False
    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1", "bits": 0, "sample_rate": 96000, **stereo}],
    ) is False
    assert verifies(
        [{"title": "T1", "isrc": "USAAA1234567", "bits": 16,
          "sample_rate": 44100, **stereo}],
        [{"title": "T1", "isrc": "USBBB1234568", "bits": 24,
          "sample_rate": 96000, **stereo}],
    ) is False

    identities = {
        "isrc": "USAAA1234567",
        "mb_trackid": "123456781234123412341234567890ab",
    }
    for identity_field, identity in identities.items():
        assert verifies(
            [{"title": "T1", identity_field: identity,
              "bits": 16, "sample_rate": 44100, **stereo}],
            [{"title": "T1", identity_field: "",
              "bits": 24, "sample_rate": 96000, **stereo}],
        ) is False
        assert verifies(
            [{"title": "T1", identity_field: "N/A",
              "bits": 16, "sample_rate": 44100, **stereo}],
            [{"title": "T1", identity_field: "N/A",
              "bits": 24, "sample_rate": 96000, **stereo}],
        ) is False

    for identity_field, placeholder in (
        ("isrc", "000000000000"),
        ("mb_trackid", "00000000-0000-0000-0000-000000000000"),
    ):
        assert verifies(
            [{"title": "Original", identity_field: placeholder,
              "bits": 16, "sample_rate": 44100, **stereo}],
            [{"title": "Different", identity_field: placeholder,
              "bits": 24, "sample_rate": 96000, **stereo}],
        ) is False

    for old_title, new_title in (
        ("東京 (Remaster)", "大阪 (Remaster)"),
        ("東京 [Remaster]", "大阪 [Remaster]"),
    ):
        assert verifies(
            [{"title": old_title, "bits": 16,
              "sample_rate": 44100, **stereo}],
            [{"title": new_title, "bits": 24,
              "sample_rate": 96000, **stereo}],
        ) is False

    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 44100,
          "channels": 2, "length": True}],
        [{"title": "T1", "bits": 24, "sample_rate": 96000,
          "channels": 2, "length": 1.0}],
    ) is False

    assert verifies(
        [{"title": "T1", "isrc": "US-AAA-12-34567",
          "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1", "isrc": "USAAA1234567",
          "bits": 24, "sample_rate": 96000, **stereo}],
    ) is True
    assert verifies(
        [{"title": "T1",
          "mb_trackid": "12345678-1234-1234-1234-1234567890ab",
          "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1",
          "mb_trackid": "123456781234123412341234567890ab",
          "bits": 24, "sample_rate": 96000, **stereo}],
    ) is True

    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1 (Remaster)", "bits": 24,
          "sample_rate": 96000, **stereo}],
    ) is False

    assert verifies(
        [
            {"title": "Same", "bits": 16, "sample_rate": 44100, **stereo},
            {"title": "Same", "bits": 16, "sample_rate": 44100, **stereo},
        ],
        [
            {"title": "Same", "tracknumber": 1,
             "bits": 24, "sample_rate": 96000, **stereo},
            {"title": "Same", "tracknumber": 1,
             "bits": 24, "sample_rate": 96000, **stereo},
        ],
    ) is False


def test_upgrade_verification_keeps_backup_on_a_degraded_walk(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [{"length": 200.0}, {"length": 180.0}, {"length": 240.0}]

    def degraded_read(f, walk_errors=None):
        if f == backup:
            if walk_errors is not None:
                walk_errors.append("disc 2: EIO")
            return original[:1]          # two tracks unreadable this run
        return original[:2]              # the replacement landed short

    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir", degraded_read)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False


def test_gap_fill_partial_import_restores_backup_despite_extra_track(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian import download as dl
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - T1.flac"
    owned.write_bytes(b"the-owned-original")
    (album_dir / "09 - Bonus.flac").write_bytes(b"\x00" * 500)
    existing = [{"path": str(owned), "title": "T1", "tracknumber": 1, "discnumber": 1}]

    staging_root = tmp_path / "staging"
    staging = staging_root / ".qobuz-run-000000000000000000000000"
    staging.mkdir(parents=True)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging_root)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 6)]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    missing = tracks[1:]
    new_flacs = [staging / f"0{i} - T{i}.flac" for i in range(2, 6)]
    for f in new_flacs:
        f.write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (existing, album_dir))
    monkeypatch.setattr(proc, "compute_missing", lambda _q, _e: (missing, [tracks[0]]))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)

    t1_rip = staging / "01 - T1.flac"
    t1_rip.write_bytes(b"\x00" * 1000)

    def fake_import(*_a, **_k):
        assert not owned.exists()
        for f in new_flacs:
            (album_dir / f.name).write_bytes(f.read_bytes())
            f.unlink()
        return True
    monkeypatch.setattr(proc, "beets_import_paths", fake_import)
    monkeypatch.setattr(
        proc,
        "resolve_album_import_placement",
        lambda _album, _token: _adopted_placement(album_dir),
    )
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staging])

    monkeypatch.setattr(dl, "rip_url", lambda *a, **k: (0, ""))
    _patch_download_receipts(monkeypatch, dl, [*new_flacs, t1_rip], staging)
    monkeypatch.setattr(dl, "cleanup_lossy", lambda f: (list(f), [], []))
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    proc.process_album(album, _args(), token="tok")

    assert owned.read_bytes() == b"the-owned-original"


def test_upgrade_verification_rejects_a_swap_hidden_by_ranking(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "Alpha", "length": 100.0, "bits": 16, "sample_rate": 44100},
        {"title": "Beta", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    replacement = [
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "Beta", "length": 100.0, "bits": 16, "sample_rate": 44100},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False


def test_upgrade_verification_requires_every_original_identity(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "Beta", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    replacement = [
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(
        proc, "read_album_dir",
        lambda f, walk_errors=None: original if f == backup else replacement,
    )

    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False


def test_upgrade_verification_counts_an_unreadable_track_as_a_downgrade(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "T1", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "T2", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    replacement = [
        {"title": "T1", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "T2", "length": 100.0, "bits": 0, "sample_rate": 0},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False
