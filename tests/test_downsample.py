import builtins
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.integrations import downsample_engine as de
from qobuz_librarian.integrations.downsample_engine import (
    _encode_opts_for_bps,
    detect_resampler_filter,
    read_local_bit_depth,
    read_sample_rate,
    read_total_samples,
    resample_one,
    target_rate,
)


@pytest.fixture
def _need_ffmpeg():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def _need_flac():
    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _hires_flac(path: Path, seconds=2.0):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=sample_rate=96000:duration={seconds}:color=pink:amplitude=0.8",
         "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
         "-c:a", "flac", str(path)],
        check=True)


def _jpeg(path: Path, color="red"):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={color}:s=64x64", "-frames:v", "1", str(path)],
        check=True)
    return path.read_bytes()



def test_resample_preserves_source_bit_depth():
    base = "aresample=resampler=soxr"

    af, fmt, depth = _encode_opts_for_bps(24, base)
    assert fmt == "s32"
    assert depth == ["-bits_per_raw_sample", "24"]
    assert af.endswith("aformat=sample_fmts=s32")

    assert _encode_opts_for_bps(16, base) == (base, "s16", [])

    assert _encode_opts_for_bps(0, base) == (base, "s32", [])


def test_flac_header_parse_anchors_at_offset_zero():
    streaminfo = bytes(4) + bytes([0x12] * 20)         # block header + body bytes
    at_zero = b"fLaC" + streaminfo
    not_at_zero = b"ID3\x04\x00" + at_zero             # real marker pushed past 0

    assert de.parse_flac_info(at_zero) != (0, 0)       # anchored marker parses
    assert de.parse_flac_info(not_at_zero) == (0, 0)   # hidden marker ignored
    assert de.parse_flac_total_samples(at_zero) != 0
    assert de.parse_flac_total_samples(not_at_zero) == 0


def test_read_sample_rate_uses_cached_scan_metadata_before_file_probe(
        tmp_path, monkeypatch):
    src = tmp_path / "track.flac"
    src.write_bytes(b"not a real flac")
    monkeypatch.setattr(de, "cfg", SimpleNamespace(FLAC_CACHE_ENABLED=True),
                        raising=False)
    monkeypatch.setattr(
        de, "flac_cache",
        SimpleNamespace(get=lambda path: {"sample_rate": "96000"}),
        raising=False,
    )

    def fail_open(*args, **kwargs):
        raise OSError("file probe should not run when cache has sample rate")

    monkeypatch.setattr(builtins, "open", fail_open)

    assert read_sample_rate(src) == 96000



def test_resample_clean_hires_shrinks_and_verifies(tmp_path, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    in_size = src.stat().st_size
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None
    assert saved is not None and saved > 0            # genuinely smaller
    assert src.stat().st_size == in_size - saved
    assert read_sample_rate(src) == 48000             # actually downsampled
    assert read_local_bit_depth(src) == 24            # depth preserved
    assert abs(read_total_samples(src) - 48000 * 2) < 48000
    assert subprocess.run(["flac", "-t", "-s", str(src)]).returncode == 0


def _peak_dbfs(path: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                        "-af", "astats", "-f", "null", "-"],
                       capture_output=True, text=True)
    return max(float(line.split("Peak level dB:")[1])
               for line in r.stderr.splitlines() if "Peak level dB:" in line)


def test_resample_pulls_a_clipping_hot_source_below_full_scale(
        tmp_path, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=sample_rate=96000:duration=2:color=pink:amplitude=0.9",
         "-af", "volume=6dB", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
         "-c:a", "flac", str(src)],
        check=True)
    assert _peak_dbfs(src) > -0.1
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None
    assert saved is not None and saved > 0
    assert read_sample_rate(src) == 48000
    assert read_local_bit_depth(src) == 24
    assert _peak_dbfs(src) < -0.5


