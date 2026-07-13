"""Library migration: placement correctness and the copy-safety guarantees."""
import csv
import json
from contextlib import contextmanager
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

def test_destination_matches_beets_layout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    track = source / "x.flac"
    track.write_bytes(b"audio")
    plan = m.build_plan(
        [(track, _meta(track=4, title="Hey"), "tags")],
        tmp_path / "dest",
    )
    assert plan.placed[0].dest_rel == Path("Artist/Album (2017)/04 - Hey.flac")


# ── classification ─────────────────────────────────────────────────────────────

def test_missing_artist_or_album_is_unplaceable_not_guessed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = source / "a.flac"
    second = source / "b.flac"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    plan = m.build_plan([
        (first, _meta(albumartist=""), "tags"),
        (second, None, ""),
    ], tmp_path / "dest")
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

def _placed_plan(tmp_path, n=1, *, existing_destination_root=False,
                 existing_destination_parents=False, companion_bytes=0):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    items = []
    for i in range(n):
        p = src_dir / f"track{i}.flac"
        p.write_bytes(b"audio-bytes-%d" % i)
        items.append((p, _meta(title=f"Song {i}", track=i + 1), "tags"))
    if companion_bytes:
        (src_dir / "booklet.pdf").write_bytes(b"x" * companion_bytes)
    destination = tmp_path / "dest"
    if existing_destination_parents:
        (destination / "Artist" / "Album (2017)").mkdir(parents=True)
    elif existing_destination_root:
        destination.mkdir()
    return m.build_plan(items, destination)


