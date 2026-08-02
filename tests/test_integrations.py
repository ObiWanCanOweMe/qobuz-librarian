"""Tests for integrations/rip.py, integrations/beets.py, integrations/lyrics.py
— the streamrip/beets seams where most real bugs live."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.lyrics import (
    load_lyric_retry,
    save_lyric_retry,
)
from qobuz_librarian.integrations.rip import (
    _FLAC_TRUNCATION_FLOOR,
    cleanup_lossy,
    cleanup_staging_residue,
    is_flac,
)

# ── shared SQLite transaction boundary ────────────────────────────────


# ── rip: FLAC validation + lossy cleanup ──────────────────────────────────


def test_is_flac_rejects_truncated_keeps_complete(tmp_path, _need_ffmpeg, _need_flac):
    def _sine(path, seconds):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=44100:duration={seconds}",
                "-c:a",
                "flac",
                str(path),
            ],
            check=True,
        )

    # A short but complete track is real audio — keep it even though it sits
    # well under the size heuristic the no-flac fallback uses.
    short = tmp_path / "interlude.flac"
    _sine(short, 1.2)
    assert short.stat().st_size < _FLAC_TRUNCATION_FLOOR
    assert is_flac(short) is True

    # An interrupted download leaves a file whose header still advertises the
    # full duration, so only decoding the (missing) frames exposes the gap.
    full = tmp_path / "full.flac"
    _sine(full, 3)
    data = full.read_bytes()
    partial = tmp_path / "partial.flac"
    partial.write_bytes(data[: len(data) * 2 // 5])
    assert is_flac(partial) is False

    assert is_flac(tmp_path / "never-written.flac") is False


def test_flac_audio_ok_treats_a_verify_timeout_as_broken(monkeypatch):
    # A `flac -t` that hangs past the timeout (a pathological/corrupt large
    # FLAC) must read as broken (False), not as "tool absent" (None): None
    # routes a large file through the size heuristic, which trusts it.
    import qobuz_librarian.integrations.rip as rip

    monkeypatch.setattr(rip.shutil, "which", lambda name: "/usr/bin/flac")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="flac", timeout=300)

    monkeypatch.setattr(rip.subprocess, "run", hang)

    assert rip.flac_audio_ok("/any/large.flac") is False


def test_cleanup_lossy_sorts_flac_lossy_and_broken(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)
    good = tmp_path / "good.flac"
    good.write_bytes(b"\x00" * 200_000)
    bad = tmp_path / "truncated.flac"
    bad.write_bytes(b"\x00" * 200_000)
    mp3 = tmp_path / "track.mp3"
    mp3.write_bytes(b"\x00" * 1000)
    # is_flac stubbed: only `good` verifies; the other FLAC is treated as broken.
    with patch("qobuz_librarian.integrations.rip.is_flac", side_effect=lambda p: p == good):
        kept, lossy, broken = cleanup_lossy([good, bad, mp3])
    assert kept == [good]
    assert lossy == [mp3] and broken == [bad]
    assert not bad.exists() and not mp3.exists()


# ── rip: staging residue cleanup ─────────────────────────────────────────


def test_cleanup_staging_residue_keeps_art_beside_leftover_audio(tmp_path, monkeypatch):
    # An interrupted run can leave a fully-downloaded album in staging; its
    # cover.jpg is the filesystem fetchart source on import (ARTWORK=sidecar).
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
    (album / "cover.jpg").write_bytes(b"img")
    (album / "meta.json").write_text("{}")
    # Multi-disc: art at album root, audio one level down.
    boxset = tmp_path / "Artist" / "BoxSet"
    (boxset / "Disc 1").mkdir(parents=True)
    (boxset / "Disc 1" / "01.flac").write_bytes(b"audio" * 1000)
    (boxset / "cover.jpg").write_bytes(b"img")
    # A legacy orphan has no run receipt, so its current occupant is preserved.
    orphan = tmp_path / "Old"
    orphan.mkdir()
    (orphan / "cover.jpg").write_bytes(b"img")

    cleanup_staging_residue()
    assert (album / "cover.jpg").exists() and (album / "meta.json").exists()
    assert (boxset / "cover.jpg").exists()
    assert (orphan / "cover.jpg").exists()


# ── exact empty-directory cleanup ────────────────────────────────────────


# ── lyrics: retry manifest + atomic writes ────────────────────────────────


class _FakeLyricFLAC:
    def __init__(self, path):
        from mutagen.flac import VCFLACDict

        self.filename = str(path)
        self.tags = VCFLACDict()
        self.save_targets = []

    def save(self, target):
        self.save_targets.append(target)
        Path(target).write_bytes(b"new-audio+tags")


def test_lyric_retry_round_trips_and_clears(tmp_path, monkeypatch):
    rfile = tmp_path / "retry.json"
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
    save_lyric_retry(["/music/a.flac", "/music/b.flac"])
    assert load_lyric_retry() == ["/music/a.flac", "/music/b.flac"]
    # Saving an empty list removes the file rather than leaving an empty manifest.
    save_lyric_retry([])
    assert not rfile.exists()


def test_write_lyrics_saves_atomically_and_clears_legacy_tag(tmp_path):
    from qobuz_librarian.integrations import lyric_fetch

    real = tmp_path / "track.flac"
    real.write_bytes(b"original-audio")

    f = _FakeLyricFLAC(real)
    f.tags["UNSYNCEDLYRICS"] = ["stale plain text"]
    lyric_fetch.write_lyrics(f, "[00:01.00]hello")

    assert f.tags["lyrics"] == ["[00:01.00]hello"]
    assert "unsyncedlyrics" not in f.tags
    # The live file must never be written in place — mutagen saves into a temp
    # copy that is then atomically swapped in, so a crash can't truncate it.
    assert f.save_targets and all(t != f.filename for t in f.save_targets)
    assert real.read_bytes() == b"new-audio+tags"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_lyric_fetch_refuses_paths_outside_or_linked_out_of_its_owned_root(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch

    music_root = tmp_path / "music"
    outside = tmp_path / "outside"
    (outside / "Artist" / "Album").mkdir(parents=True)
    music_root.mkdir()
    (music_root / "Artist").symlink_to(outside / "Artist", target_is_directory=True)
    outside_track = outside / "outside.flac"
    linked_track = music_root / "Artist" / "Album" / "linked.flac"
    outside_track.write_bytes(b"outside audio")
    (outside / "Artist" / "Album" / "linked.flac").write_bytes(b"linked outside audio")
    state_path = tmp_path / "lyrics-state.json"
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)

    counts = lyric_fetch.fetch_for_paths(
        [outside_track, linked_track],
        owned_root=music_root,
        state_path=state_path,
        rescan=True,
        workers=1,
        lyrics_format="both",
    )
    indexed = lyric_fetch.index_existing(
        [outside_track, linked_track],
        owned_root=music_root,
        state_path=tmp_path / "lyrics-index-state.json",
        workers=1,
    )

    assert counts == {"unsafe-path": 2}
    assert indexed == {"unsafe-path": 2}
    assert outside_track.read_bytes() == b"outside audio"
    assert (outside / "Artist" / "Album" / "linked.flac").read_bytes() == (b"linked outside audio")
    assert not outside_track.with_suffix(".lrc").exists()
    assert not linked_track.with_suffix(".lrc").exists()


# ── beets: _beets_direct behaviour ─────────────────────────────────────────


def test_beets_runtime_check_rejects_an_unrelated_executable(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    unrelated = shutil.which("true")
    assert unrelated is not None
    monkeypatch.setattr(cfg, "BEETS_PYTHON", unrelated)

    assert beets.beets_runtime_path() is None


def test_beets_direct_preflights_a_new_database_before_import(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    config_dir = tmp_path / "beets"
    config_dir.mkdir()
    database = config_dir / "library.db"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)

    inspected = []
    imported = []

    def reject_filesystem(anchor, *_args, **_kwargs):
        inspected.append(anchor["descriptor"])
        raise OSError("unsupported database filesystem")

    monkeypatch.setattr(beets, "inspect_sqlite_source", reject_filesystem)
    monkeypatch.setattr(
        beets,
        "_beets_direct_guarded",
        lambda *_args, **_kwargs: imported.append(True) or (True, "ok"),
    )

    real_connect = beets.sqlite3.connect

    def fail_bootstrap(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("file:/proc/self/fd/"):
            raise beets.sqlite3.DatabaseError("incomplete bootstrap")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(beets.sqlite3, "connect", fail_bootstrap)
    assert beets._beets_direct(None, lambda: None, [str(tmp_path)]) == (False, "error")
    assert not database.exists()
    assert not list(config_dir.glob(".qobuz-beets-bootstrap-*"))
    assert imported == []

    monkeypatch.setattr(beets.sqlite3, "connect", real_connect)
    assert beets._beets_direct(None, lambda: None, [str(tmp_path)]) == (False, "error")
    assert inspected and inspected[0] is not None
    assert imported == []
    assert database.read_bytes().startswith(b"SQLite format 3\0")


def test_beets_direct_detects_silent_skip_by_unmoved_audio(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    captured_env = {}
    captured_args = []

    class _Proc:
        def __init__(self, lines=(), on_wait=None):
            self.stdout = iter(lines)
            self.returncode = 0
            self._on_wait = on_wait

        def wait(self, timeout=None):
            if self._on_wait:
                self._on_wait()
            return 0

        def kill(self):
            pass

    def _popen_returning(proc):
        def _popen(*args, **kwargs):
            captured_args.append(args[0])
            captured_env.update(kwargs.get("env") or {})
            return proc

        return _popen

    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
    album = tmp_path / "Artist - Album"
    album.mkdir()
    track = album / "01.flac"
    track.write_bytes(b"flac-bytes")

    # beets moves the staged track into the library (here, deletes it) and
    # prints a per-item "Skipping." for a duplicate.
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc(["Skipping.\n"], track.unlink)))
    runtime = beets._checked_beets_runtime(sys.executable)
    assert runtime is not None
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is True and kind == "ok"
    assert captured_args[-1][:4] == [
        sys.executable,
        "-I",
        str(beets._managed_beets_entrypoint()),
        "--run-beets",
    ]
    assert captured_env.get("BEETSDIR") == str(cfg.BEETS_CONFIG_DIR)

    # beets exits 0 but moves nothing out of staging — the real silent skip.
    track.write_bytes(b"flac-bytes")
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc()))
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is False and kind == "error"

    # A runtime replaced after preflight must not fall back to an unchecked
    # PATH launcher.
    invalid_runtime = beets._BeetsRuntime(
        runtime.python,
        (*runtime.link_identity[:-1], runtime.link_identity[-1] + 1),
        runtime.target_identity,
    )
    spawned_before = len(captured_args)
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=invalid_runtime,
    )
    assert ok is False and kind == "error"
    assert len(captured_args) == spawned_before


def test_beets_pruning_stays_bound_to_the_captured_staging_roots(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    staging = tmp_path / "staging"
    stable_run = staging / ".qobuz-run-111111111111111111111111"
    swapped_run = staging / ".qobuz-run-222222222222222222222222"
    stable_album = stable_run / "Artist" / "Album"
    swapped_album = swapped_run / "Artist" / "Album"
    for album in (stable_album, swapped_album):
        disc = album / "Disc 2"
        disc.mkdir(parents=True)
        (disc / "01.flac").write_bytes(b"track")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    capture = beets._capture_beets_prune_roots(
        [str(stable_album), str(swapped_album)]
    )
    assert capture is not None
    (stable_album / "Disc 2" / "01.flac").unlink()
    (swapped_album / "Disc 2" / "01.flac").unlink()
    parked = staging / "parked-original"
    swapped_run.rename(parked)
    outside = tmp_path / "outside"
    (outside / "Artist" / "Album" / "Disc 2").mkdir(parents=True)
    swapped_run.symlink_to(outside, target_is_directory=True)

    try:
        beets._prune_captured_beets_directories(capture)
    finally:
        beets._close_beets_prune_capture(capture)

    assert stable_run.is_dir()
    assert list(stable_run.iterdir()) == []
    assert swapped_run.is_symlink()
    assert (outside / "Artist" / "Album" / "Disc 2").is_dir()
    assert (parked / "Artist" / "Album" / "Disc 2").is_dir()


# ── beets: staging tag prep (quarantine, never delete) ────────────────────


def test_beets_source_capture_omits_release_manifest(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    staging = tmp_path / "staging"
    album = staging / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").write_bytes(b"audio")
    (album / "cover.json").write_text("{}")
    (album / ".qobuz-librarian-release.json").write_text(
        '{"schema_version":1,"provider":"qobuz","release_id":"123"}'
    )
    config_dir = tmp_path / "beets-config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets" / "library.db")
    monkeypatch.setattr(beets, "_resolve_beets_runtime", lambda: object())
    monkeypatch.setattr(
        beets,
        "_configured_beets_plugins",
        lambda _runtime: {
            "plugins": [],
            "plugin_paths": [],
            "musicbrainz_enabled": False,
            "disabled": [],
        },
    )
    monkeypatch.setattr(beets, "_prepare_staging_tags", lambda roots=None: None)

    prepared_sources = []
    _override, cleanup, _runtime = beets._prepare_for_beets_run(
        roots=[album], source_files_out=prepared_sources
    )
    try:
        assert all(
            receipt.path.name != ".qobuz-librarian-release.json"
            for receipt in prepared_sources
        )
        assert {receipt.path.name for receipt in prepared_sources} == {
            "01 - Track.flac", "cover.json"}
    finally:
        if cleanup is not None:
            cleanup()


def test_prepare_staging_tags_sets_aside_untagged_keeps_tagged(tmp_path, monkeypatch, _need_ffmpeg):
    # A cancelled/crashed rip leaves untagged FLACs beets would file under
    # '/_/'. They're moved out of the import set — but set aside, never deleted.
    from mutagen.flac import FLAC

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.integrations.staging import capture_file

    staging = tmp_path / "staging"
    data = tmp_path / "data"
    staging.mkdir()
    data.mkdir()
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", data)

    tagged = staging / "Real Artist" / "Real Album" / "01 - Good.flac"
    untagged = staging / "Partial" / "00 -.flac"
    _make_silent_flac(tagged)
    _make_silent_flac(untagged)
    f = FLAC(str(tagged))
    f["albumartist"], f["album"], f["title"] = ["Real Artist"], ["Real Album"], ["Good"]
    f.save()
    broken = staging / "Broken" / "x.flac"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a flac at all")

    moved = beets._prepare_staging_tags()
    assert tagged.exists()
    assert not untagged.exists() and untagged in moved
    assert not broken.exists() and broken in moved
    assert len(list((staging / cfg.BEETS_RETRY_DIR).rglob("*.flac"))) == 2

    clean = capture_file(tagged)
    assert clean is not None
    binding = [
        {
            "slot": "qobuz:1",
            "path": str(clean.path),
            "identity": list(clean.identity),
        }
    ]
    intent = beets._prepare_staging_tags(roots=[tagged.parent], managed_bindings=binding)
    assert intent[0]["identity"] == list(clean.identity)

    f = FLAC(str(tagged))
    f["album"] = ["  Real Album  "]
    f.save()
    dirty = capture_file(tagged)
    assert dirty is not None
    binding[0]["identity"] = list(dirty.identity)
    with pytest.raises(OSError, match="requires a tag-clean rewrite"):
        beets._prepare_staging_tags(roots=[tagged.parent], managed_bindings=binding)
    assert capture_file(tagged, expected=dirty.identity) is not None
    assert FLAC(str(tagged))["album"] == ["  Real Album  "]

    rewritten = beets.prepare_managed_staging_tags(
        [tagged.parent],
        binding,
        authority_check=lambda: None,
    )
    assert rewritten[0]["identity"] != list(dirty.identity)
    assert FLAC(str(tagged))["album"] == ["Real Album"]

    f = FLAC(str(tagged))
    f["album"] = ["  Real Album  "]
    f.save()
    dirty = capture_file(tagged)
    assert dirty is not None
    binding[0]["identity"] = list(dirty.identity)
    authority_live = [True]
    commit_checks = []

    def authority_check():
        if not authority_live[0]:
            raise RuntimeError("lease lost")

    def stop_at_commit(_tags, _path, *, commit_guard, **_kwargs):
        authority_live[0] = False
        try:
            allowed = commit_guard()
        except RuntimeError:
            allowed = False
        commit_checks.append(allowed)
        raise OSError("commit refused")

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "save_flac_tags", stop_at_commit)
    with pytest.raises(OSError, match="tag-clean rewrite failed"):
        beets.prepare_managed_staging_tags(
            [tagged.parent],
            binding,
            authority_check=authority_check,
        )
    assert commit_checks == [False]
    assert capture_file(tagged, expected=dirty.identity) is not None


def test_prepare_managed_staging_tags_returns_bindings_in_input_order(
    tmp_path, monkeypatch, _need_ffmpeg
):
    # The scan walks the tree in directory order, but the durable runner
    # compares the result against the catalogue-ordered bindings. The records
    # must come back in the order they were passed, not readdir order.
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.integrations.staging import capture_file

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)

    album = staging / "Artist" / "Album"
    binding = []
    for n in (1, 2, 3):
        path = album / f"0{n} - Track {n}.flac"
        _make_silent_flac(path)
        from mutagen.flac import FLAC
        f = FLAC(str(path))
        f["albumartist"], f["album"], f["title"] = ["Artist"], ["Album"], [f"Track {n}"]
        f.save()
        clean = capture_file(path)
        binding.append({
            "slot": f"qobuz:{n}",
            "path": str(clean.path),
            "identity": list(clean.identity),
        })

    # Hand them in reverse of on-disk name order; the walk won't match this.
    binding.reverse()
    rewritten = beets.prepare_managed_staging_tags(
        [album], binding, authority_check=lambda: None
    )
    assert [r["slot"] for r in rewritten] == [b["slot"] for b in binding]


# ── beets: import override pins non-destructive duplicate handling ─────────


def test_append_album_path_suffix_changes_only_album_component():
    from qobuz_librarian.integrations.beets import append_album_path_suffix

    assert append_album_path_suffix(
        "$albumartist/$album ($year)/$track - $title",
        " [qobuz-200]",
    ) == "$albumartist/$album ($year) [qobuz-200]/$track - $title"


@pytest.mark.parametrize("template", [
    "$artist/$track - $title",
    "$album/$album/$track - $title",
])
def test_append_album_path_suffix_refuses_ambiguous_templates(template):
    from qobuz_librarian.integrations.beets import append_album_path_suffix

    with pytest.raises(ValueError, match="album path component"):
        append_album_path_suffix(template, " [qobuz-200]")


def test_collision_suffix_is_written_to_each_effective_album_path(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.beets import (
        _build_import_override_yaml,
        _render_beets_override,
    )

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    configured = {
        "plugins": [],
        "disabled": [],
        "musicbrainz_enabled": False,
        "plugin_paths": [],
        "paths": {
            "default": "$albumartist/$album ($year)/$track - $title",
            "comp": "Compilations/$album ($year)/$track - $title",
            "singleton": "Singletons/$artist - $title",
            "albumtype:soundtrack": (
                "Soundtracks/$albumartist/$album ($year)/$track - $title"
            ),
        },
    }

    plain = _render_beets_override(configured)
    collision = _render_beets_override(
        configured, album_path_suffix=" [qobuz-200]")

    assert "$album ($year) [qobuz-200]/$track" in collision
    assert "Compilations/$album ($year) [qobuz-200]/$track" in collision
    assert "Singletons/$artist - $title" in collision
    assert (
        "  'albumtype:soundtrack': "
        "'Soundtracks/$albumartist/$album ($year) [qobuz-200]/$track - $title'"
        in collision
    )
    assert "[qobuz-200]" not in plain
    assert plain == _build_import_override_yaml(configured)

    configured["paths"]["singleton"] = "Singletons/$album/$track - $title"
    singleton_album = _render_beets_override(
        configured, album_path_suffix=" [qobuz-200]")
    assert "Singletons/$album [qobuz-200]/$track" in singleton_album


def test_empty_collision_suffix_preserves_frozen_override_yaml(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    monkeypatch.setattr(
        beets,
        "__file__",
        "/app/qobuz_librarian/integrations/beets.py",
    )
    configured = {
        "plugins": [],
        "disabled": [],
        "musicbrainz_enabled": False,
        "plugin_paths": [],
        "paths": {
            "default": "$albumartist/$album/$track - $title",
            "comp": "Compilations/$album/$track - $title",
            "singleton": "Singletons/$artist - $title",
            "albumtype:soundtrack": "Soundtracks/$album/$track - $title",
        },
    }

    assert beets._render_beets_override(configured, album_path_suffix="") == (
        "library: '/config/beets/musiclibrary.db'\n"
        "directory: '/music'\n"
        "import:\n"
        "  quiet: yes\n"
        "  incremental: no\n"
        "  autotag: no\n"
        "  move: yes\n"
        "  duplicate_action: merge\n"
        "pluginpath:\n"
        "  - '/app/qobuz_librarian/integrations/beets_plugins'\n"
        "disabled_plugins: []\n"
        "plugins: [qobuz_art_guard]\n"
    )


def test_config_probe_returns_effective_album_paths(monkeypatch):
    import json
    from types import SimpleNamespace

    from qobuz_librarian.integrations import beets

    runtime = SimpleNamespace(python="/beets/python")
    monkeypatch.setattr(beets, "_beets_runtime_matches", lambda _runtime: True)
    payload = {
        "version": beets._BEETS_CONFIG_PROTOCOL_VERSION,
        "beets_version": beets._SUPPORTED_BEETS_VERSION,
        "python": [3, 12],
        "plugins": [],
        "plugin_paths": [],
        "disabled": [],
        "musicbrainz_enabled": False,
        "paths": {
            "default": "$albumartist/$album/$track - $title",
            "comp": "Compilations/$album/$track - $title",
            "singleton": "Singletons/$artist - $title",
            "albumtype:soundtrack": "Soundtracks/$album/$track - $title",
        },
    }
    monkeypatch.setattr(
        beets.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode() + b"\n",
            stderr=b"",
        ),
    )

    paths = beets._configured_beets_plugins(runtime)["paths"]
    assert paths == payload["paths"]
    assert list(paths) == [
        "default",
        "comp",
        "singleton",
        "albumtype:soundtrack",
    ]


def test_unsupported_collision_template_stops_before_staging_or_beets(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    config_dir = tmp_path / "beets-config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}")
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets" / "library.db")
    monkeypatch.setattr(beets, "_resolve_beets_runtime", lambda: object())
    monkeypatch.setattr(
        beets,
        "_configured_beets_plugins",
        lambda _runtime: {
            "plugins": [],
            "plugin_paths": [],
            "musicbrainz_enabled": False,
            "disabled": [],
            "paths": {
                "default": "$albumartist/$album/$track - $title",
                "comp": "Compilations/$album/$track - $title",
                "singleton": "Singletons/$artist - $title",
                "genre:Classical": "Classical/$album/$album/$track - $title",
            },
        },
    )
    prepared = []
    monkeypatch.setattr(
        beets, "_prepare_staging_tags", lambda roots=None: prepared.append(roots))
    lines = []
    monkeypatch.setattr(beets.log, "info", lines.append)

    assert beets._prepare_for_beets_run(
        album_path_suffix=" [qobuz-200]") == (None, None, None)
    assert prepared == []
    assert any("identity-review" in line for line in lines)


def test_unsupported_managed_collision_template_stops_before_staging(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    config_dir = tmp_path / "beets-config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}")
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", config_dir)
    monkeypatch.setattr(beets, "_resolve_beets_runtime", lambda: object())
    monkeypatch.setattr(
        beets,
        "_configured_beets_plugins",
        lambda _runtime: {
            "plugins": [],
            "plugin_paths": [],
            "musicbrainz_enabled": False,
            "disabled": [],
            "paths": {
                "default": "$albumartist/$album/$track - $title",
                "comp": "Compilations/$album/$track - $title",
                "singleton": "Singletons/$artist - $title",
                "genre:Classical": "Classical/$album/$album/$track - $title",
            },
        },
    )
    prepared = []
    reservations = []
    monkeypatch.setattr(
        beets,
        "_prepare_staging_tags",
        lambda **kwargs: prepared.append(kwargs),
    )

    assert beets._prepare_managed_beets_run(
        [],
        [],
        {"operation_id": "a" * 64, "item_id": "b" * 64},
        on_reservation=reservations.append,
        album_path_suffix=" [qobuz-200]",
    ) is None
    assert prepared == []
    assert reservations == []


def test_collision_suffix_reaches_both_beets_import_entry_points(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import beets

    album_dir = tmp_path / "staging" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    seen = []

    def prepare(*, roots, ownership_out=None, source_files_out=None,
                album_path_suffix=""):
        seen.append(album_path_suffix)
        return Path("/override.yaml"), lambda: None, object()

    monkeypatch.setattr(beets, "_prepare_for_beets_run", prepare)
    monkeypatch.setattr(beets, "_beets_direct", lambda *_args, **_kwargs: (True, "ok"))

    assert beets.beets_import_paths(
        consolidate=False,
        album_dirs=[album_dir],
        album_path_suffix=" [qobuz-200]",
    ) is True
    assert beets.beets_import_albums(
        [album_dir], album_path_suffix=" [qobuz-200]") == "ok"
    assert seen == [" [qobuz-200]", " [qobuz-200]"]


def test_import_placement_uses_collision_suffix_for_manifested_friendly_path(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library.release_identity import (
        ReleaseIdentity,
        publish_release_identity,
    )

    friendly = tmp_path / "music" / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    monkeypatch.setattr(beets, "find_album_dir_filesystem", lambda _album: friendly)

    placement = beets.resolve_album_import_placement(
        {"id": "200", "title": "Album", "artist": {"name": "Artist"}},
        "token",
    )

    assert placement.suffix == " [qobuz-200]"


def test_import_placement_reports_ambiguous_unmarked_friendly_path(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import beets

    friendly = tmp_path / "music" / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    candidates = [{"id": "100"}, {"id": "200"}]
    monkeypatch.setattr(beets, "find_album_dir_filesystem", lambda _album: friendly)
    monkeypatch.setattr(
        beets,
        "find_qobuz_album_candidates_for_dir",
        lambda *_args, **_kwargs: candidates,
    )
    monkeypatch.setattr(
        beets,
        "select_legacy_release",
        lambda _existing, _candidates, **_kwargs: (
            None, [object(), object()]
        ),
    )

    with pytest.raises(beets.AlbumImportIdentityAmbiguous):
        beets.resolve_album_import_placement(
            {"id": "200", "title": "Album", "artist": {"name": "Artist"}},
            "token",
        )


def test_import_placement_refuses_unmarked_path_without_compatible_release(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import beets

    friendly = tmp_path / "music" / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    monkeypatch.setattr(beets, "find_album_dir_filesystem", lambda _album: friendly)
    monkeypatch.setattr(
        beets,
        "find_qobuz_album_candidates_for_dir",
        lambda *_args, **_kwargs: [{"id": "200"}],
    )
    monkeypatch.setattr(
        beets,
        "select_legacy_release",
        lambda _existing, _candidates, **_kwargs: (None, []),
    )

    with pytest.raises(beets.AlbumPlacementAttention, match="unmarked"):
        beets.resolve_album_import_placement(
            {"id": "200", "title": "Album", "artist": {"name": "Artist"}},
            "token",
        )


def test_import_placement_rejects_directory_swapped_after_legacy_selection(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import beets

    friendly = tmp_path / "music" / "Artist" / "Album (2020)"
    reviewed = tmp_path / "reviewed-album"
    replacement = tmp_path / "empty-replacement"
    friendly.mkdir(parents=True)
    replacement.mkdir()
    (friendly / "01 - One.flac").write_bytes(b"reviewed audio")
    candidates = [{
        "id": "200",
        "tracks": {"items": [{
            "title": "One",
            "isrc": "A",
            "media_number": 1,
            "track_number": 0,
        }]},
    }]
    monkeypatch.setattr(beets, "find_album_dir_filesystem", lambda _album: friendly)
    monkeypatch.setattr(
        beets,
        "find_qobuz_album_candidates_for_dir",
        lambda *_args, **_kwargs: candidates,
    )
    real_select = beets.select_legacy_release

    def select_then_swap(*args, **kwargs):
        result = real_select(*args, **kwargs)
        friendly.rename(reviewed)
        replacement.rename(friendly)
        return result

    monkeypatch.setattr(beets, "select_legacy_release", select_then_swap)

    with pytest.raises(beets.AlbumPlacementAttention, match="changed|proof"):
        beets.resolve_album_import_placement(
            {"id": "200", "title": "Album", "artist": {"name": "Artist"}},
            "token",
        )

    assert not (friendly / ".qobuz-librarian-release.json").exists()
    assert (reviewed / "01 - One.flac").read_bytes() == b"reviewed audio"


def test_import_placement_aba_reads_held_a_and_never_adopts_b(
        monkeypatch, tmp_path, _need_ffmpeg):
    from mutagen.flac import FLAC

    from qobuz_librarian import config as cfg
    from qobuz_librarian import run_lock
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library import album_placement
    from qobuz_librarian.library.release_identity import read_release_identity

    friendly = tmp_path / "music" / "Artist" / "Album (2020)"
    a_away = tmp_path / "a-away"
    b_source = tmp_path / "b-source"
    b_away = tmp_path / "b-away"
    a_track = friendly / "01.flac"
    b_track = b_source / "01.flac"
    _make_silent_flac(a_track)
    _make_silent_flac(b_track)
    a_tags = FLAC(str(a_track))
    a_tags["title"] = "Alpha"
    a_tags["isrc"] = "USAAA0000001"
    a_tags["tracknumber"] = "1"
    a_tags.save()
    b_tags = FLAC(str(b_track))
    b_tags["title"] = "Beta"
    b_tags["isrc"] = "USBBB0000002"
    b_tags["tracknumber"] = "1"
    b_tags["comment"] = "replacement-b-has-different-audio-and-tags"
    b_tags.save()
    a_digest = hashlib.sha256(a_track.read_bytes()).hexdigest()
    b_digest = hashlib.sha256(b_track.read_bytes()).hexdigest()
    assert a_digest != b_digest
    wanted = {
        "id": "100",
        "tracks": {"items": [{
            "title": "Alpha",
            "isrc": "USAAA0000001",
            "media_number": 1,
            "track_number": 1,
        }]},
    }
    replacement = {
        "id": "200",
        "tracks": {"items": [{
            "title": "Beta",
            "isrc": "USBBB0000002",
            "media_number": 1,
            "track_number": 1,
        }]},
    }
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "beets-adoption.lock")
    monkeypatch.setattr(beets, "find_album_dir_filesystem", lambda _album: friendly)
    monkeypatch.setattr(
        beets,
        "find_qobuz_album_candidates_for_dir",
        lambda *_args, **_kwargs: [wanted, replacement],
    )
    real_reader = album_placement._read_held_audio_meta
    observed = []

    def read_a_during_public_aba(path):
        friendly.rename(a_away)
        b_source.rename(friendly)
        value = real_reader(path)
        observed.append((value["title"], value["isrc"], hashlib.sha256(
            path.read_bytes()
        ).hexdigest()))
        friendly.rename(b_away)
        a_away.rename(friendly)
        return value

    monkeypatch.setattr(
        album_placement,
        "_read_held_audio_meta",
        read_a_during_public_aba,
    )
    real_select = beets.select_legacy_release
    selected_ids = []

    def record_selection(*args, **kwargs):
        selected, compatible = real_select(*args, **kwargs)
        selected_ids.append(
            str(selected.album["id"]) if selected is not None else None
        )
        return selected, compatible

    monkeypatch.setattr(beets, "select_legacy_release", record_selection)
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with pytest.raises(beets.AlbumPlacementAttention, match="changed|authority"):
            beets.resolve_album_import_placement(
                {"id": "100", "title": "Album", "artist": {"name": "Artist"}},
                "token",
            )
    finally:
        lease.close()

    assert observed == [("Alpha", "USAAA0000001", a_digest)]
    assert "200" not in selected_ids
    assert read_release_identity(friendly) is None


def test_import_placement_uses_held_manifest_and_audio_count_while_scan_is_live(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library import catalog
    from qobuz_librarian.library.album_placement import PlacementDisposition

    friendly = tmp_path / "music" / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    (friendly / "01 - One.flac").write_bytes(b"held legacy audio")
    intended = {
        "id": "100",
        "title": "Album",
        "artist": {"name": "Artist"},
        "release_date_original": "2020-01-01",
        "maximum_bit_depth": 16,
        "maximum_sampling_rate": 44.1,
        "tracks_count": 1,
        "tracks": {"items": [{
            "title": "One",
            "isrc": "",
            "media_number": 1,
            "track_number": 1,
        }]},
    }
    candidate = {key: value for key, value in intended.items() if key != "tracks"}
    public_reads = []
    real_read_identity = catalog.read_release_identity
    real_count_audio = catalog._count_audio_files_in

    def read_identity(path):
        public_reads.append(("manifest", Path(path)))
        return real_read_identity(path)

    def count_audio(path):
        public_reads.append(("audio_count", Path(path)))
        return real_count_audio(path)

    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "held-catalog-state.lock")
    monkeypatch.setattr(beets, "find_album_dir_filesystem", lambda _album: friendly)
    monkeypatch.setattr(catalog, "find_album_dir_filesystem", lambda _album: friendly)
    monkeypatch.setattr(catalog, "search_albums", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(catalog, "get_album", lambda *_args, **_kwargs: intended)
    monkeypatch.setattr(catalog, "read_release_identity", read_identity)
    monkeypatch.setattr(catalog, "_count_audio_files_in", count_audio)

    placement = beets.resolve_album_import_placement(intended, "token")

    assert placement.disposition is PlacementDisposition.ADOPTED
    assert placement.destination == friendly
    assert public_reads == []


def test_import_override_pins_duplicate_action_merge(monkeypatch):
    # OUR importer must pin duplicate_action: merge regardless of the user's
    # config.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    conf = yaml.safe_load(beets._build_import_override_yaml())
    assert conf["import"]["duplicate_action"] == "merge"
    assert conf["plugins"][-1] == "qobuz_art_guard"
    assert conf["pluginpath"][0] == str(Path(beets.__file__).parent / "beets_plugins")
    # Streamrip already wrote authoritative Qobuz tags, so autotag must be pinned
    # off — otherwise a user's autotag:yes pushes downloads through MusicBrainz
    # matching and strands unmatched albums in staging under quiet mode.
    assert conf["import"]["autotag"] is False


def _duplicate_album_fixture(tmp_path, *, conflicting_attribute=False):
    import sqlite3

    music = tmp_path / "music" / "Artist" / "Album"
    music.mkdir(parents=True)
    first = music / "01.flac"
    second = music / "02.flac"
    cover = music / "cover.jpg"
    first.write_bytes(b"first audio")
    second.write_bytes(b"second audio")
    cover.write_bytes(b"artwork")
    database = tmp_path / "library.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE albums (
                added REAL, album TEXT, albumartist TEXT, artpath BLOB,
                custom_field TEXT, id INTEGER PRIMARY KEY
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB,
                title TEXT, mtime REAL
            );
            CREATE TABLE album_attributes (
                id INTEGER PRIMARY KEY, entity_id INTEGER,
                key TEXT, value TEXT
            );
            CREATE TABLE item_attributes (
                id INTEGER PRIMARY KEY, entity_id INTEGER,
                key TEXT, value TEXT
            );
        """)
        artpath = os.fsencode(cover)
        connection.executemany(
            "INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?)",
            [
                (10.0, "Album", "Artist", artpath, "opaque", 1),
                (20.0, "Album", "Artist", None, "opaque", 2),
            ],
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?)",
            [
                (11, 1, os.fsencode(first), "First", 101.25),
                (12, 2, os.fsencode(second), "Second", 202.5),
            ],
        )
        connection.executemany(
            "INSERT INTO album_attributes VALUES (?, ?, ?, ?)",
            [
                (21, 1, "qobuz_id", "123"),
                (22, 1, "source", "qobuz"),
                (23, 2, "qobuz_id", "123"),
                (
                    24,
                    2,
                    "loser_only" if conflicting_attribute else "source",
                    "must survive" if conflicting_attribute else "qobuz",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO item_attributes VALUES (?, ?, ?, ?)",
            [(31, 11, "token", "one"), (32, 12, "token", "two")],
        )
        connection.commit()
    finally:
        connection.close()
    return database, (first, second, cover)


def _duplicate_album_db_snapshot(database):
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        return tuple(
            (table, connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall())
            for table in ("albums", "items", "album_attributes", "item_attributes")
        )
    finally:
        connection.close()


def _configure_consolidation(monkeypatch, tmp_path, database):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 30)
    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)


