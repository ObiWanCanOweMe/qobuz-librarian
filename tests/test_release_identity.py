import json
import os
import stat

import pytest

from qobuz_librarian.library import release_identity
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


def test_publish_refuses_success_when_album_is_renamed_after_manifest_link(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    moved = tmp_path / "renamed-away"
    album.mkdir()
    identity = ReleaseIdentity("qobuz", "123")
    real_link = os.link
    real_fsync = os.fsync
    evidence_fsynced = []

    def link_then_replace(*args, **kwargs):
        result = real_link(*args, **kwargs)
        album.rename(moved)
        album.mkdir()
        return result

    def track_evidence_fsync(descriptor):
        if (
            (moved / MANIFEST_NAME).exists()
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
        ):
            evidence_fsynced.append(True)
        return real_fsync(descriptor)

    monkeypatch.setattr(release_identity.os, "link", link_then_replace)
    monkeypatch.setattr(release_identity.os, "fsync", track_evidence_fsync)

    with pytest.raises(ReleaseManifestError, match="directory changed"):
        publish_release_identity(album, identity)

    assert read_release_identity(album) is None
    assert read_release_identity(moved) == identity
    assert evidence_fsynced == [True, True]


def test_read_refuses_a_manifest_replaced_after_its_contents_are_read(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    publish_release_identity(album, ReleaseIdentity("qobuz", "123"))
    manifest = album / MANIFEST_NAME
    displaced = album / "displaced-manifest.json"
    real_read = os.read

    def read_then_replace(*args, **kwargs):
        contents = real_read(*args, **kwargs)
        manifest.rename(displaced)
        manifest.write_text(
            '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
        )
        return contents

    monkeypatch.setattr(release_identity.os, "read", read_then_replace)

    with pytest.raises(ReleaseManifestError, match="changed while it was read"):
        read_release_identity(album)


def test_read_refuses_absent_when_a_manifest_appears_during_open(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    manifest = album / MANIFEST_NAME
    real_open = os.open

    def open_then_publish(path, *args, **kwargs):
        if path != MANIFEST_NAME:
            return real_open(path, *args, **kwargs)
        try:
            return real_open(path, *args, **kwargs)
        except FileNotFoundError:
            manifest.write_text(
                '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
            )
            raise

    monkeypatch.setattr(release_identity.os, "open", open_then_publish)

    with pytest.raises(ReleaseManifestError, match="changed while it was read"):
        read_release_identity(album)


def test_read_revalidates_manifest_entry_after_parsing(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    publish_release_identity(album, ReleaseIdentity("qobuz", "123"))
    manifest = album / MANIFEST_NAME
    displaced = album / "displaced-manifest.json"
    real_parse = release_identity._manifest_identity_from_bytes

    def parse_then_replace(contents):
        identity = real_parse(contents)
        manifest.rename(displaced)
        manifest.write_text(
            '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
        )
        return identity

    monkeypatch.setattr(
        release_identity,
        "_manifest_identity_from_bytes",
        parse_then_replace,
    )

    with pytest.raises(ReleaseManifestError, match="changed while it was read"):
        read_release_identity(album)


def test_read_revalidates_same_inode_manifest_contents_after_parsing(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    publish_release_identity(album, ReleaseIdentity("qobuz", "123"))
    manifest = album / MANIFEST_NAME
    real_parse = release_identity._manifest_identity_from_bytes

    def parse_then_overwrite(contents):
        identity = real_parse(contents)
        manifest.write_text(
            '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
        )
        return identity

    monkeypatch.setattr(
        release_identity,
        "_manifest_identity_from_bytes",
        parse_then_overwrite,
    )

    with pytest.raises(ReleaseManifestError, match="changed while it was read"):
        read_release_identity(album)


def test_read_rejects_same_inode_overwrite_during_final_named_check(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    publish_release_identity(album, ReleaseIdentity("qobuz", "123"))
    manifest = album / MANIFEST_NAME
    real_stat = os.stat
    replaced = False

    def stat_then_overwrite(path, *args, **kwargs):
        nonlocal replaced
        value = real_stat(path, *args, **kwargs)
        if path == MANIFEST_NAME and kwargs.get("dir_fd") is not None:
            manifest.write_text(
                '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
            )
            replaced = True
        return value

    monkeypatch.setattr(release_identity.os, "stat", stat_then_overwrite)

    with pytest.raises(ReleaseManifestError, match="changed while it was read"):
        read_release_identity(album)

    assert replaced is True
    assert json.loads(manifest.read_text())["release_id"] == "456"


def test_publish_rejects_same_inode_temp_overwrite_before_link(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    real_link = os.link

    def overwrite_then_link(source, destination, *args, **kwargs):
        (album / source).write_text(
            '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
        )
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(release_identity.os, "link", overwrite_then_link)

    with pytest.raises(ReleaseManifestError, match="temporary release manifest changed"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "123"))

    assert read_release_identity(album) == ReleaseIdentity("qobuz", "456")
    assert any(
        path.name.startswith(release_identity._TEMP_PREFIX)
        for path in album.iterdir()
    )


def test_publish_retains_temp_if_final_link_disappears_before_cleanup(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    real_remove = release_identity._remove_temporary_manifest
    removed_final = False

    def remove_final_then_temp(*args, **kwargs):
        nonlocal removed_final
        directory_descriptor = args[0]
        if not removed_final:
            os.unlink(MANIFEST_NAME, dir_fd=directory_descriptor)
            removed_final = True
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(
        release_identity,
        "_remove_temporary_manifest",
        remove_final_then_temp,
    )

    with pytest.raises(ReleaseManifestError, match="release manifest changed"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "123"))

    evidence = [
        path for path in album.iterdir()
        if path.name.startswith(release_identity._TEMP_PREFIX)
    ]
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["release_id"] == "123"


def test_publish_cleanup_failure_closes_every_held_descriptor(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    real_open_album = release_identity._open_album_directory
    real_temporary = release_identity._temporary_manifest
    held = []

    def capture_album_descriptors(*args, **kwargs):
        receipt, descriptors = real_open_album(*args, **kwargs)
        held.extend(descriptors)
        return receipt, descriptors

    def capture_temporary(*args, **kwargs):
        name, descriptor = real_temporary(*args, **kwargs)
        held.append(descriptor)
        return name, descriptor

    def refuse_cleanup(*_args, **_kwargs):
        raise ReleaseManifestError("cannot remove temporary release manifest")

    monkeypatch.setattr(
        release_identity,
        "_open_album_directory",
        capture_album_descriptors,
    )
    monkeypatch.setattr(
        release_identity,
        "_temporary_manifest",
        capture_temporary,
    )
    monkeypatch.setattr(
        release_identity,
        "_remove_temporary_manifest",
        refuse_cleanup,
    )

    with pytest.raises(ReleaseManifestError, match="cannot remove temporary"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "123"))

    assert held
    for descriptor in held:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_release_id_and_reserved_artifact_rules():
    assert identity_from_album({"id": 123}) == ReleaseIdentity("qobuz", "123")
    assert identity_from_album({"id": " 123 "}) == ReleaseIdentity("qobuz", "123")
    assert identity_from_album({"id": ""}) is None
    assert is_ignored_library_artifact("._track.flac") is True
    assert is_ignored_library_artifact(MANIFEST_NAME) is True
    assert is_ignored_library_artifact("album.json") is False
