"""Library migration: placement correctness and the copy-safety guarantees."""
from pathlib import Path

import pytest

from qobuz_librarian.library import migrate as m


def _meta(**kw):
    base = {
        "albumartist": "Artist", "album": "Album", "title": "Song",
        "track": 1, "disc": 1, "disctotal": 0, "year": 2017,
        "compilation": False, "ext": ".flac",
    }
    base.update(kw)
    return base


# ── tag normalization ────────────────────────────────────────────────────────

def test_normalize_tags_parses_slashed_track_and_disc_and_year():
    meta = m.normalize_tags(
        {"albumartist": ["A"], "album": ["B"], "title": ["T"],
         "tracknumber": ["3/12"], "discnumber": ["2/2"], "date": ["2008-05-01"]},
        stem="03 T", ext=".flac")
    assert meta["track"] == 3
    assert meta["disc"] == 2
    assert meta["disctotal"] == 2
    assert meta["year"] == 2008


# ── destination path ──────────────────────────────────────────────────────────

def test_destination_matches_beets_layout():
    plan = m.build_plan([(Path("/src/x.flac"), _meta(track=4, title="Hey"), "tags")],
                        Path("/dest"))
    assert plan.placed[0].dest_rel == Path("Artist/Album (2017)/04 - Hey.flac")


# ── classification ─────────────────────────────────────────────────────────────

def test_missing_artist_or_album_is_unplaceable_not_guessed():
    plan = m.build_plan([
        (Path("/src/a.flac"), _meta(albumartist=""), "tags"),
        (Path("/src/b.flac"), None, ""),
    ], Path("/dest"))
    assert len(plan.unplaceable) == 2
    assert not plan.placed


# ── AcoustID match selection ──────────────────────────────────────────────────

def test_acoustid_rejects_low_confidence_and_ambiguous_matches():
    assert m.choose_acoustid_match([{"score": 0.5, "artist": "A"}]) is None
    assert m.choose_acoustid_match([
        {"score": 0.95, "artist": "Oasis"},
        {"score": 0.93, "artist": "Blur"},
    ]) is None
    chosen = m.choose_acoustid_match([{"score": 0.97, "artist": "Oasis", "title": "T"}])
    assert chosen["artist"] == "Oasis"


# ── safe copy / execution ──────────────────────────────────────────────────────

def _placed_plan(tmp_path, n=1):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    items = []
    for i in range(n):
        p = src_dir / f"track{i}.flac"
        p.write_bytes(b"audio-bytes-%d" % i)
        items.append((p, _meta(title=f"Song {i}", track=i + 1), "tags"))
    return m.build_plan(items, tmp_path / "dest")