def test_duplicate_album_fold_preserves_files_and_all_nonstructural_data(tmp_path, monkeypatch):
    import hashlib
    import sqlite3

    from qobuz_librarian.integrations import beets

    database, files = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    relative_item = os.path.join("Artist", "Album", "01.flac")
    assert beets._consolidation_item_dir(relative_item) == str(files[0].parent)
    assert (
        beets._consolidation_path_is_protected(
            beets._consolidation_item_dir(relative_item),
            {files[0].parent},
        )
        is True
    )
    before_files = [
        (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).digest(),
        )
        for path in files
    ]
    before = _duplicate_album_db_snapshot(database)

    beets._consolidate_duplicate_albums()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT id, album_id, path, title, mtime FROM items ORDER BY id"
        ).fetchall() == [
            (11, 1, os.fsencode(files[0]), "First", 101.25),
            (12, 1, os.fsencode(files[1]), "Second", 202.5),
        ]
        assert connection.execute("SELECT * FROM albums ORDER BY id").fetchall() == [
            before[0][1][0]
        ]
        assert (
            connection.execute("SELECT * FROM album_attributes ORDER BY id").fetchall()
            == before[2][1][:2]
        )
        assert (
            connection.execute("SELECT * FROM item_attributes ORDER BY id").fetchall()
            == before[3][1]
        )
    finally:
        connection.close()
    assert before_files == [
        (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).digest(),
        )
        for path in files
    ]


