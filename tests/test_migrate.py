"""Library migration: placement correctness and the copy-safety guarantees."""
import csv
import ctypes
import json
import stat
from pathlib import Path

import pytest

from qobuz_librarian.library import migrate as m
from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseIdentity,
    publish_release_identity,
    read_release_identity,
)


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


# ── sealed filesystem evidence ───────────────────────────────────────────

_DIRECTORY_IDENTITY_MASK = 0x0001 | 0x0002 | 0x0100 | 0x0800 | 0x1000


def _fake_directory_statx(returned_mask, requested_masks):
    def statx(descriptor, path, flags, requested_mask, result):
        requested_masks.append(requested_mask)
        value = ctypes.cast(result, ctypes.POINTER(m._Statx)).contents
        value.mask = returned_mask
        value.mode = stat.S_IFDIR | 0o755
        value.ino = 42
        value.btime.tv_sec = 1_700_000_000
        value.btime.tv_nsec = 123
        value.dev_major = 8
        value.dev_minor = 1
        value.mnt_id = 99
        return 0

    return statx


def test_statx_directory_identity_accepts_missing_atime(monkeypatch):
    requested_masks = []
    monkeypatch.setattr(
        m,
        "_statx_function",
        lambda: _fake_directory_statx(
            _DIRECTORY_IDENTITY_MASK,
            requested_masks,
        ),
    )

    identity = m._statx_directory_identity(7, b"", m._AT_EMPTY_PATH)

    assert requested_masks == [_DIRECTORY_IDENTITY_MASK]
    assert identity == [
        stat.S_IFDIR,
        m.os.makedev(8, 1),
        42,
        stat.S_IFDIR | 0o755,
        1_700_000_000,
        123,
        99,
    ]


@pytest.mark.parametrize("missing", [m._STATX_BTIME, m._STATX_MNT_ID])
def test_statx_directory_identity_rejects_missing_proof_field(
        monkeypatch, missing):
    monkeypatch.setattr(
        m,
        "_statx_function",
        lambda: _fake_directory_statx(
            _DIRECTORY_IDENTITY_MASK & ~missing,
            [],
        ),
    )

    with pytest.raises(
            OSError,
            match="filesystem cannot prove directory incarnation safely"):
        m._statx_directory_identity(7, b"", m._AT_EMPTY_PATH)


# ── destination path ─────────────────────────────────────────────────────────────────────────

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


