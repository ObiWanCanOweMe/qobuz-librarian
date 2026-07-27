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


def test_build_counts_negative_audio_metadata_as_unknown(tmp_path, monkeypatch):
    """A corrupt negative tag must not turn a supported file into hi-res/CD."""
    from qobuz_librarian.library import census

    root = tmp_path / "music"
    track = root / "Artist" / "Album" / "track.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    monkeypatch.setattr(
        census.scanner, "read_audio_meta", lambda _path: _meta(-24, 96000, 5))

    result = census.build(root)

    assert result.complete is True
    assert result.data["tiers"]["unknown"] == [1, 5]


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