def test_copy_mode_leaves_originals_untouched(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    src.chmod(0o640)
    m.os.setxattr(src, "user.qobuz-migration-test", b"preserved")
    m.os.utime(
        src,
        ns=(1_600_000_000_000_000_000, 1_600_000_010_000_000_000),
    )
    source_times = src.stat()
    plan = m.build_plan(
        [(src, _meta(title="Song 0", track=1), "tags")],
        plan.dest_root,
    )
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 1 and res.failed == 0
    assert src.exists()                                   # original sacred
    dst = plan.dest_root / plan.placed[0].dest_rel
    assert dst.stat().st_mode & 0o777 == 0o640
    assert m.os.getxattr(dst, "user.qobuz-migration-test") == b"preserved"
    assert dst.stat().st_atime_ns == source_times.st_atime_ns
    assert dst.stat().st_mtime_ns == source_times.st_mtime_ns
    assert dst.read_bytes() == src.read_bytes()
    assert not dst.with_name(dst.name + ".partial").exists()


def test_in_place_mode_moves_only_after_verified_copy(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    res = m.execute_plan(plan, in_place=True)
    assert res.copied == 1
    assert not src.exists()                               # moved
    assert (plan.dest_root / plan.placed[0].dest_rel).exists()


def test_in_place_prunes_only_sealed_empty_source_parents(tmp_path):
    source_root = tmp_path / "src"
    album = source_root / "Loose" / "Old Album"
    album.mkdir(parents=True)
    source = album / "track.flac"
    source.write_bytes(b"audio")
    (source_root / "Loose" / "keep.txt").write_text("keep")
    items = m.CollectedItems(
        source_root, m._capture_root_receipt(source_root))
    items.append((source, _meta(), "tags"))
    plan = m.build_plan(items, tmp_path / "dest")

    result = m.execute_plan(plan, in_place=True)

    assert result.pruned == 1
    assert not album.exists()
    assert (source_root / "Loose" / "keep.txt").read_text() == "keep"


def test_execute_never_overwrites_a_destination_that_wins_publish_race(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    dst = plan.dest_root / plan.placed[0].dest_rel
    rename_noreplace = m._rename_noreplace_at
    raced = False

    def publish_racer(source_fd, source_name, destination_fd, destination_name):
        nonlocal raced
        if source_name.startswith(".qobuz-migrate-copy-") and not raced:
            raced = True
            descriptor = m.os.open(
                destination_name,
                m.os.O_WRONLY | m.os.O_CREAT | m.os.O_EXCL,
                0o600,
                dir_fd=destination_fd,
            )
            m.os.write(descriptor, b"precious")
            m.os.close(descriptor)
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(m, "_rename_noreplace_at", publish_racer)
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 0 and res.skipped == 1
    assert dst.read_bytes() == b"precious"                # not clobbered
    assert plan.placed[0].source.exists()


def test_execute_refuses_a_source_replacement_after_preview(tmp_path):
    plan = _placed_plan(tmp_path)
    source = plan.placed[0].source
    reviewed = source.with_name("reviewed.flac")
    source.rename(reviewed)
    source.write_bytes(b"unrelated replacement")

    result = m.execute_plan(plan, in_place=True)

    destination = plan.dest_root / plan.placed[0].dest_rel
    assert result.failed == 1
    assert not destination.exists()
    assert source.read_bytes() == b"unrelated replacement"
    assert reviewed.read_bytes().startswith(b"audio-bytes-")


def test_source_replacement_at_retirement_is_restored_not_moved(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    source = plan.placed[0].source
    displaced = source.with_name("reviewed-original.flac")
    rename_noreplace = m._rename_noreplace_at
    raced = False

    def replace_before_retirement(
            source_fd, source_name, destination_fd, destination_name):
        nonlocal raced
        if source_name == source.name and destination_name == "held" and not raced:
            raced = True
            source.rename(displaced)
            source.write_bytes(b"unrelated replacement")
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(m, "_rename_noreplace_at", replace_before_retirement)

    result = m.execute_plan(plan, in_place=True)

    assert raced
    assert result.failed == 1
    assert source.read_bytes() == b"unrelated replacement"
    assert displaced.read_bytes().startswith(b"audio-bytes-")


def test_execute_refuses_a_destination_parent_symlink_after_preview(tmp_path):
    plan = _placed_plan(tmp_path, existing_destination_root=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (plan.dest_root / "Artist").symlink_to(outside, target_is_directory=True)

    result = m.execute_plan(plan, in_place=False)

    assert result.failed == 1
    assert not list(outside.rglob("*.flac"))
    assert plan.placed[0].source.exists()


def test_scan_refuses_files_on_a_nested_source_mount(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "track.flac").write_bytes(b"audio")
    root_receipt = m._capture_root_receipt(source_root)
    root_mount_id = root_receipt["existing"][-1]["identity"][6]

    monkeypatch.setattr(
        m, "_descriptor_mount_id", lambda _descriptor: root_mount_id + 1)

    with pytest.raises(OSError, match="nested filesystem mount"):
        m.collect_items(source_root)


def test_publish_interrupt_keeps_the_exact_source_and_reconciles_destination(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    destination = plan.dest_root / plan.placed[0].dest_rel
    rename_noreplace = m._rename_noreplace_at

    def interrupt_after_publish(
            source_fd, source_name, destination_fd, destination_name):
        rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)
        if source_name.startswith(".qobuz-migrate-copy-"):
            raise KeyboardInterrupt

    monkeypatch.setattr(m, "_rename_noreplace_at", interrupt_after_publish)

    result = m.execute_plan(plan, in_place=True)

    assert result.cancelled and result.failed == 1
    assert plan.placed[0].source.read_bytes().startswith(b"audio-bytes-")
    assert not destination.exists()
    assert result.recoveries == []
    assert not list(destination.parent.glob(".qobuz-migrate-copy-*"))

    monkeypatch.setattr(m, "_rename_noreplace_at", rename_noreplace)
    artifact = m.write_results_manifest(result, plan=plan)
    with Path(artifact["path"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    summary = next(row for row in rows if row["record"] == "summary")
    counters = json.loads(summary["reason"])
    assert summary["status"] == "cancelled"
    assert counters["cancelled"] is True
    assert counters["failed"] == 1
    assert counters["pruned"] == 0


def test_fatal_track_error_keeps_a_publishable_partial_result(
        tmp_path, monkeypatch):
    plan = _placed_plan(
        tmp_path, n=2, existing_destination_parents=True)
    stream_copy = m._stream_copy_noreplace
    opened_close = m._OpenedFile.close
    calls = 0
    fatal_started = False

    class SourceTeardownFailure(OSError):
        pass

    def stop_on_second_track(*args, **kwargs):
        nonlocal calls, fatal_started
        calls += 1
        if calls == 2:
            fatal_started = True
            raise SystemExit("simulated fatal migration boundary")
        return stream_copy(*args, **kwargs)

    def fail_source_teardown_after_fatal(opened):
        opened_close(opened)
        if fatal_started:
            raise SourceTeardownFailure("source lease teardown failed")

    monkeypatch.setattr(m, "_stream_copy_noreplace", stop_on_second_track)
    monkeypatch.setattr(m._OpenedFile, "close", fail_source_teardown_after_fatal)

    with pytest.raises(m.MigrationExecutionAbort) as stopped:
        m.execute_plan(plan, in_place=True)

    abort = stopped.value
    assert isinstance(abort.cause, SystemExit)
    result = abort.result
    assert result.cancelled
    assert result.copied == 1
    assert result.failed == 1
    assert [outcome[2] for outcome in result.outcomes] == [
        m.COPIED,
        m.FAILED,
    ]
    assert result.cleanup_failures == [{
        "boundary": "track source teardown",
        "error": "SourceTeardownFailure: source lease teardown failed",
    }]
    assert not plan.placed[0].source.exists()
    assert plan.placed[1].source.exists()

    artifact = m.write_results_manifest(result, plan=plan)
    with Path(artifact["path"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    summary = next(row for row in rows if row["record"] == "summary")
    tracks = [row for row in rows if row["record"] == "track"]
    cleanup = [row for row in rows if row["record"] == "cleanup"]
    assert summary["status"] == "cancelled"
    assert [row["status"] for row in tracks] == [m.COPIED, m.FAILED]
    assert [row["state"] for row in cleanup] == ["track source teardown"]

    with pytest.raises(SystemExit, match="simulated fatal migration boundary"):
        abort.reraise()


def test_writer_teardown_cannot_mask_a_fatal_partial_result(monkeypatch):
    from qobuz_librarian import file_exclusion

    class FatalTrack(BaseException):
        pass

    class TeardownFailure(BaseException):
        pass

    result = m.ExecResult(copied=1, cancelled=True)
    result.outcomes.append((Path("source.flac"), Path("dest.flac"), m.COPIED, ""))
    cause = FatalTrack("fatal track boundary")

    @contextmanager
    def failing_scope():
        try:
            yield True
        finally:
            raise TeardownFailure("writer scope did not close cleanly")

    def fatal_execution(*_args, **_kwargs):
        raise m.MigrationExecutionAbort(result, cause) from cause

    monkeypatch.setattr(
        file_exclusion, "inode_write_exclusion_scope", failing_scope)
    monkeypatch.setattr(m, "_execute_plan", fatal_execution)

    with pytest.raises(m.MigrationExecutionAbort) as stopped:
        m.execute_plan(object())

    abort = stopped.value
    assert abort.result is result
    assert abort.cause is cause
    assert result.cleanup_failures == [{
        "boundary": "writer-protection teardown",
        "error": "TeardownFailure: writer scope did not close cleanly",
    }]
    assert "writer-protection teardown" in "\n".join(cause.__notes__)


def test_publish_rollback_retains_a_destination_when_exclusion_is_refused(
        tmp_path, monkeypatch):
    from qobuz_librarian import file_exclusion

    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    source = plan.placed[0].source
    destination = plan.dest_root / plan.placed[0].dest_rel
    rename_noreplace = m._rename_noreplace_at
    acquire_exclusion = file_exclusion.acquire_inode_write_exclusion
    writer_took_over = False

    def interrupt_after_publish(
            source_fd, source_name, destination_fd, destination_name):
        rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)
        if source_name.startswith(".qobuz-migrate-copy-"):
            raise KeyboardInterrupt

    def refuse_destination_exclusion(descriptor):
        nonlocal writer_took_over
        if Path(m.os.readlink(f"/proc/self/fd/{descriptor}")) == destination:
            with destination.open("wb") as handle:
                handle.write(b"writer-owned bytes")
            writer_took_over = True
            return None
        return acquire_exclusion(descriptor)

    monkeypatch.setattr(m, "_rename_noreplace_at", interrupt_after_publish)
    monkeypatch.setattr(
        file_exclusion,
        "acquire_inode_write_exclusion",
        refuse_destination_exclusion,
    )

    result = m.execute_plan(plan, in_place=True)

    assert writer_took_over and result.cancelled
    assert source.exists()
    assert destination.read_bytes() == b"writer-owned bytes"
    assert result.recoveries[0]["state"] == "duplicate-publication"


def test_publish_rollback_restores_a_late_destination_replacement(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    destination = plan.dest_root / plan.placed[0].dest_rel
    displaced = tmp_path / "displaced-publication.flac"
    rename_noreplace = m._rename_noreplace_at
    unlink = m.os.unlink
    replaced = False

    def refuse_source_retirement(*args, **kwargs):
        return False

    def install_replacement():
        nonlocal replaced
        if replaced:
            return
        replaced = True
        destination.rename(displaced)
        destination.write_bytes(b"late user replacement")

    def replace_before_cleanup(
            source_fd, source_name, destination_fd, destination_name):
        if destination_name == "held" and source_name == destination.name:
            install_replacement()
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    def replace_before_unlink(name, *args, **kwargs):
        if m.os.fsdecode(name) == destination.name:
            install_replacement()
        return unlink(name, *args, **kwargs)

    monkeypatch.setattr(m, "_remove_sealed_source", refuse_source_retirement)
    monkeypatch.setattr(m, "_rename_noreplace_at", replace_before_cleanup)
    monkeypatch.setattr(m.os, "unlink", replace_before_unlink)

    result = m.execute_plan(plan, in_place=True)

    assert replaced and result.failed == 1
    assert destination.read_bytes() == b"late user replacement"
    assert displaced.read_bytes().startswith(b"audio-bytes-")
    assert not list(destination.parent.glob(".qobuz-migrate-publication-*"))


def test_publish_rollback_reports_a_first_replacement_blocked_by_a_second(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    destination = plan.dest_root / plan.placed[0].dest_rel
    displaced = tmp_path / "displaced-publication.flac"
    rename_noreplace = m._rename_noreplace_at
    first_installed = False
    second_installed = False

    def refuse_source_retirement(*args, **kwargs):
        return False

    def race_cleanup(
            source_fd, source_name, destination_fd, destination_name):
        nonlocal first_installed, second_installed
        if (
            destination_name == "held"
            and source_name == destination.name
            and not first_installed
        ):
            first_installed = True
            destination.rename(displaced)
            destination.write_bytes(b"first user arrival")
        elif (
            source_name == "held"
            and destination_name == destination.name
            and first_installed
            and not second_installed
        ):
            second_installed = True
            destination.write_bytes(b"second user arrival")
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(m, "_remove_sealed_source", refuse_source_retirement)
    monkeypatch.setattr(m, "_rename_noreplace_at", race_cleanup)

    result = m.execute_plan(plan, in_place=True)

    assert first_installed and second_installed
    assert result.failed == 1
    assert destination.read_bytes() == b"second user arrival"
    assert displaced.read_bytes().startswith(b"audio-bytes-")
    first_recoveries = [
        Path(record["location"])
        for record in result.recoveries
        if record["state"] == "replacement-guarded"
    ]
    assert len(first_recoveries) == 1
    assert first_recoveries[0].read_bytes() == b"first user arrival"


def test_source_retirement_interrupt_restores_the_exact_source(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_root=True)
    source = plan.placed[0].source
    destination = plan.dest_root / plan.placed[0].dest_rel
    unlink = m.os.unlink
    interrupted = False

    def interrupt_after_unlink(name, *args, **kwargs):
        nonlocal interrupted
        unlink(name, *args, **kwargs)
        if name == "held" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(m.os, "unlink", interrupt_after_unlink)

    result = m.execute_plan(plan, in_place=True)

    assert result.cancelled and result.failed == 1
    assert source.read_bytes().startswith(b"audio-bytes-")
    assert not destination.exists()
    assert result.recoveries == []
    assert not list(source.parent.glob(".qobuz-migrate-source-*"))


def test_destination_unlink_at_source_commit_is_restored_from_exact_guard(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    source = plan.placed[0].source
    destination = plan.dest_root / plan.placed[0].dest_rel
    remove_sealed_source = m._remove_sealed_source
    raced = False

    def retire_with_unlink_race(
            opened_source, receipt, destination_intact=None,
            recoveries=None):
        checks = 0

        def unlink_after_final_proof():
            nonlocal checks, raced
            checks += 1
            exact = destination_intact()
            if checks == 4 and exact:
                destination.unlink()
                raced = True
            return exact

        return remove_sealed_source(
            opened_source, receipt, unlink_after_final_proof, recoveries)

    monkeypatch.setattr(m, "_remove_sealed_source", retire_with_unlink_race)

    result = m.execute_plan(plan, in_place=True)

    assert raced
    assert result.copied == 1 and result.failed == 0
    assert not source.exists()
    assert destination.read_bytes().startswith(b"audio-bytes-")
    assert not list(destination.parent.glob(".qobuz-migrate-destination-*"))


def test_destination_guard_interrupt_returns_recovery_and_stops(
        tmp_path, monkeypatch):
    plan = _placed_plan(
        tmp_path, n=2, existing_destination_parents=True)
    sources = [entry.source for entry in plan.placed]
    unlink = m.os.unlink
    interrupted = False

    def interrupt_after_guard_unlink(name, *args, **kwargs):
        nonlocal interrupted
        unlink(name, *args, **kwargs)
        descriptor = kwargs.get("dir_fd")
        if (
            name == "anchor"
            and descriptor is not None
            and ".qobuz-migrate-destination-" in m.os.readlink(
                f"/proc/self/fd/{descriptor}")
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(m.os, "unlink", interrupt_after_guard_unlink)

    result = m.execute_plan(plan, in_place=True)

    assert interrupted and result.cancelled
    assert result.failed == 1 and len(result.outcomes) == 1
    assert not sources[0].exists() and sources[1].exists()
    assert len(result.recoveries) == 1
    recovery = Path(result.recoveries[0]["location"])
    assert recovery.read_bytes() == b"audio-bytes-0"


def test_parent_replacement_after_publish_cannot_retire_the_source(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    destination = plan.dest_root / plan.placed[0].dest_rel
    displaced = destination.parent.with_name("displaced-album")
    fsync_directories = m._fsync_directories
    swapped = False

    def swap_after_commit(*descriptors):
        nonlocal swapped
        fsync_directories(*descriptors)
        if not swapped and destination.exists():
            swapped = True
            destination.parent.rename(displaced)
            destination.parent.mkdir()
            (destination.parent / "unrelated.txt").write_text("keep me")

    monkeypatch.setattr(m, "_fsync_directories", swap_after_commit)

    result = m.execute_plan(plan, in_place=True)

    assert result.failed == 1
    assert plan.placed[0].source.exists()
    assert (destination.parent / "unrelated.txt").read_text() == "keep me"
    assert (displaced / destination.name).read_bytes().startswith(b"audio-bytes-")


def test_resume_carries_companions_only_for_identical_destination_audio(tmp_path):
    source_root = tmp_path / "source"
    matching = source_root / "matching"
    unrelated = source_root / "unrelated"
    matching.mkdir(parents=True)
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
        plan, in_place=True, resume_entries=resume_entries,
        progress=lambda *event: execution_progress.append(event))

    assert matching_audio.exists()
    assert matching_dest.read_bytes() == b"same recording"
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


def test_fatal_resume_recheck_records_the_attempted_track(tmp_path, monkeypatch):
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

    def stop_recheck(*_args, **_kwargs):
        raise SystemExit("fatal existing-copy recheck")

    monkeypatch.setattr(m, "_open_file_receipt", stop_recheck)

    with pytest.raises(m.MigrationExecutionAbort) as stopped:
        m.execute_plan(plan, resume_entries=resume_entries)

    assert isinstance(stopped.value.cause, SystemExit)
    assert stopped.value.result.outcomes == [(
        source,
        Path("Artist/Album (2024)/01 - Song.flac"),
        m.FAILED,
        "fatal existing-copy recheck",
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
    verify_existing = m._verified_existing_audio

    def counted_verify_existing(*args, **kwargs):
        nonlocal comparisons
        comparisons += 1
        return verify_existing(*args, **kwargs)

    monkeypatch.setattr(m, "_verified_existing_audio", counted_verify_existing)
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
    verify_existing = m._verified_existing_audio

    def counted_verify_existing(*args, **kwargs):
        nonlocal comparisons
        comparisons += 1
        return verify_existing(*args, **kwargs)

    monkeypatch.setattr(m, "_verified_existing_audio", counted_verify_existing)
    resume_entries = m.verified_resume_entries(plan)
    assert resume_entries == []

    comparisons = 0
    cli_migrate._print_preview(
        plan, verbose=False, in_place=False, resume_entries=resume_entries)

    assert comparisons == 0


def test_space_estimate_counts_every_durable_copy_and_companion(tmp_path):
    plan = _placed_plan(tmp_path, n=2, companion_bytes=500)
    total = sum(e.source.stat().st_size for e in plan.placed)
    need, free = m.space_estimate(plan, in_place=False)
    assert need == total + 500
    assert free is not None and free > 0
    assert m.space_estimate(plan, in_place=True)[0] == total + 500


def test_collect_items_refuses_an_incomplete_source_walk(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    visible = source / "visible.flac"
    visible.write_bytes(b"audio")

    real_listdir = m.os.listdir
    source_inode = source.stat().st_ino
    source_reads = 0

    def incomplete_walk(descriptor):
        nonlocal source_reads
        if (
            isinstance(descriptor, int)
            and m.os.fstat(descriptor).st_ino == source_inode
        ):
            source_reads += 1
            if source_reads == 2:
                raise OSError("migration subtree EIO")
        return real_listdir(descriptor)

    monkeypatch.setattr(m.os, "listdir", incomplete_walk)

    with pytest.raises(OSError, match="migration subtree EIO"):
        m.collect_items(source)


def test_preview_artifact_refuses_replacement_and_unreviewed_entry(tmp_path):
    plan = _placed_plan(tmp_path, n=2)
    artifact = m.write_manifest(plan)
    assert m.verify_audit_artifact(plan, artifact)

    original = plan.placed[0]
    alternate = Path("Artist/Other (2017)/01 - Other.flac")
    exists, _file_receipt, path_receipt, _reason = m._destination_state(
        plan.dest_root_receipt, plan.dest_root, alternate)
    assert not exists
    unreviewed = m.PlanEntry(
        source=original.source,
        status=m.PLACE,
        dest_rel=alternate,
        source_receipt=original.source_receipt,
        destination_path_receipt=path_receipt,
    )
    selected = m.MigrationPlan(
        dest_root=plan.dest_root,
        entries=[unreviewed],
        source_root=plan.source_root,
        source_root_receipt=plan.source_root_receipt,
        dest_root_receipt=plan.dest_root_receipt,
        destination_name_semantics=plan.destination_name_semantics,
    )
    assert not m.verify_audit_artifact(selected, artifact)

    artifact_path = Path(artifact["path"])
    original_bytes = artifact_path.read_bytes()
    artifact_path.unlink()
    artifact_path.write_bytes(original_bytes)
    assert not m.verify_audit_artifact(plan, artifact)


# ── web flow (scan → review candidates → execute copy) ─────────────────────────

def _sealed_web_choice(files, dest):
    items = [
        (path, _meta(title=path.stem, track=index), "tags")
        for index, path in enumerate(files, 1)
    ]
    plan = m.build_plan(items, dest)
    manifest_artifact = m.write_manifest(plan)
    return [{"payload": {
        "entries": [
            (str(entry.source), str(entry.dest_rel), entry.source_receipt,
             entry.destination_path_receipt)
            for entry in plan.placed
        ],
        "source_root": str(plan.source_root),
        "source_root_receipt": plan.source_root_receipt,
        "dest_root_receipt": plan.dest_root_receipt,
        "destination_name_semantics": plan.destination_name_semantics,
        "manifest_artifact": manifest_artifact,
        "companion_receipts": plan.companion_receipts,
    }}]

def test_execute_migration_copies_selected_and_keeps_originals(tmp_path):
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "src"
    src.mkdir()
    f1, f2 = src / "x.flac", src / "y.flac"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    dest = tmp_path / "dest"
    chosen = _sealed_web_choice((f1, f2), dest)
    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=False)
    copied = sorted((dest / "Artist/Album (2017)").glob("*.flac"))
    assert [path.read_bytes() for path in copied] == [b"one", b"two"]
    assert f1.exists() and f2.exists()             # copy mode: originals intact
    assert "2 files copied" in job.summary
    assert list(dest.glob("migration-results-*.csv"))


@pytest.mark.parametrize("surface", ["cli", "web"])
def test_report_fatality_cannot_mask_the_migration_failure(
        tmp_path, monkeypatch, surface):
    from types import SimpleNamespace

    from qobuz_librarian.modes import migrate as migrate_mode
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    class MigrationFatality(BaseException):
        pass

    class ReportFatality(BaseException):
        pass

    class DisplayFatality(BaseException):
        pass

    source = tmp_path / "src" / "track.flac"
    source.parent.mkdir()
    source.write_bytes(b"audio")
    dest = tmp_path / "dest"
    chosen = _sealed_web_choice((source,), dest)
    result = m.ExecResult(cancelled=True)
    primary = MigrationFatality("primary migration failure")

    def stop_execution(*_args, **_kwargs):
        raise m.MigrationExecutionAbort(result, primary) from primary

    def stop_report(*_args, **_kwargs):
        raise ReportFatality("result publication failed")

    monkeypatch.setattr(m, "execute_plan", stop_execution)
    monkeypatch.setattr(m, "write_results_manifest", stop_report)

    if surface == "web":
        job = jm.Job(title="migration")
        original_push_line = job.push_line

        def fail_after_recording(line):
            original_push_line(line)
            if "partial migration report could not be saved" in line:
                raise DisplayFatality("result warning was interrupted")

        monkeypatch.setattr(job, "push_line", fail_after_recording)

        def invoke():
            flows.execute_migration(job, chosen, str(dest), in_place=False)
    else:
        plan = SimpleNamespace(placed=[object()])
        monkeypatch.setattr(migrate_mode, "HAVE_MUTAGEN", True)
        monkeypatch.setattr(
            migrate_mode, "_resolve_paths", lambda _args: (source.parent, dest))
        monkeypatch.setattr(migrate_mode, "_print_preview", lambda *_a: None)
        monkeypatch.setattr(m, "collect_items", lambda *_a, **_kw: [object()])
        monkeypatch.setattr(m, "build_plan", lambda *_a: plan)
        monkeypatch.setattr(
            m, "verified_resume_entries", lambda *_a, **_kw: [])
        monkeypatch.setattr(
            m, "write_manifest", lambda *_a: {"path": str(dest / "plan.csv")})
        monkeypatch.setattr(m, "space_estimate", lambda *_a, **_kw: (0, 1))
        monkeypatch.setattr(m, "verify_audit_artifact", lambda *_a: True)
        args = SimpleNamespace(
            dry_run=False,
            yes=True,
            verbose=False,
            in_place=False,
            acoustid=False,
        )
        original_log_info = migrate_mode.log.info

        def fail_after_logging(message):
            original_log_info(message)
            if "The migration ran" in str(message):
                raise DisplayFatality("result warning was interrupted")

        monkeypatch.setattr(migrate_mode.log, "info", fail_after_logging)

        def invoke():
            migrate_mode.run_migrate_mode(args)

    with pytest.raises(MigrationFatality) as stopped:
        invoke()

    assert stopped.value is primary
    assert isinstance(primary.__cause__, ReportFatality)
    assert "could not be published" in "\n".join(primary.__notes__)
    if surface == "web":
        assert any("partial migration report could not be saved" in line
                   for line in job.log_lines)


def test_web_execute_refuses_a_legacy_unsealed_preview(tmp_path):
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    source = tmp_path / "source.flac"
    source.write_bytes(b"reviewed bytes")
    destination = tmp_path / "dest"
    chosen = [{"payload": {"entries": [
        (str(source), "Artist/Album (2017)/01 - Track.flac"),
    ]}}]
    job = jm.Job(title="migration")

    flows.execute_migration(job, chosen, str(destination), in_place=True)

    assert job.error and "missing the saved preview details" in job.error
    assert source.read_bytes() == b"reviewed bytes"
    assert not destination.exists()


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
    chosen = _sealed_web_choice((f1,), dest)
    destination = dest / chosen[0]["payload"]["entries"][0][1]
    monkeypatch.setattr(
        engine, "space_estimate",
        lambda plan, in_place, resume_entries=None: (10_000, 10),
    )

    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=True, src=src)
    assert job.error and "free space" in job.error.lower()
    assert not destination.exists()
    assert f1.exists()                                   # nothing was moved

    # The explicit override lets the same short move through.
    job2 = jm.Job(title="mig2")
    flows.execute_migration(job2, chosen, str(dest), in_place=True, src=src,
                            allow_low_space=True)
    assert not job2.error
    assert destination.exists()


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
    monkeypatch.setattr(
        migrate_mode.engine,
        "write_manifest",
        lambda *a, **k: {"path": str(dest / "preview.csv")},
    )
    monkeypatch.setattr(
        migrate_mode.engine, "verify_audit_artifact", lambda *a, **k: True)
    executed = []

    def _fake_execute(*a, **k):
        executed.append(1)
        return SimpleNamespace(copied=1, skipped=0, lingered=0, failed=0,
                               cancelled=False, failures=[], outcomes=[],
                               companion_outcomes=[], recoveries=[])
    monkeypatch.setattr(migrate_mode.engine, "execute_plan", _fake_execute)
    monkeypatch.setattr(
        migrate_mode.engine,
        "write_results_manifest",
        lambda *a, **k: {"path": str(dest / "results.csv")},
    )

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
