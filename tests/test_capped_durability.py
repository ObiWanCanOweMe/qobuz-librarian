from datetime import datetime, timedelta, timezone

from qobuz_librarian.library import catalog
from qobuz_librarian.quality import decision


def _old_entry(scope=None):
    entry = {
        "ts": (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    }
    if scope:
        entry["scope"] = scope
    return entry


def test_old_local_downsample_marker_still_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(decision.cfg, "CAPPED_FILE", tmp_path / "capped.json")
    monkeypatch.setattr(decision.cfg, "MUSIC_ROOT", tmp_path)
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    key = decision._local_album_cap_key(album_dir)
    capped = {
        key: {**_old_entry(scope="local_album"), "album_dir": str(album_dir)}
    }
    assert decision.is_local_album_capped(album_dir, capped) is True


def test_old_qobuz_partial_marker_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(decision.cfg, "CAPPED_FILE", tmp_path / "capped.json")
    capped = {"12345": _old_entry()}
    assert decision.is_album_capped("12345", capped) is False


def test_imported_album_can_be_found_from_staged_track_signatures(tmp_path, monkeypatch):
    music = tmp_path / "music"
    album_dir = music / "Bill Evans" / "Waltz For Debby (2023)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - My Foolish Heart.flac"
    track.write_bytes(b"fake flac")
    signature = ("bill evans", "waltz for debby", 1, 1, "my foolish heart")

    monkeypatch.setattr(catalog.config, "MUSIC_ROOT", music)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.rip._flac_signature",
        lambda p: signature if p == track else None,
    )

    assert catalog.find_album_dir_by_track_signatures([signature]) == album_dir