def test_copy_mode_leaves_originals_untouched(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 1 and res.failed == 0
    assert src.exists()                                   # original sacred
    dst = plan.dest_root / plan.placed[0].dest_rel
    assert dst.read_bytes() == src.read_bytes()
    assert not dst.with_name(dst.name + ".partial").exists()


def test_in_place_mode_moves_only_after_verified_copy(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    res = m.execute_plan(plan, in_place=True)
    assert res.copied == 1
    assert not src.exists()                               # moved
    assert (plan.dest_root / plan.placed[0].dest_rel).exists()


def test_move_tree_relocates_whole_folder_atomically(tmp_path):
    src = tmp_path / "src_album"
    (src / "Disc 1").mkdir(parents=True)
    (src / "01 - a.flac").write_bytes(b"AAA")
    (src / "Disc 1" / "02 - b.flac").write_bytes(b"BBB")
    (src / "cover.jpg").write_bytes(b"IMG")
    dst = tmp_path / "primary" / "src_album"
    assert m.move_tree(src, dst) is True
    assert not src.exists()
    assert (dst / "01 - a.flac").read_bytes() == b"AAA"
    assert (dst / "Disc 1" / "02 - b.flac").read_bytes() == b"BBB"
    assert (dst / "cover.jpg").read_bytes() == b"IMG"


def test_move_tree_cross_filesystem_verifies_before_deleting_source(tmp_path, monkeypatch):
    # Force the cross-filesystem path: os.rename raises, so the move must fall
    # back to a per-file copy-verify-replace and only then remove each source —
    # nothing deleted before a proven copy exists at the destination.
    src = tmp_path / "src_album"
    (src / "Disc 1").mkdir(parents=True)
    (src / "01 - a.flac").write_bytes(b"A" * 1000)
    (src / "Disc 1" / "02 - b.flac").write_bytes(b"B" * 2000)
    dst = tmp_path / "primary" / "src_album"

    def _no_rename(*a, **k):
        raise OSError("simulated cross-device link")
    monkeypatch.setattr(m.os, "rename", _no_rename)

    assert m.move_tree(src, dst) is True
    assert not src.exists()
    assert (dst / "01 - a.flac").read_bytes() == b"A" * 1000
    assert (dst / "Disc 1" / "02 - b.flac").read_bytes() == b"B" * 2000
    assert not (dst / "01 - a.flac.partial").exists()


def test_execute_never_overwrites_a_destination_that_appears_late(tmp_path):
    plan = _placed_plan(tmp_path)
    dst = plan.dest_root / plan.placed[0].dest_rel
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"precious")
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 0 and res.skipped == 1
    assert dst.read_bytes() == b"precious"                # not clobbered
    assert plan.placed[0].source.exists()


def test_resume_carries_companions_only_for_identical_destination_audio(tmp_path):
    matching = tmp_path / "matching"
    unrelated = tmp_path / "unrelated"
    matching.mkdir()
    unrelated.mkdir()
    matching_audio = matching / "track.flac"
    unrelated_audio = unrelated / "track.flac"
    matching_audio.write_bytes(b"same recording")
    unrelated_audio.write_bytes(b"source recording")
    (matching / "cover.jpg").write_bytes(b"matching cover")
    (unrelated / "cover.jpg").write_bytes(b"wrong cover")

    dest = tmp_path / "dest"
    matching_dest = dest / "Artist" / "Match (2024)" / "01 - Song.flac"
    unrelated_dest = dest / "Artist" / "Other (2024)" / "01 - Song.flac"
    matching_dest.parent.mkdir(parents=True)
    unrelated_dest.parent.mkdir(parents=True)
    matching_dest.write_bytes(matching_audio.read_bytes())
    unrelated_dest.write_bytes(b"different recording")

    plan = m.build_plan([
        (matching_audio, _meta(album="Match", year=2024), "tags"),
        (unrelated_audio, _meta(album="Other", year=2024), "tags"),
    ], dest)
    assert len(plan.collisions) == 2
    assert all(entry.dest_rel is not None for entry in plan.collisions)

    preview_progress = []
    resume_entries = m.verified_resume_entries(
        plan, progress=lambda *event: preview_progress.append(event))
    assert len(resume_entries) == 1
    assert [event[:3] for event in preview_progress] == [
        ("Checking existing copies", 1, 2),
        ("Checking existing copies", 2, 2),
    ]
    execution_progress = []
    result = m.execute_plan(
        plan, in_place=False, resume_entries=resume_entries,
        progress=lambda *event: execution_progress.append(event))

    assert (matching_dest.parent / "cover.jpg").read_bytes() == b"matching cover"
    assert not (unrelated_dest.parent / "cover.jpg").exists()
    assert result.companions == 1
    assert result.skipped == 1
    assert [event[:3] for event in execution_progress] == [
        ("Rechecking existing copies", 1, 1),
        ("Carrying cover art and sidecars", 0, 0),
    ]


def test_resume_changed_after_preview_is_reported_as_failed(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "track.flac"
    source.write_bytes(b"same recording")

    dest = tmp_path / "dest"
    destination = dest / "Artist" / "Album (2024)" / "01 - Song.flac"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    plan = m.build_plan([
        (source, _meta(album="Album", year=2024), "tags"),
    ], dest)
    resume_entries = m.verified_resume_entries(plan)
    assert len(resume_entries) == 1

    destination.write_bytes(b"changed after preview")
    result = m.execute_plan(plan, resume_entries=resume_entries)

    assert result.failed == 1
    assert result.skipped == 0
    assert result.outcomes == [(
        source,
        Path("Artist/Album (2024)/01 - Song.flac"),
        m.FAILED,
        "existing destination could not be reverified",
    )]


def test_web_scan_stops_during_existing_copy_verification(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    source = tmp_path / "source" / "track.flac"
    source.parent.mkdir()
    source.write_bytes(b"same recording")
    dest = tmp_path / "dest"
    destination = dest / "Artist" / "Album (2024)" / "01 - Song.flac"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    items = [(source, _meta(album="Album", year=2024), "tags")]
    monkeypatch.setattr(m, "collect_items", lambda *_args, **_kwargs: items)

    comparisons = 0
    same_content = m._same_content

    def counted_same_content(*args, **kwargs):
        nonlocal comparisons
        comparisons += 1
        return same_content(*args, **kwargs)

    monkeypatch.setattr(m, "_same_content", counted_same_content)
    job = jm.Job(title="migration")

    def cancel_from_progress(phase, *_args):
        if phase == "Checking existing copies":
            job.cancel_requested = True

    job.push_progress = cancel_from_progress
    flows.scan_migration(
        job, source.parent, dest, use_acoustid=False, in_place=False
    )

    assert comparisons == 0
    assert job.candidates == []
    assert not (dest / "migration-manifest.csv").exists()
    assert job.summary == "Stopped while checking existing copies. Nothing was copied."


def test_execute_stops_during_existing_copy_recheck(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "track.flac"
    source.write_bytes(b"same recording")
    (source_dir / "cover.jpg").write_bytes(b"cover")
    dest = tmp_path / "dest"
    destination = dest / "Artist" / "Album (2024)" / "01 - Song.flac"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    plan = m.build_plan([
        (source, _meta(album="Album", year=2024), "tags"),
    ], dest)
    resume_entries = m.verified_resume_entries(plan)
    cancelling = False

    def progress(phase, *_args):
        nonlocal cancelling
        if phase == "Rechecking existing copies":
            cancelling = True

    result = m.execute_plan(
        plan,
        resume_entries=resume_entries,
        cancel_check=lambda: cancelling,
        progress=progress,
    )

    assert result.cancelled is True
    assert result.skipped == 0
    assert result.companions == 0
    assert not (destination.parent / "cover.jpg").exists()


def test_cli_preview_reuses_an_empty_resume_review(tmp_path, monkeypatch):
    from qobuz_librarian.modes import migrate as cli_migrate

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "track.flac"
    source.write_bytes(b"source audio")
    dest = tmp_path / "dest"
    destination = dest / "Artist" / "Album (2024)" / "01 - Song.flac"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"other audio!")
    plan = m.build_plan([
        (source, _meta(album="Album", year=2024), "tags"),
    ], dest)

    comparisons = 0
    same_content = m._same_content

    def counted_same_content(*args, **kwargs):
        nonlocal comparisons
        comparisons += 1
        return same_content(*args, **kwargs)

    monkeypatch.setattr(m, "_same_content", counted_same_content)
    resume_entries = m.verified_resume_entries(plan)
    assert resume_entries == []

    comparisons = 0
    cli_migrate._print_preview(
        plan, verbose=False, in_place=False, resume_entries=resume_entries)

    assert comparisons == 0


def test_space_estimate_counts_copy_bytes_but_not_same_fs_moves(tmp_path):
    plan = _placed_plan(tmp_path, n=2)
    total = sum(e.source.stat().st_size for e in plan.placed)
    need, free = m.space_estimate(plan, in_place=False)
    assert need == total                            # a copy writes every file
    assert free is not None and free > 0
    # An in-place move within one filesystem is a rename — no bytes written.
    assert m.space_estimate(plan, in_place=True)[0] == 0
    # A same-folder companion (cover art/booklet) is copied into the destination
    # even for a same-fs in-place move, so its bytes belong in the estimate —
    # without this the preview understates a library with large booklets/scans.
    booklet = plan.placed[0].source.parent / "booklet.pdf"
    booklet.write_bytes(b"x" * 500)
    assert m.space_estimate(plan, in_place=False)[0] == total + 500
    assert m.space_estimate(plan, in_place=True)[0] == 500


def test_collect_items_refuses_an_incomplete_source_walk(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    visible = source / "visible.flac"
    visible.write_bytes(b"audio")

    def incomplete_walk(_root, errors=None):
        yield visible
        if errors is not None:
            errors.append(OSError("migration subtree EIO"))

    monkeypatch.setattr(m, "iter_tree_no_symlinks", incomplete_walk)

    with pytest.raises(OSError, match="migration subtree EIO"):
        m.collect_items(source)


# ── web flow (scan → review candidates → execute copy) ─────────────────────────

def test_execute_migration_copies_selected_and_keeps_originals(tmp_path):
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "src"
    src.mkdir()
    f1, f2 = src / "x.flac", src / "y.flac"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    dest = tmp_path / "dest"
    chosen = [{"payload": {"entries": [
        (str(f1), "Artist/Album (2017)/01 - A.flac"),
        (str(f2), "Artist/Album (2017)/02 - B.flac"),
    ]}}]
    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=False)
    assert (dest / "Artist/Album (2017)/01 - A.flac").read_bytes() == b"one"
    assert (dest / "Artist/Album (2017)/02 - B.flac").read_bytes() == b"two"
    assert f1.exists() and f2.exists()             # copy mode: originals intact
    assert "2 files copied" in job.summary
    assert (dest / "migration-results.csv").exists()


def test_execute_migration_blocks_low_space_in_place_move(tmp_path, monkeypatch):
    # An in-place move into a destination that's known to be short on space must
    # be refused before any file is touched — running out mid-move would scatter
    # the library — unless the user passes the deliberate low-space override.
    from qobuz_librarian.library import migrate as engine
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "src"
    src.mkdir()
    f1 = src / "x.flac"
    f1.write_bytes(b"one")
    dest = tmp_path / "dest"
    chosen = [{"payload": {"entries": [(str(f1), "Artist/Album (2017)/01 - A.flac")]}}]
    monkeypatch.setattr(
        engine, "space_estimate",
        lambda plan, in_place, resume_entries=None: (10_000, 10),
    )

    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=True, src=src)
    assert job.error and "free space" in job.error.lower()
    assert not (dest / "Artist/Album (2017)/01 - A.flac").exists()
    assert f1.exists()                                   # nothing was moved

    # The explicit override lets the same short move through.
    job2 = jm.Job(title="mig2")
    flows.execute_migration(job2, chosen, str(dest), in_place=True, src=src,
                            allow_low_space=True)
    assert not job2.error
    assert (dest / "Artist/Album (2017)/01 - A.flac").exists()


def test_fingerprint_lookup_resolves_album_year_and_is_placeable():
    resp = {"results": [{"score": 0.98, "recordings": [{
        "title": "Kong", "artists": [{"name": "Bonobo"}],
        "releasegroups": [
            {"type": "Single", "title": "Kong", "releases": [{"date": {"year": 2009}}]},
            {"type": "Album", "title": "Black Sands", "artists": [{"name": "Bonobo"}],
             "releases": [{"date": {"year": 2011}}, {"date": {"year": 2010}}]},
        ]}]}]}
    meta = m.identify_from_lookup(resp, 0.9, "stem", ".flac")
    assert meta["album"] == "Black Sands"      # Album type preferred over Single
    assert meta["year"] == 2010                # earliest release year
    assert meta["albumartist"] == "Bonobo"
    assert m.is_placeable(meta)                # the whole point: now placeable


def test_run_migrate_gates_on_insufficient_destination_space(tmp_path, monkeypatch):
    # Short on space: an unattended (--yes) run must refuse outright (a partial
    # in-place move scatters the library), and an interactive run needs a typed
    # override — not the casual confirm that could be answered with a stray "y".
    from types import SimpleNamespace
    from unittest.mock import patch

    from qobuz_librarian.modes import migrate as migrate_mode

    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    plan = SimpleNamespace(
        placed=[SimpleNamespace(source=src / "a.flac",
                                dest_rel=Path("Artist/Album/a.flac"))],
        unplaceable=[], collisions=[],
        summary=lambda: {"place": 1, "unplaceable": 0, "collision": 0})

    monkeypatch.setattr(migrate_mode, "_resolve_paths", lambda args: (src, dest))
    monkeypatch.setattr(migrate_mode.engine, "collect_items", lambda *a, **k: [object()])
    monkeypatch.setattr(migrate_mode.engine, "build_plan", lambda items, d: plan)
    monkeypatch.setattr(migrate_mode.engine, "write_manifest", lambda *a, **k: None)
    executed = []

    def _fake_execute(*a, **k):
        executed.append(1)
        return SimpleNamespace(copied=1, skipped=0, lingered=0, failed=0,
                               cancelled=False, failures=[], outcomes=[])
    monkeypatch.setattr(migrate_mode.engine, "execute_plan", _fake_execute)
    monkeypatch.setattr(migrate_mode.engine, "write_results_manifest", lambda *a, **k: None)
    monkeypatch.setattr(migrate_mode.engine, "prune_empty_dirs", lambda *a, **k: 0)

    def _args(**kw):
        base = dict(dry_run=False, yes=False, verbose=False, in_place=True, acoustid=False)
        base.update(kw)
        return SimpleNamespace(**base)

    # Short on space + unattended → refuse, no partial move.
    monkeypatch.setattr(
        migrate_mode.engine, "space_estimate",
        lambda p, in_place, resume_entries=None: (100, 10),
    )
    migrate_mode.run_migrate_mode(_args(yes=True))
    assert executed == []

    # Short + interactive: a casual decline cancels…
    with patch("builtins.input", side_effect=["no"]):
        migrate_mode.run_migrate_mode(_args())
    assert executed == []
    # …only a typed "yes" overrides.
    with patch("builtins.input", side_effect=["yes"]):
        migrate_mode.run_migrate_mode(_args())
    assert executed == [1]

    # Enough space → the normal confirm path still runs.
    executed.clear()
    monkeypatch.setattr(
        migrate_mode.engine, "space_estimate",
        lambda p, in_place, resume_entries=None: (10, 100),
    )
    with patch("builtins.input", side_effect=["y"]):
        migrate_mode.run_migrate_mode(_args())
    assert executed == [1]


def test_place_file_keeps_the_source_when_the_flush_fails(monkeypatch, tmp_path):
    # Cross-filesystem move: the copy verified byte-for-byte, but its bytes
    # couldn't be forced to disk. Deleting the source then would leave the
    # only durable copy nowhere — keep it and let the caller see the move as
    # not clean.
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.library import migrate

    src = tmp_path / "src" / "01 - Track.flac"
    src.parent.mkdir()
    src.write_bytes(b"the-master")
    dst = tmp_path / "dst" / "01 - Track.flac"

    def cross_fs(*_a, **_k):
        raise OSError(18, "Invalid cross-device link")
    monkeypatch.setattr(migrate.os, "rename", cross_fs)
    monkeypatch.setattr(bkmod, "_fsync", lambda _p: False)

    migrate._place_file(src, dst, move=True)

    assert src.exists() and src.read_bytes() == b"the-master"
