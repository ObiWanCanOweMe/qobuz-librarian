import builtins
import shutil
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
    """A real 24-bit / 96 kHz FLAC the resampler would downsample to 48 kHz.

    Uses noise (not a sine) so the audio is near-incompressible — the way real
    hi-res music behaves — and the 96→48 kHz downsample genuinely halves it,
    exercising the size-sanity guard instead of tripping it on a trivially
    compressible test tone."""
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


# ── existing: pure helper ──────────────────────────────────────────────────

def test_resample_preserves_source_bit_depth():
    base = "aresample=resampler=soxr"

    # 24-bit must stay 24-bit: feeding s32 to the flac encoder writes a 32-bit
    # stream unless bits_per_raw_sample pins it, which would inflate the master.
    af, fmt, depth = _encode_opts_for_bps(24, base)
    assert fmt == "s32"
    assert depth == ["-bits_per_raw_sample", "24"]
    assert af.endswith("aformat=sample_fmts=s32")

    # 16-bit stays 16-bit with no depth pin and the filter left alone.
    assert _encode_opts_for_bps(16, base) == (base, "s16", [])

    # Unknown depth (header unreadable) falls back to the safe s32 default.
    assert _encode_opts_for_bps(0, base) == (base, "s32", [])


def test_flac_header_parse_anchors_at_offset_zero():
    # STREAMINFO is the first block immediately after the fLaC marker at offset 0.
    # A leading ID3v2 tag (or any bytes) that happens to contain "fLaC" must not
    # be read as the header — the old scan parsed garbage at the false marker and
    # returned a nonzero, wrong rate/depth the metaflac backstop (which only fires
    # on a 0) never caught, feeding an irreversible in-place resample.
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


# ── integration: the destructive resample path ─────────────────────────────

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
    # Duration preserved (samples scale with the rate ratio).
    assert abs(read_total_samples(src) - 48000 * 2) < 48000
    # And it still decodes.
    assert subprocess.run(["flac", "-t", "-s", str(src)]).returncode == 0


def _peak_dbfs(path: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                        "-af", "astats", "-f", "null", "-"],
                       capture_output=True, text=True)
    return max(float(line.split("Peak level dB:")[1])
               for line in r.stderr.splitlines() if "Peak level dB:" in line)


def test_resample_pulls_a_clipping_hot_source_below_full_scale(
        tmp_path, _need_ffmpeg, _need_flac):
    """A hot, clipped master whose soxr reconstruction overshoots full scale
    must not come back clipped: the resample attenuates it by just enough to
    land at the ceiling, instead of hard-clipping the overshoot into audible
    distortion. Without the guard the integer output pegs at 0.0 dBFS."""
    src = tmp_path / "track.flac"
    # Brickwalled pink noise: hot enough to clip (flat-topped, so it overshoots
    # on reconstruction) and incompressible (so it genuinely shrinks).
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=sample_rate=96000:duration=2:color=pink:amplitude=0.9",
         "-af", "volume=6dB", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
         "-c:a", "flac", str(src)],
        check=True)
    assert _peak_dbfs(src) > -0.1                      # source is clipped hot
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None
    assert saved is not None and saved > 0
    assert read_sample_rate(src) == 48000
    assert read_local_bit_depth(src) == 24
    # Below full scale => attenuated, not clipped (a clipped output pegs at 0.0).
    assert _peak_dbfs(src) < -0.5


def test_resample_preserves_embedded_jpeg_and_all_pictures(tmp_path, _need_ffmpeg, _need_flac):
    # Without -c:v copy the FLAC muxer transcodes embedded JPEG art to PNG
    # (inflating the file), and without -map 0 all but one PICTURE block is
    # dropped. Embed two distinct JPEG covers and require both survive as JPEG.
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
    # A truncated hi-res FLAC still advertises its full duration in STREAMINFO;
    # ffmpeg resamples only the decodable prefix and exits 0, and flac -t passes
    # on that short output. The duration check must refuse the swap so the
    # damaged master (which the repair feature exists to catch) is never
    # silently replaced by a shortened encode.
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
    # No stray temp left behind.
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_keeps_original_when_source_bit_depth_unreadable(
        tmp_path, monkeypatch, _need_ffmpeg, _need_flac):
    # A FLAC whose bit depth can't be read (bps==0 — e.g. a leading-ID3 tag with
    # no metaflac to fall back on) used to encode at the s32 default and overwrite
    # the master in place, inflating a 24-bit file to 32-bit with the depth-match
    # guard disabled. A downsample has no re-download behind it, so refuse and keep
    # the original — the same stance as the truncated-source and decode-fail guards.
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)                              # real 24-bit / 96 kHz
    before = src.read_bytes()
    monkeypatch.setattr(de, "read_local_bit_depth", lambda p: 0)
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
    monkeypatch.setattr(de, "_decode_ok", lambda p: False)
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
    # A flush that genuinely fails means the encode may exist only in the page
    # cache; swapping it over the only lossless master risks a half-written
    # file after a crash. Refuse the swap — same stance as the decode guard.
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


def test_downsample_dir_stops_between_tracks_on_cancel(tmp_path, monkeypatch):
    # Stop must not wait for every remaining encode of the album: once the
    # cancel check trips, queued encodes are discarded and only the in-flight
    # one finishes (its swap is atomic), so a Stop mid-album lands in seconds,
    # not after the whole track list.
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

    # Trips only once the first encode has finished — an always-True check
    # would now be caught by the entry gate before any encode starts.
    res = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path,
                            cancel_check=lambda: len(calls) > 0)

    assert res["cancelled"] is True
    assert res["resampled"] <= 2 and len(calls) <= 2   # rest were discarded


def test_downsample_dir_cancel_before_start_touches_nothing(tmp_path, monkeypatch):
    # A Stop can land while the album waits for the staging lock; the album
    # must not begin rewriting the moment the lock arrives.
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
    # The stop arrived after the only track had already finished: every
    # candidate was processed, so the album must count as done (and get
    # capped) instead of being offered all over again as "cancelled".
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
    # The rewrite finished and only the directory flush failed: the file on
    # disk IS the resampled copy. Tallying it as an untouched error leaves the
    # album unmarked as capped and the summary claiming nothing changed while
    # the lossless master is already gone.
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
