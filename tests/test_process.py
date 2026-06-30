"""process_album integration behaviour: a web cancel must skip the import, a
gap-fill that comes back lossy must restore the backed-up originals, and the
staging sweep/preflight guards hold. The download-phase bookkeeping itself lives
in test_download.py, against the shared run_album_download.
"""
from types import SimpleNamespace

import pytest


def _args(**over):
    base = dict(force=False, yes=True, no_import=False, dry_run=False,
                verbose=False, consolidate=False, no_upgrade=False,
                no_downsample=True, migrate_multi_artist=False,
                auto_upgrade=False, prefer_hires=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_sweep_staging_artwork_removes_artwork_dirs(monkeypatch, tmp_path):
    """`beet import` leaves streamrip's __artwork/ cover-image dirs behind."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    artwork = staging / "Artist" / "Album (2020)" / "__artwork"
    artwork.mkdir(parents=True)
    (artwork / "cover-abc.jpg").write_bytes(b"\xff\xd8\xff")
    keep = staging / "Artist" / "Album (2020)" / "12 - Side B.flac"
    keep.write_bytes(b"fLaC")

    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    proc.sweep_staging_artwork()

    assert not artwork.exists()
    assert keep.exists()


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
    # The job was cancelled while the rip ran; process_album sees it after the
    # download returns and must discard rather than import.
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: True)

    def fake_download(**kw):
        kw["result"].update(n_ok=2, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    beets_runs = []
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: beets_runs.append(1) or True)

    result = proc.process_album(album, _args(), token="tok")

    assert result["result"] == "cancelled"
    assert result["imported"] is False
    assert beets_runs == []


def test_treat_as_new_downloads_an_owned_album_as_a_separate_edition(monkeypatch, tmp_path):
    """A normal download of an album the user already owns short-circuits as
    'already complete'. With treat_as_new (the "get this edition too" path) the
    ownership scan is bypassed, so it downloads in full and imports as a brand-new
    album — album_dir stays None, so beets consolidation is off and the new
    edition isn't folded into the owned one's folder."""
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
    # The album IS owned: the original edition resolves with all tracks present.
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (list(tracks), owned_dir))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: owned_dir)
    monkeypatch.setattr(proc, "compute_missing",
                        lambda q, e: ([], list(q)) if e else (list(q), []))
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: ([], 0))
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
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
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: consolidate_seen.append(k.get("consolidate")) or True)

    # Default: owned + complete → skipped, nothing downloaded or imported.
    assert proc.process_album(album, _args(), token="tok")["result"] == "already_complete"
    assert consolidate_seen == []

    # treat_as_new: ownership bypassed → full download + import as a new album,
    # consolidation off so it's never merged into the owned edition's folder.
    result = proc.process_album(album, _args(), token="tok", treat_as_new=True)
    assert result.get("imported") is True
    assert consolidate_seen == [False]


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
    monkeypatch.setattr(proc, "staged_album_dirs_since", lambda _s: [staged_album])
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: ([], 1))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
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
    monkeypatch.setattr(proc, "staged_album_dirs_since", lambda _s: [first_staged])
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: ([], 1))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
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
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: ([], 0))
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "track_signatures_for_album_dirs", lambda _d: [])

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
    """A full-album gap-fill stashes the owned tracks before re-ripping; if a
    re-ripped track comes back lossy, process_album's finally restores them."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian import download as dl
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - T1.flac"
    owned.write_bytes(b"the-owned-original")
    existing = [{"path": str(owned), "title": "T1", "tracknumber": 1, "discnumber": 1}]

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 6)]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    # 4 of 5 missing → full-album re-rip, the path that backs up present tracks.
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
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: ([], 0))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)

    # The download itself runs for real through run_album_download; only its
    # primitives are stubbed. The re-ripped owned track comes back lossy.
    monkeypatch.setattr(dl, "rip_url", lambda *a, **k: (0, ""))
    monkeypatch.setattr(dl, "files_added_since",
                        lambda _s: new_flacs + [staging / "01 - T1.mp3"])
    monkeypatch.setattr(dl, "cleanup_lossy", lambda _f: (new_flacs, ["01 - T1"], []))
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
    original = [{"length": 200.0}, {"length": 180.0}, {"length": 240.0}]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)

    # A track the matcher dropped: the rebuilt folder has fewer files.
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f: original if f == backup else original[:2])
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    # All tracks present but one re-ripped short (decodes, so flac -t passes):
    # total playtime drops below the original.
    truncated = [{"length": 200.0}, {"length": 20.0}, {"length": 240.0}]
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f: original if f == backup else truncated)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    # Can't even locate the import: keep the backup rather than guess.
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    # A complete, full-length rebuild verifies → safe to drop the backup.
    complete = tmp_path / "Complete"
    complete.mkdir()
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: complete)
    monkeypatch.setattr(proc, "read_album_dir", lambda _f: list(original))
    assert proc._upgrade_replacement_verified({"id": "x"}, complete, backup) is True
