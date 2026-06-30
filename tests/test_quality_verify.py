import shutil

import pytest

import qobuz_librarian.config as config
from qobuz_librarian.quality import verify


class _FakeDir:
    """Stand-in path; read_album_dir is monkeypatched in these tests."""


def _patch(monkeypatch, tracks, tier):
    monkeypatch.setattr(verify, "read_album_dir", lambda d: tracks)
    monkeypatch.setattr(config, "STREAMRIP_QUALITY", tier)


def test_cd_only_source_served_cd_is_not_under(monkeypatch):
    _patch(monkeypatch, [{"bits": 16, "sample_rate": 44100}], tier=4)
    album = {"maximum_bit_depth": 16, "maximum_sampling_rate": 44.1}
    assert verify.rip_shortfall([_FakeDir()], album)["under"] is False


def test_hires_source_served_cd_is_under(monkeypatch):
    _patch(monkeypatch, [{"bits": 16, "sample_rate": 44100}], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    r = verify.rip_shortfall([_FakeDir()], album)
    assert r["under"] is True and r["n_below"] == 1


def test_one_low_track_among_good_is_under(monkeypatch):
    _patch(monkeypatch, [
        {"bits": 24, "sample_rate": 96000},
        {"bits": 16, "sample_rate": 44100},
        {"bits": 24, "sample_rate": 96000},
    ], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    r = verify.rip_shortfall([_FakeDir()], album)
    assert r["under"] is True and r["n_below"] == 1


def test_served_at_cap_target_is_not_under(monkeypatch):
    _patch(monkeypatch, [{"bits": 24, "sample_rate": 96000}], tier=3)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
    assert verify.rip_shortfall([_FakeDir()], album)["under"] is False


def test_cap_matters_same_rip_under_higher_cap(monkeypatch):
    _patch(monkeypatch, [{"bits": 24, "sample_rate": 96000}], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
    assert verify.rip_shortfall([_FakeDir()], album)["under"] is True


def test_effective_tier_override_controls_target(monkeypatch):
    _patch(monkeypatch, [{"bits": 24, "sample_rate": 96000}], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
    result = verify.rip_shortfall([_FakeDir()], album, effective_tier=3)
    assert result["under"] is False


def test_redownload_fallback_restores_first_staged_rip_when_retry_lands_nothing(
        tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    (first / "01.flac").write_bytes(b"first rip")

    def discard_retry_output():
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

    dirs, retry_kept = verify.redownload_with_staged_fallback(
        [first],
        discard_retry_output=discard_retry_output,
        run_retry=lambda: None,
        collect_staged_dirs=lambda: [])

    assert dirs == [first]
    assert retry_kept is False
    assert (first / "01.flac").read_bytes() == b"first rip"


def test_redownload_fallback_keeps_retry_when_retry_lands_audio(tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    (first / "01.flac").write_bytes(b"first rip")

    def run_retry():
        first.mkdir(parents=True)
        (first / "02.flac").write_bytes(b"retry rip")

    dirs, retry_kept = verify.redownload_with_staged_fallback(
        [first],
        discard_retry_output=lambda: None,
        run_retry=run_retry,
        collect_staged_dirs=lambda: [first])

    assert dirs == [first]
    assert retry_kept is True
    assert not (first / "01.flac").exists()
    assert (first / "02.flac").read_bytes() == b"retry rip"


def test_redownload_fallback_restores_first_rip_on_interrupt(tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    (first / "01.flac").write_bytes(b"first rip")

    def discard_retry_output():
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

    def run_retry():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        verify.redownload_with_staged_fallback(
            [first],
            discard_retry_output=discard_retry_output,
            run_retry=run_retry,
            collect_staged_dirs=lambda: [])

    assert (first / "01.flac").read_bytes() == b"first rip"