def test_duplicate_album_fold_rolls_back_an_interrupted_transaction(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import beets

    database, _ = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    before = _duplicate_album_db_snapshot(database)
    fold = beets._fold_duplicate_album_group

    def interrupt_after_mutation(*args, **kwargs):
        assert fold(*args, **kwargs) is True
        raise KeyboardInterrupt

    monkeypatch.setattr(beets, "_fold_duplicate_album_group", interrupt_after_mutation)

    with pytest.raises(KeyboardInterrupt):
        beets._consolidate_duplicate_albums()

    assert _duplicate_album_db_snapshot(database) == before


def _load_art_guard_for_test(monkeypatch, loaded_plugins):
    import importlib.util
    import sys
    import types

    class FakeLog:
        def warning(self, *_args, **_kwargs):
            pass

    class FakeBeetsPlugin:
        def __init__(self):
            self.name = "qobuz_art_guard"
            self._log = FakeLog()

        def register_listener(self, *_args, **_kwargs):
            pass

    plugins_module = types.ModuleType("beets.plugins")
    plugins_module.BeetsPlugin = FakeBeetsPlugin
    plugins_module.find_plugins = lambda: loaded_plugins
    plugins_module.send = lambda *_args, **_kwargs: []
    beets_module = types.ModuleType("beets")
    beets_module.plugins = plugins_module
    monkeypatch.setitem(sys.modules, "beets", beets_module)
    monkeypatch.setitem(sys.modules, "beets.plugins", plugins_module)

    from qobuz_librarian.integrations import beets

    plugin_path = Path(beets.__file__).parent / "beets_plugins" / "qobuz_art_guard.py"
    spec = importlib.util.spec_from_file_location("_qobuz_art_guard_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _art_guard_task(root, staging, album_name):
    import types

    destination_dir = root / "Artist" / album_name
    candidate = staging / f"{album_name}.jpg"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"new artwork")

    class Album:
        albumartist = "Artist"
        album = album_name
        artpath = None
        stored = 0

        def art_destination(self, _candidate, *, item_dir):
            return os.path.join(item_dir, b"cover.jpg")

        def store(self):
            self.stored += 1

    class Item:
        def __init__(self):
            self.id = 1
            self.path = os.fsencode(candidate)

        @staticmethod
        def destination():
            return os.fsencode(destination_dir / "01.flac")

    class Task:
        toppath = os.fsencode(staging)

        def __init__(self):
            self.album = Album()
            self.pruned = []
            self.item = Item()

        def imported_items(self):
            return [self.item]

        def prune(self, path):
            self.pruned.append(path)

    task = Task()
    selected = types.SimpleNamespace(path=os.fsencode(candidate), source_name="filesystem")

    class FetchArt:
        name = "fetchart"
        store_source = False

        def __init__(self):
            self.art_candidates = {task: selected}

        @staticmethod
        def _is_source_file_removal_enabled():
            return False

        @staticmethod
        def _is_candidate_fallback(_candidate):
            return False

    return task, selected, FetchArt(), destination_dir


def test_art_guard_publishes_only_in_a_new_held_album_directory(tmp_path, monkeypatch):
    import gc
    import types

    root = tmp_path / "music"
    staging = tmp_path / "staging"
    root.mkdir()
    loaded = []
    module = _load_art_guard_for_test(monkeypatch, loaded)
    session = types.SimpleNamespace(lib=types.SimpleNamespace(directory=os.fsencode(root)))
    gc.collect()
    descriptor_count = len(os.listdir("/proc/self/fd"))

    existing_task, _, existing_fetchart, existing_dir = _art_guard_task(root, staging, "Existing")
    existing_dir.mkdir(parents=True)
    existing_cover = existing_dir / "cover.jpg"
    existing_cover.write_bytes(b"user artwork")
    loaded[:] = [existing_fetchart]
    plugin = module.QobuzArtGuardPlugin()
    plugin._guard_art(session, existing_task)
    plugin._publish_art(session, existing_task)

    assert existing_cover.read_bytes() == b"user artwork"
    assert existing_task.album.artpath is None
    assert existing_fetchart.art_candidates == {}

    new_task, _, new_fetchart, new_dir = _art_guard_task(root, staging, "Brand New")
    loaded[:] = [new_fetchart]
    plugin._guard_art(session, new_task)
    assert not new_dir.exists()
    new_dir.mkdir(parents=True)
    (new_dir / "01.flac").write_bytes(b"audio")
    new_task.item.path = os.fsencode(new_dir / "01.flac")
    plugin._publish_art(session, new_task)

    assert (new_dir / "cover.jpg").read_bytes() == b"new artwork"
    assert new_task.album.artpath == os.fsencode(new_dir / "cover.jpg")
    assert new_task.album.stored == 1
    assert new_fetchart.art_candidates == {}

    real_copy = module._copy_candidate_to_private

    race_root = tmp_path / "race-music"
    race_root.mkdir()
    race_session = types.SimpleNamespace(
        lib=types.SimpleNamespace(directory=os.fsencode(race_root))
    )
    race_task, _, race_fetchart, race_dir = _art_guard_task(race_root, staging, "Moving Parent")
    loaded[:] = [race_fetchart]
    plugin._guard_art(race_session, race_task)
    race_dir.mkdir(parents=True)
    (race_dir / "01.flac").write_bytes(b"audio")
    race_task.item.path = os.fsencode(race_dir / "01.flac")
    displaced_artist = tmp_path / "displaced-artist-with-art"

    def move_parent_after_copy(parent_fd, candidate_fd):
        copied = real_copy(parent_fd, candidate_fd)
        (race_root / "Artist").rename(displaced_artist)
        (race_root / "Artist").mkdir()
        return copied

    monkeypatch.setattr(module, "_copy_candidate_to_private", move_parent_after_copy)
    plugin._publish_art(race_session, race_task)

    assert race_task.album.artpath is None
    assert not (displaced_artist / "Moving Parent" / "cover.jpg").exists()
    assert not (race_root / "Artist" / "Moving Parent" / "cover.jpg").exists()
    assert len(os.listdir("/proc/self/fd")) == descriptor_count


@pytest.fixture
def _need_ffmpeg():
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def _need_flac():
    import shutil

    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _make_silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def test_import_override_yaml_round_trips_through_parser(monkeypatch):
    # The beets override is written by a hand-rolled single-quoted YAML
    # emitter.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", "/config/beets/musiclibrary.db")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", "/music")
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "$albumartist's picks/$album %aunique{}")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "Singles/$artist - $title")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "embed")

    parsed = yaml.safe_load(beets._build_import_override_yaml())

    assert parsed["library"] == "/config/beets/musiclibrary.db"
    assert parsed["directory"] == "/music"
    assert parsed["import"]["move"] is True
    assert parsed["import"]["autotag"] is False
    assert parsed["import"]["duplicate_action"] == "merge"
    # the apostrophe survived the single-quote doubling
    assert parsed["paths"]["default"] == "$albumartist's picks/$album %aunique{}"
    assert parsed["paths"]["singleton"] == "Singles/$artist - $title"
    assert "fetchart" in parsed["plugins"] and "embedart" in parsed["plugins"]

    # A relative deployment path must still name the same database and music
    # root after the override is written inside the Beets config directory.
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("music"))
    relative = yaml.safe_load(beets._build_import_override_yaml())
    assert relative["library"] == str(Path("beets/musiclibrary.db").absolute())
    assert relative["directory"] == str(Path("music").absolute())