def test_migration_enumeration_ignores_appledouble_audio_and_companions(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("track.flac", "._track.flac", "cover.jpg", "._cover.jpg"):
        (source / name).write_bytes(name.encode())
    (source / ".qobuz-librarian-release.json").write_text(
        '{"schema_version":1,"provider":"qobuz","release_id":"123"}'
    )

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
    assert ".qobuz-librarian-release.json" not in [
        receipt["relative"][-1] for receipt in companions
    ]


def test_migration_plan_fallback_does_not_copy_appledouble_companion(
        tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = tmp_path / "source"
    source.mkdir()
    track = source / "track.flac"
    track.write_bytes(b"audio")
    appledouble = source / "._cover.jpg"
    appledouble.write_bytes(b"AppleDouble sidecar")
    destination = tmp_path / "destination"

    source_receipt = {
        "path": str(source),
        "mount_coordinate": {},
    }
    items = m.CollectedItems(source, source_receipt)
    items.append((
        track,
        _meta(),
        "tags",
        {"relative": [track.name]},
    ))

    class Binding:
        path = source

        def __init__(self):
            self.root_fd = m.os.open(
                source, m.os.O_RDONLY | m.os.O_DIRECTORY)

        def close(self):
            m.os.close(self.root_fd)

        def matches_public(self):
            return True

    monkeypatch.setattr(
        m,
        "_capture_root_receipt",
        lambda path, **_kwargs: {
            "path": str(Path(path)),
            "mount_coordinate": {},
        },
    )
    monkeypatch.setattr(m, "_require_separate_root_receipts", lambda *_a: None)
    monkeypatch.setattr(
        m,
        "_probe_destination_name_semantics",
        lambda _path: {
            "case_sensitive": True,
            "normalization_sensitive": True,
        },
    )
    monkeypatch.setattr(m, "_open_root_receipt", lambda *_a, **_k: Binding())
    monkeypatch.setattr(
        m,
        "_destination_state",
        lambda *_a: (
            False,
            None,
            {"relative": ["Artist", "Album (2017)", "01 - Song.flac"]},
            "",
        ),
    )
    monkeypatch.setattr(
        m,
        "_scan_file_from_descriptor",
        lambda _binding, _parent_fd, _parents, relative: (
            source.joinpath(*relative),
            None,
            {"relative": list(relative)},
        ),
    )

    plan = m.build_plan(items, destination)
    entry = plan.placed[0]
    result = m.ExecResult(outcomes=[
        (entry.source, entry.dest_rel, m.COPIED, ""),
    ])

    def copy_companion(_opened, _receipt, _binding, relative):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(appledouble.read_bytes())

    monkeypatch.setattr(
        m,
        "_open_file_receipt",
        lambda *_a: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(m, "_stream_copy_noreplace", copy_companion)
    m._carry_companion_files(
        plan,
        result,
        entry_map={m._plan_entry_key(entry.source, entry.dest_rel): entry},
        source_root_binding=SimpleNamespace(path=source),
        destination_root_binding=object(),
    )

    assert plan.companion_receipts == []
    assert result.companions == 0
    assert not (destination / entry.dest_rel.parent / appledouble.name).exists()


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


def _release_identity_plan(tmp_path, *, tracks=1):
    album = tmp_path / "src" / "Legacy Album"
    album.mkdir(parents=True)
    items = []
    for index in range(tracks):
        track = album / f"track{index}.flac"
        track.write_bytes(f"audio-{index}".encode())
        items.append((
            track,
            _meta(title=f"Song {index}", track=index + 1),
            "tags",
        ))
    (album / "cover.jpg").write_bytes(b"cover")
    publish_release_identity(album, ReleaseIdentity("qobuz", "100"))
    return m.build_plan(items, tmp_path / "dest"), album


def test_release_identity_preview_is_sealed_outside_generic_companions(
        tmp_path):
    plan, _album = _release_identity_plan(tmp_path)

    assert [
        record["identity"]["release_id"]
        for record in plan.release_identities
    ] == ["100"]
    assert all(
        receipt["relative"][-1] != MANIFEST_NAME
        for receipt in plan.companion_receipts
    )
    assert [receipt["relative"][-1]
            for receipt in plan.companion_receipts] == ["cover.jpg"]


def test_supplied_companion_channel_cannot_smuggle_release_manifest(tmp_path):
    initial, _album = _release_identity_plan(tmp_path)
    entry = initial.placed[0]
    items = m.CollectedItems(initial.source_root, initial.source_root_receipt)
    items.append((entry.source, entry.meta, "tags", entry.source_receipt))
    items.companion_receipts = [
        initial.release_identities[0]["source_receipt"],
    ]

    rebuilt = m.build_plan(items, tmp_path / "other-dest")

    assert rebuilt.companion_receipts == []
    assert len(rebuilt.release_identities) == 1


def test_release_identity_publishes_only_after_all_album_audio_succeeds(
        tmp_path, monkeypatch):
    plan, album = _release_identity_plan(tmp_path, tracks=2)
    second_destination = plan.dest_root / plan.placed[1].dest_rel
    original_copy = m._stream_copy_noreplace

    def fail_second(source, receipt, binding, destination, *args):
        if Path(destination) == plan.placed[1].dest_rel:
            raise OSError("second track failed")
        return original_copy(source, receipt, binding, destination, *args)

    monkeypatch.setattr(m, "_stream_copy_noreplace", fail_second)

    result = m.execute_plan(plan)

    destination_album = plan.dest_root / Path("Artist/Album (2017)")
    assert result.copied == 0
    assert result.failed == 2
    assert not (plan.dest_root / plan.placed[0].dest_rel).exists()
    assert not second_destination.exists()
    assert not (destination_album / MANIFEST_NAME).exists()
    assert result.release_identity_outcomes == [(
        album / MANIFEST_NAME,
        Path("Artist/Album (2017)"),
        m.FAILED,
        "not all mapped album audio completed or verified",
    )]


def test_unverified_album_collision_blocks_every_album_track(tmp_path):
    destination_album = tmp_path / "dest" / "Artist" / "Album (2017)"
    destination_album.mkdir(parents=True)
    conflicting = destination_album / "02 - Song 1.flac"
    conflicting.write_bytes(b"different audio")
    plan, source_album = _release_identity_plan(tmp_path, tracks=2)

    resumes = m.verified_resume_entries(plan)
    result = m.execute_plan(plan, resume_entries=resumes)

    assert resumes == []
    assert result.copied == 0 and result.failed == 2
    assert not (destination_album / "01 - Song 0.flac").exists()
    assert conflicting.read_bytes() == b"different audio"
    assert all(path.exists() for path in source_album.glob("*.flac"))
    assert not (destination_album / MANIFEST_NAME).exists()


def test_release_identity_executes_and_resume_is_idempotent(tmp_path):
    plan, album = _release_identity_plan(tmp_path)

    first = m.execute_plan(plan)
    destination_album = plan.dest_root / Path("Artist/Album (2017)")

    assert first.copied == 1 and first.failed == 0
    assert first.companions == 1
    assert read_release_identity(destination_album) == ReleaseIdentity(
        "qobuz", "100")
    assert first.release_identity_outcomes[-1][2] == m.COPIED
    assert (album / MANIFEST_NAME).exists()

    resumed_plan = m.build_plan([
        (album / "track0.flac", _meta(title="Song 0", track=1), "tags"),
    ], plan.dest_root)
    resumes = m.verified_resume_entries(resumed_plan)
    resumed = m.execute_plan(resumed_plan, resume_entries=resumes)

    assert resumed.skipped == 1 and resumed.failed == 0
    assert resumed.release_identity_outcomes[-1][2] == m.SKIPPED
    assert read_release_identity(destination_album).release_id == "100"


def test_release_identity_in_place_retires_audio_after_publication(tmp_path):
    plan, source_album = _release_identity_plan(tmp_path)

    result = m.execute_plan(plan, in_place=True)

    destination_album = plan.dest_root / "Artist" / "Album (2017)"
    assert result.copied == 1 and result.failed == 0
    assert not (source_album / "track0.flac").exists()
    assert read_release_identity(destination_album).release_id == "100"
    assert (source_album / MANIFEST_NAME).exists()


def test_multidisc_source_manifest_is_transferred_from_album_root(tmp_path):
    album = tmp_path / "src" / "Album"
    disc_one = album / "Disc 1"
    disc_two = album / "Disc 2"
    disc_one.mkdir(parents=True)
    disc_two.mkdir()
    first = disc_one / "one.flac"
    second = disc_two / "two.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    publish_release_identity(album, ReleaseIdentity("qobuz", "100"))

    plan = m.build_plan([
        (first, _meta(title="One", track=1, disc=1, disctotal=2), "tags"),
        (second, _meta(title="Two", track=1, disc=2, disctotal=2), "tags"),
    ], tmp_path / "dest")
    result = m.execute_plan(plan)

    assert len(plan.release_identities) == 1
    assert plan.release_identities[0]["source_folder"] == []
    assert sorted(plan.release_identities[0]["mapped_source_folders"]) == [
        ["Disc 1"], ["Disc 2"]]
    assert result.copied == 2 and result.failed == 0
    assert read_release_identity(
        plan.dest_root / "Artist" / "Album (2017)").release_id == "100"


def test_source_manifest_cannot_fan_out_to_distinct_destination_albums(
        tmp_path):
    album = tmp_path / "src" / "Album"
    album.mkdir(parents=True)
    first = album / "one.flac"
    second = album / "two.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    publish_release_identity(album, ReleaseIdentity("qobuz", "100"))

    with pytest.raises(OSError, match="multiple destination albums"):
        m.build_plan([
            (first, _meta(album="First", title="One", track=1), "tags"),
            (second, _meta(album="Second", title="Two", track=1), "tags"),
        ], tmp_path / "dest")

    assert not (tmp_path / "dest").exists()


def test_source_manifest_fan_out_includes_nonresumable_collisions(tmp_path):
    album = tmp_path / "src" / "Album"
    album.mkdir(parents=True)
    first = album / "one.flac"
    second = album / "two.flac"
    duplicate = album / "duplicate.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    duplicate.write_bytes(b"duplicate")
    publish_release_identity(album, ReleaseIdentity("qobuz", "100"))

    with pytest.raises(OSError, match="multiple destination albums"):
        m.build_plan([
            (first, _meta(album="First", title="One", track=1), "tags"),
            (second, _meta(album="Second", title="Same", track=1), "tags"),
            (duplicate, _meta(
                album="Second", title="Same", track=1), "tags"),
        ], tmp_path / "dest")

    assert not (tmp_path / "dest").exists()


@pytest.mark.parametrize("destination_manifest", ["different", "malformed"])
def test_release_identity_conflict_fails_album_before_audio_mutation(
        tmp_path, destination_manifest):
    destination_album = tmp_path / "dest" / "Artist" / "Album (2017)"
    destination_album.mkdir(parents=True)
    if destination_manifest == "different":
        publish_release_identity(
            destination_album, ReleaseIdentity("qobuz", "200"))
    else:
        (destination_album / MANIFEST_NAME).write_text("not json")
    destination_before = (destination_album / MANIFEST_NAME).read_bytes()
    plan, source_album = _release_identity_plan(tmp_path)
    source_before = (
        b'{"schema_version":1,"provider":"qobuz","release_id":"100"}\n'
    )

    result = m.execute_plan(plan)

    assert result.copied == 0 and result.failed == 1
    assert not (plan.dest_root / plan.placed[0].dest_rel).exists()
    assert (source_album / MANIFEST_NAME).read_bytes() == source_before
    assert (destination_album / MANIFEST_NAME).read_bytes() == destination_before
    assert result.release_identity_outcomes[0][2] == m.FAILED
    assert "destination release manifest" in result.release_identity_outcomes[0][3]
    artifact = m.write_results_manifest(result, plan=plan)
    with Path(artifact["path"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    identity_row = next(
        row for row in rows if row["record"] == "release_identity")
    assert identity_row["status"] == m.FAILED
    assert "destination release manifest" in identity_row["reason"]


def test_changed_source_release_receipt_fails_album_before_audio_mutation(
        tmp_path):
    plan, source_album = _release_identity_plan(tmp_path)
    manifest = source_album / MANIFEST_NAME
    reviewed = manifest.read_bytes()
    manifest.unlink()
    manifest.write_bytes(reviewed)

    result = m.execute_plan(plan)

    assert result.copied == 0 and result.failed == 1
    assert not (plan.dest_root / plan.placed[0].dest_rel).exists()
    assert manifest.read_bytes() == reviewed
    assert result.release_identity_outcomes[0][2] == m.FAILED
    assert "source release manifest changed" in result.release_identity_outcomes[0][3]


def test_release_identity_is_in_plan_and_result_audit_evidence(tmp_path):
    plan, _album = _release_identity_plan(tmp_path)
    preview = m.write_manifest(plan)
    assert preview["context"]["release_identities"] == plan.release_identities

    result = m.execute_plan(plan)
    artifact = m.write_results_manifest(result, plan=plan)
    with Path(artifact["path"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    identity = next(row for row in rows if row["record"] == "release_identity")
    assert identity["status"] == m.COPIED
    summary = json.loads(next(
        row["reason"] for row in rows if row["record"] == "summary"))
    assert summary["release_identity_attempts"] == 1
    assert summary["release_identity_failures"] == 0
    assert summary["companion_attempts"] == 1
    assert summary["companions_copied"] == 1


def test_release_identity_change_invalidates_preview_artifact(tmp_path):
    plan, _album = _release_identity_plan(tmp_path)
    preview = m.write_manifest(plan)
    plan.release_identities[0]["identity"]["release_id"] = "200"

    assert m.verify_audit_artifact(plan, preview) is False
    assert not (plan.dest_root / plan.placed[0].dest_rel).exists()


def test_missing_release_identity_invalidates_preview_artifact(tmp_path):
    plan, _album = _release_identity_plan(tmp_path)
    preview = m.write_manifest(plan)
    plan.release_identities = []

    assert m.verify_audit_artifact(plan, preview) is False
    assert not (plan.dest_root / plan.placed[0].dest_rel).exists()


@pytest.mark.parametrize("in_place", [False, True])
def test_late_destination_identity_conflict_rolls_back_album_audio(
        tmp_path, monkeypatch, in_place):
    plan, source_album = _release_identity_plan(tmp_path)
    entry = plan.placed[0]
    destination = plan.dest_root / entry.dest_rel
    original_copy = m._stream_copy_noreplace
    raced = False

    def add_conflict_after_audio(source, receipt, binding, target, *args):
        nonlocal raced
        published = original_copy(source, receipt, binding, target, *args)
        if not raced and Path(target) == entry.dest_rel:
            raced = True
            publish_release_identity(
                destination.parent, ReleaseIdentity("qobuz", "200"))
        return published

    monkeypatch.setattr(m, "_stream_copy_noreplace", add_conflict_after_audio)

    result = m.execute_plan(plan, in_place=in_place)

    assert result.copied == 0 and result.failed == 1
    assert not destination.exists()
    assert (source_album / "track0.flac").exists()
    assert read_release_identity(destination.parent).release_id == "200"
    assert result.release_identity_outcomes[-1][2] == m.FAILED


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


def test_execute_refuses_a_destination_parent_symlink_after_preview(tmp_path):
    plan = _placed_plan(tmp_path, existing_destination_root=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (plan.dest_root / "Artist").symlink_to(outside, target_is_directory=True)

    result = m.execute_plan(plan, in_place=False)

    assert result.failed == 1
    assert not list(outside.rglob("*.flac"))
    assert plan.placed[0].source.exists()


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
        "release_identities": plan.release_identities,
    }}]


def _stub_migration_scan(monkeypatch, tmp_path, receipt_marker="receipt"):
    source = tmp_path / "source" / "Album" / "a.flac"
    entry = m.PlanEntry(
        source=source,
        status=m.PLACE,
        dest_rel=Path("Artist/Album (2026)/01 - A.flac"),
        source_receipt={"relative": ["Album", "a.flac"],
                        "marker": receipt_marker},
        destination_path_receipt={"missing": ["Artist", "Album (2026)"],
                                  "marker": receipt_marker},
    )
    plan = m.MigrationPlan(
        dest_root=tmp_path / "dest",
        entries=[entry],
        source_root=tmp_path / "source",
        source_root_receipt={"source": receipt_marker},
        dest_root_receipt={"dest": receipt_marker},
        destination_name_semantics={"case_sensitive": True},
    )
    monkeypatch.setattr(m, "collect_items", lambda *_a, **_k: [object()])
    monkeypatch.setattr(m, "build_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(m, "verified_resume_entries", lambda *_a, **_k: [])
    monkeypatch.setattr(
        m, "write_manifest",
        lambda *_a, **_k: {
            "path": str(tmp_path / "dest" / "manifest.csv"),
            "receipt": {"marker": receipt_marker},
        },
    )
    monkeypatch.setattr(m, "space_estimate", lambda *_a, **_k: (1, 10))
    return plan


def test_compact_migration_scan_payload_is_disk_backed(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows, job_persistence
    from qobuz_librarian.web import jobs as jm

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()
    marker = "full-receipt-" + ("x" * 20_000)
    _stub_migration_scan(monkeypatch, tmp_path, marker)
    job = jm.Job(title="Migration", kind="scan")

    flows.scan_migration(job, tmp_path / "source", tmp_path / "dest",
                         use_acoustid=False)

    assert len(job.candidates) == 1
    candidate = job.candidates[0]
    assert candidate["payload"] == {
        "migration_payload_ref": {"version": 1},
    }
    assert marker not in json.dumps(job.candidates)
    stored = job_persistence.load_migration_candidate_payload(
        job.id, candidate["cid"]
    )
    assert stored["entries"][0][2]["marker"] == marker
    assert stored["manifest_artifact"]["receipt"]["marker"] == marker


def test_migration_scan_payload_write_failure_is_not_actionable(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows, job_persistence
    from qobuz_librarian.web import jobs as jm

    _stub_migration_scan(monkeypatch, tmp_path)
    deleted = []
    monkeypatch.setattr(
        job_persistence, "persist_migration_candidate_payload",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        job_persistence, "delete_migration_payloads", deleted.append,
    )
    job = jm.Job(title="Migration", kind="scan")

    flows.scan_migration(job, tmp_path / "source", tmp_path / "dest",
                         use_acoustid=False)

    assert job.candidates == []
    assert job.error and "saved safely" in job.error
    assert job.summary == job.error
    assert deleted == [job.id]

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


def test_referenced_migration_payload_executes_after_persistence_reload(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows, job_persistence
    from qobuz_librarian.web import jobs as jm

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()
    src = tmp_path / "src"
    src.mkdir()
    source = src / "x.flac"
    source.write_bytes(b"one")
    dest = tmp_path / "dest"
    inline = _sealed_web_choice((source,), dest)[0]["payload"]
    inline["resume_entries"] = []
    job = jm.Job(title="Migration", kind="scan")
    assert job_persistence.persist_migration_candidate_payload(
        job.id, "c0", inline
    )
    chosen = [{
        "cid": "c0", "kind": "migrate", "title": "Album",
        "artist": "Artist", "detail": "1 track", "selected": True,
        "payload": job_persistence.migration_payload_reference(),
    }]
    job_persistence._conn.close()
    job_persistence._conn = None
    job_persistence.init()

    flows.execute_migration(job, chosen, str(dest), in_place=False)

    copied = list((dest / "Artist/Album (2017)").glob("*.flac"))
    assert [path.read_bytes() for path in copied] == [b"one"]
    assert source.exists()
    assert not job.error


def test_referenced_migration_payload_preserves_release_identity(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows, job_persistence
    from qobuz_librarian.web import jobs as jm

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()
    album = tmp_path / "src" / "Album"
    album.mkdir(parents=True)
    source = album / "x.flac"
    source.write_bytes(b"one")
    publish_release_identity(album, ReleaseIdentity("qobuz", "100"))
    dest = tmp_path / "dest"
    inline = _sealed_web_choice((source,), dest)[0]["payload"]
    inline["resume_entries"] = []
    job = jm.Job(title="Migration", kind="scan")
    assert job_persistence.persist_migration_candidate_payload(
        job.id, "c0", inline)
    chosen = [{
        "cid": "c0", "kind": "migrate", "title": "Album",
        "artist": "Artist", "detail": "1 track", "selected": True,
        "payload": job_persistence.migration_payload_reference(),
    }]

    flows.execute_migration(job, chosen, str(dest), in_place=False)

    assert read_release_identity(
        dest / "Artist" / "Album (2017)") == ReleaseIdentity("qobuz", "100")
    assert not job.error


def test_missing_persisted_release_identity_row_stops_before_audio(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows, job_persistence
    from qobuz_librarian.web import jobs as jm

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()
    album = tmp_path / "src" / "Album"
    album.mkdir(parents=True)
    source = album / "x.flac"
    source.write_bytes(b"one")
    publish_release_identity(album, ReleaseIdentity("qobuz", "100"))
    dest = tmp_path / "dest"
    inline = _sealed_web_choice((source,), dest)[0]["payload"]
    inline["resume_entries"] = []
    job = jm.Job(title="Migration", kind="scan")
    assert job_persistence.persist_migration_candidate_payload(
        job.id, "c0", inline)
    conn = job_persistence._get_conn()
    conn.execute(
        "DELETE FROM migration_candidate_entries "
        "WHERE job_id=? AND candidate_id=? AND entry_kind='release_identity'",
        (job.id, "c0"),
    )
    conn.commit()
    chosen = [{
        "cid": "c0", "kind": "migrate", "title": "Album",
        "artist": "Artist", "detail": "1 track", "selected": True,
        "payload": job_persistence.migration_payload_reference(),
    }]

    flows.execute_migration(job, chosen, str(dest), in_place=False)

    assert job.error and "no longer matches" in job.error
    assert not (dest / "Artist" / "Album (2017)" / "01 - x.flac").exists()
    assert source.exists()


@pytest.mark.parametrize("chosen", [
    [{"cid": "missing", "payload": {
        "migration_payload_ref": {"version": 1}}}],
    [{"cid": "future", "payload": {
        "migration_payload_ref": {"version": 2}}}],
    [
        {"cid": "c0", "payload": {
            "migration_payload_ref": {"version": 1}}},
        {"cid": "legacy", "payload": {"entries": [],
                                         "manifest_artifact": {}}},
    ],
])
def test_referenced_migration_payload_failure_stops_before_mutation(
        tmp_path, monkeypatch, chosen):
    from qobuz_librarian.library import migrate as engine
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    touched = []
    monkeypatch.setattr(
        engine, "verify_audit_artifact",
        lambda *_a, **_k: touched.append(True),
    )
    job = jm.Job(title="Migration", kind="scan")

    flows.execute_migration(job, chosen, str(tmp_path / "dest"),
                            in_place=False)

    assert job.error and "saved preview details" in job.error
    assert touched == []
    assert not (tmp_path / "dest").exists()


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
