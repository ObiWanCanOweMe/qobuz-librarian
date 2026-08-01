import json

import pytest

from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseIdentity,
    ReleaseManifestError,
    identity_from_album,
    is_ignored_library_artifact,
    publish_release_identity,
    read_release_identity,
)


def test_manifest_round_trip_and_no_replace(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    identity = ReleaseIdentity("qobuz", "123")

    assert publish_release_identity(album, identity) is True
    assert read_release_identity(album) == identity
    assert publish_release_identity(album, identity) is False

    with pytest.raises(ReleaseManifestError, match="different release"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "456"))

    assert json.loads((album / MANIFEST_NAME).read_text()) == {
        "schema_version": 1,
        "provider": "qobuz",
        "release_id": "123",
    }


def test_manifest_rejects_links_unknown_fields_and_oversize(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version":1,"provider":"qobuz","release_id":"1"}')
    (album / MANIFEST_NAME).symlink_to(outside)
    with pytest.raises(ReleaseManifestError, match="regular file"):
        read_release_identity(album)

    (album / MANIFEST_NAME).unlink()
    (album / MANIFEST_NAME).write_text(
        '{"schema_version":1,"provider":"qobuz","release_id":"1","title":"x"}'
    )
    with pytest.raises(ReleaseManifestError, match="schema"):
        read_release_identity(album)

    (album / MANIFEST_NAME).write_bytes(b"x" * 4097)
    with pytest.raises(ReleaseManifestError, match="large"):
        read_release_identity(album)


def test_publish_rejects_a_replaced_album_directory(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    value = album.stat()
    expected = (value.st_dev, value.st_ino)
    album.rename(tmp_path / "moved")
    album.mkdir()

    with pytest.raises(ReleaseManifestError, match="directory changed"):
        publish_release_identity(
            album,
            ReleaseIdentity("qobuz", "123"),
            expected_directory=expected,
        )


def test_release_id_and_reserved_artifact_rules():
    assert identity_from_album({"id": 123}) == ReleaseIdentity("qobuz", "123")
    assert identity_from_album({"id": " 123 "}) == ReleaseIdentity("qobuz", "123")
    assert identity_from_album({"id": ""}) is None
    assert is_ignored_library_artifact("._track.flac") is True
    assert is_ignored_library_artifact(MANIFEST_NAME) is True
    assert is_ignored_library_artifact("album.json") is False
