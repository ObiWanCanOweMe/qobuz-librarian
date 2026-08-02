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
    is_release_manifest_name,
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


def test_publish_refuses_success_when_album_is_renamed_after_exclusive_rename(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    moved = tmp_path / "renamed-away"
    album.mkdir()
    identity = ReleaseIdentity("qobuz", "123")
    real_rename = release_identity._rename_noreplace
    real_fsync = os.fsync
    evidence_fsynced = []

    def rename_then_replace(*args, **kwargs):
        result = real_rename(*args, **kwargs)
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

    monkeypatch.setattr(release_identity, "_rename_noreplace", rename_then_replace)
    monkeypatch.setattr(release_identity.os, "fsync", track_evidence_fsync)

    with pytest.raises(ReleaseManifestError, match="changed|cannot publish"):
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


def test_publish_rejects_transaction_replacement_before_exclusive_rename(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    real_rename = release_identity._rename_noreplace

    def replace_then_rename(source_fd, source, destination_fd, destination):
        (album / source).rename(album / "displaced-transaction")
        (album / source).write_text(
            '{"schema_version":1,"provider":"qobuz","release_id":"456"}\n'
        )
        return real_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(release_identity, "_rename_noreplace", replace_then_rename)

    with pytest.raises(ReleaseManifestError, match="transaction changed"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "123"))

    assert read_release_identity(album) == ReleaseIdentity("qobuz", "456")
    assert any(
        path.name.startswith(release_identity._TRANSACTION_PREFIX)
        for path in album.iterdir()
    )


def test_publish_retains_recovery_if_final_disappears_before_directory_fsync(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    removed_final = False
    real_fsync_directory = release_identity._fsync_directory
    fsync_calls = 0

    def remove_final_then_interrupt(directory_descriptor):
        nonlocal fsync_calls, removed_final
        fsync_calls += 1
        if fsync_calls == 1:
            return real_fsync_directory(directory_descriptor)
        if not removed_final:
            os.unlink(MANIFEST_NAME, dir_fd=directory_descriptor)
            removed_final = True
        raise ReleaseManifestError("release manifest changed before directory fsync")

    monkeypatch.setattr(
        release_identity,
        "_fsync_directory",
        remove_final_then_interrupt,
    )

    with pytest.raises(ReleaseManifestError, match="release manifest changed"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "123"))

    evidence = [
        path for path in album.iterdir()
        if path.name.startswith(release_identity._TRANSACTION_PREFIX)
    ]
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["release_id"] == "123"


def test_publish_cleanup_failure_closes_every_held_descriptor(
        tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    real_create = release_identity._create_manifest_transaction
    held = []

    def capture_held_descriptor(*args, **kwargs):
        authority = real_create(*args, **kwargs)
        held.append(authority.descriptor)
        return authority

    def refuse_cleanup(*_args, **_kwargs):
        raise ReleaseManifestError("cannot remove temporary release manifest")

    monkeypatch.setattr(
        release_identity,
        "_create_manifest_transaction",
        capture_held_descriptor,
    )
    monkeypatch.setattr(
        release_identity,
        "_rename_noreplace",
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


def test_recovery_artifact_is_shared_excluded_from_generic_enumeration(tmp_path):
    from qobuz_librarian.library.scanner import iter_tree_no_symlinks

    album = tmp_path / "Album"
    album.mkdir()
    transaction_name = ".qobuz-librarian-release.txn-fixed.flac"
    transaction = album / transaction_name
    transaction.write_bytes(b"not audio")

    assert is_ignored_library_artifact(transaction_name) is True
    assert is_release_manifest_name(transaction_name) is True
    assert list(iter_tree_no_symlinks(album)) == []


def test_recovery_artifact_is_excluded_by_migration_backup_and_census(tmp_path):
    from qobuz_librarian.library import backup, census, migrate

    album = tmp_path / "Album"
    album.mkdir()
    transaction = album / ".qobuz-librarian-release.txn-fixed.flac"
    transaction.write_bytes(b"not audio")

    collected = migrate.collect_items(album)
    assert list(collected) == []
    assert collected.companion_receipts == []
    assert backup.backup_gap_fill_files([transaction], album) is None
    assert transaction.read_bytes() == b"not audio"

    inventory = census.build(album)
    assert inventory.complete is True
    assert inventory.processed == 0
    assert inventory.errors == []
    assert inventory.data is not None
    assert inventory.data["total_tracks"] == 0


def test_recovery_artifact_is_excluded_from_beets_source_capture(
    tmp_path, monkeypatch
):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    album = tmp_path / "staging" / "Artist" / "Album"
    album.mkdir(parents=True)
    transaction = album / ".qobuz-librarian-release.txn-fixed.flac"
    transaction.write_bytes(b"not audio")
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
        },
    )
    monkeypatch.setattr(beets, "_prepare_staging_tags", lambda roots=None: None)

    prepared_sources = []
    _override, cleanup, _runtime = beets._prepare_for_beets_run(
        roots=[album], source_files_out=prepared_sources
    )
    try:
        assert prepared_sources == []
    finally:
        if cleanup is not None:
            cleanup()


def test_compatibility_publish_reuses_current_run_lock_lease(
    tmp_path, monkeypatch
):
    from qobuz_librarian import config, run_lock

    album = tmp_path / "Album"
    album.mkdir()
    monkeypatch.setattr(config, "LOCK_FILE", tmp_path / "data" / "run.lock")
    lease = run_lock.acquire()
    assert lease is not None
    try:
        monkeypatch.setattr(
            run_lock,
            "acquire",
            lambda: pytest.fail("current live lease must be reused"),
        )
        assert publish_release_identity(
            album,
            ReleaseIdentity("qobuz", "123"),
        ) is True
        assert lease.closed is False
        assert run_lock.current_lease() is lease
    finally:
        lease.close()


def test_compatibility_publish_closes_lease_it_acquires(tmp_path, monkeypatch):
    from qobuz_librarian import config, run_lock

    album = tmp_path / "Album"
    album.mkdir()
    monkeypatch.setattr(config, "LOCK_FILE", tmp_path / "data" / "run.lock")
    real_acquire = run_lock.acquire
    acquired = []

    def capture_acquired_lease():
        lease = real_acquire()
        acquired.append(lease)
        return lease

    assert run_lock.current_lease() is None
    monkeypatch.setattr(run_lock, "acquire", capture_acquired_lease)
    assert publish_release_identity(
        album,
        ReleaseIdentity("qobuz", "123"),
    ) is True
    assert len(acquired) == 1
    assert acquired[0] is not None
    assert acquired[0].closed is True
    assert run_lock.current_lease() is None


def test_compatibility_publish_fails_closed_without_run_lock(
    tmp_path, monkeypatch
):
    from qobuz_librarian import run_lock

    album = tmp_path / "Album"
    album.mkdir()
    monkeypatch.setattr(run_lock, "current_lease", lambda: None)
    monkeypatch.setattr(run_lock, "acquire", lambda: None)

    with pytest.raises(ReleaseManifestError, match="requires the live run lock"):
        publish_release_identity(album, ReleaseIdentity("qobuz", "123"))

    assert not (album / MANIFEST_NAME).exists()