def test_resample_preserves_embedded_jpeg_and_all_pictures(tmp_path, _need_ffmpeg, _need_flac):
    from mutagen.flac import FLAC, Picture
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    front = _jpeg(tmp_path / "front.jpg", "red")
    back = _jpeg(tmp_path / "back.jpg", "blue")
    f = FLAC(str(src))
    for typ, data in ((3, front), (4, back)):
        pic = Picture()
        pic.type, pic.mime, pic.data = typ, "image/jpeg", data
        f.add_picture(pic)
    f.save()

    af, _ = detect_resampler_filter()
    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None and saved is not None

    pics = FLAC(str(src)).pictures
    assert len(pics) == 2                              # -map 0 kept both
    for pic in pics:
        assert pic.mime == "image/jpeg"               # -c:v copy, not PNG
        assert pic.data[:2] == b"\xff\xd8"            # real JPEG SOI marker
    assert {bytes(p.data) for p in pics} == {front, back}


def test_resample_keeps_truncated_source_untouched(tmp_path, _need_ffmpeg, _need_flac):
    full = tmp_path / "full.flac"
    _hires_flac(full, 3.0)
    data = full.read_bytes()
    src = tmp_path / "track.flac"
    src.write_bytes(data[: len(data) * 2 // 5])        # 40% — header lies full
    before = src.read_bytes()
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err is not None
    assert src.read_bytes() == before                  # original untouched
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_keeps_original_when_source_bit_depth_unreadable(
        tmp_path, monkeypatch, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)                              # real 24-bit / 96 kHz
    before = src.read_bytes()
    monkeypatch.setattr(de, "read_local_bit_depth", lambda p, **_kwargs: 0)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err is not None
    assert src.read_bytes() == before                  # master left untouched
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_keeps_original_when_decode_fails(tmp_path, monkeypatch,
                                                   _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    before = src.read_bytes()
    monkeypatch.setattr(de, "_decode_ok", lambda p, **_kwargs: False)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err == "resampled file failed verification"
    assert src.read_bytes() == before
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_target_rate_family_table():
    assert target_rate(96000) == 48000
    assert target_rate(192000) == 48000
    assert target_rate(88200) == 44100
    assert target_rate(176400) == 44100
    assert target_rate(44100) is None                  # already CD rate
    assert target_rate(48000) is None


def test_resample_keeps_original_when_flush_fails(tmp_path, monkeypatch,
                                                  _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    before = src.read_bytes()
    monkeypatch.setattr("qobuz_librarian.library.backup._fsync", lambda _p: False)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err is not None
    assert src.read_bytes() == before
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_refuses_to_overwrite_a_same_name_replacement(
        tmp_path, monkeypatch, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    replacement = tmp_path / "replacement.flac"
    displaced = tmp_path / "displaced.flac"
    _hires_flac(src, 1.0)
    _hires_flac(replacement, 1.5)
    original_bytes = src.read_bytes()
    replacement_bytes = replacement.read_bytes()
    real_exchange = de._exchange_existing

    def replace_before_exchange(*args, **kwargs):
        src.rename(displaced)
        replacement.rename(src)
        return real_exchange(*args, **kwargs)

    monkeypatch.setattr(de, "_exchange_existing", replace_before_exchange)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one(
        "track.flac", 96000, 48000, af, base_dir=tmp_path)

    assert saved is None and err is not None
    assert src.read_bytes() == replacement_bytes
    assert displaced.read_bytes() == original_bytes
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_keep_originals_refuses_a_source_that_no_longer_matches_its_receipt(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import backup

    src = tmp_path / "track.flac"
    src.write_bytes(b"hi-res master")
    value = src.stat()
    receipt = {
        "identity": (stat.S_IFMT(value.st_mode), value.st_dev, value.st_ino),
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "changed_ns": value.st_ctime_ns,
        "sha256": "0" * 64,
    }

    def fake_stash(files, album_dir, *, include_identity_receipts=False):
        assert include_identity_receipts is True
        return tmp_path / "kept", {src}, {src: receipt}

    def probe_after_receipt(*_args, **_kwargs):
        raise AssertionError("a mismatched keep-original receipt reached encoding")

    monkeypatch.setattr(backup, "stash_downsample_originals", fake_stash)
    monkeypatch.setattr(de, "read_sample_rate", lambda _path: 96000)
    monkeypatch.setattr(de, "read_local_bit_depth", probe_after_receipt)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))

    result = de.downsample_dir(
        tmp_path,
        verbose=False,
        base_dir=tmp_path,
        keep_originals=True,
    )

    assert result["resampled"] == 0
    assert result["errors"] == 1
    assert src.read_bytes() == b"hi-res master"


def test_downsample_never_sweeps_a_glob_matching_user_file(
        tmp_path, monkeypatch):
    user_file = tmp_path / ".compress-user-master.flac"
    user_file.write_bytes(b"user-owned audio")
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))

    result = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path)

    assert result["resampled"] == 0
    assert user_file.read_bytes() == b"user-owned audio"


def test_downsample_dir_stops_between_tracks_on_cancel(tmp_path, monkeypatch):
    import time as _time

    calls = []

    def fake_resample(rel, sr, rate, af, base_dir=None):
        calls.append(rel)
        _time.sleep(0.2)
        return (rel, sr, rate, 10, None)

    monkeypatch.setattr(de, "RESAMPLE_WORKERS", 1)
    monkeypatch.setattr(de, "resample_one", fake_resample)
    monkeypatch.setattr(de, "read_sample_rate", lambda p: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    for n in range(4):
        (tmp_path / f"{n}.flac").write_bytes(b"x")

    res = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path,
                            cancel_check=lambda: len(calls) > 0)

    assert res["cancelled"] is True
    assert res["resampled"] <= 2 and len(calls) <= 2   # rest were discarded


def test_downsample_dir_cancel_before_start_touches_nothing(tmp_path, monkeypatch):
    calls = []

    def fake_resample(rel, sr, rate, af, base_dir=None):
        calls.append(rel)
        return (rel, sr, rate, 10, None)

    monkeypatch.setattr(de, "RESAMPLE_WORKERS", 1)
    monkeypatch.setattr(de, "resample_one", fake_resample)
    monkeypatch.setattr(de, "read_sample_rate", lambda p: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    (tmp_path / "0.flac").write_bytes(b"x")

    res = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path,
                            cancel_check=lambda: True)

    assert res["cancelled"] is True
    assert calls == [] and res["resampled"] == 0


def test_downsample_dir_cancel_after_the_last_track_counts_complete(tmp_path, monkeypatch):
    calls = []

    def fake_resample(rel, sr, rate, af, base_dir=None):
        calls.append(rel)
        return (rel, sr, rate, 10, None)

    monkeypatch.setattr(de, "RESAMPLE_WORKERS", 1)
    monkeypatch.setattr(de, "resample_one", fake_resample)
    monkeypatch.setattr(de, "read_sample_rate", lambda p: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    (tmp_path / "0.flac").write_bytes(b"x")

    res = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path,
                            cancel_check=lambda: len(calls) > 0)

    assert calls == ["0.flac"]
    assert res["resampled"] == 1
    assert res["cancelled"] is False


def test_flush_failed_rewrite_counts_as_resampled_with_a_warning(tmp_path, monkeypatch):
    def fake_resample(rel, sr, rate, af, base_dir=None):
        return (rel, sr, rate, 10,
                "resampled, but the folder couldn't be flushed to disk")

    monkeypatch.setattr(de, "RESAMPLE_WORKERS", 1)
    monkeypatch.setattr(de, "resample_one", fake_resample)
    monkeypatch.setattr(de, "read_sample_rate", lambda p: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    (tmp_path / "0.flac").write_bytes(b"x")

    res = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path)

    assert res["resampled"] == 1
    assert res["errors"] == 0
    assert res["flush_warnings"] == 1
    assert res["saved_bytes"] == 10
