import errno
import gc
import os
import signal
import sqlite3

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.library import post_import_relocation as relocation


def _configure(tmp_path, monkeypatch):
    music = tmp_path / "music"
    data = tmp_path / "data"
    beets = tmp_path / "beets"
    for directory in (music, data, beets):
        directory.mkdir()
    database = beets / "library.db"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", beets)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    return music, data, database


def _database(database, *, tracks, artwork=None):
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artpath BLOB)"
        )
        connection.executemany(
            "INSERT INTO items (id, path) VALUES (?, ?)",
            ((index, os.fsencode(path)) for index, path in enumerate(tracks, 1)),
        )
        connection.execute(
            "INSERT INTO albums (id, artpath) VALUES (1, ?)",
            (os.fsencode(artwork) if artwork is not None else None,),
        )
        connection.commit()
    finally:
        connection.close()


def test_abandoned_relative_file_result_closes_every_owned_descriptor(
    tmp_path,
    monkeypatch,
):
    music, _data, _database_path = _configure(tmp_path, monkeypatch)
    track = music / "Artist" / "Album" / "01 - Track.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    root_fd = relocation._open_directory(music)
    descriptors = ()
    try:
        descriptor, chain = relocation._open_relative_file(
            root_fd, "Artist/Album/01 - Track.flac"
        )
        descriptors = (descriptor, *chain)
        assert all(os.fstat(value) for value in descriptors)

        del descriptor, chain
        gc.collect()

        for value in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(value)
        assert os.fstat(root_fd)
    finally:
        for value in descriptors:
            try:
                os.close(value)
            except OSError:
                pass
        os.close(root_fd)


def test_closed_descriptor_owner_does_not_close_a_reused_number(
    tmp_path,
    monkeypatch,
):
    music, _data, _database_path = _configure(tmp_path, monkeypatch)
    track = music / "Artist" / "Album" / "01 - Track.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    root_fd = relocation._open_directory(music)
    descriptors = ()
    replacement_fd = None
    try:
        descriptor, chain = relocation._open_relative_file(
            root_fd, "Artist/Album/01 - Track.flac"
        )
        descriptors = (descriptor, *chain)
        relocation._close_all(chain)
        for value in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(value)

        replacement_fd = os.open(os.devnull, os.O_RDONLY)
        if replacement_fd != descriptor:
            os.dup2(replacement_fd, descriptor)
            os.close(replacement_fd)
            replacement_fd = descriptor
        del chain
        gc.collect()

        assert os.fstat(replacement_fd)
        assert os.fstat(root_fd)
    finally:
        if replacement_fd is not None:
            try:
                os.close(replacement_fd)
            except OSError:
                pass
        for value in descriptors:
            try:
                os.close(value)
            except OSError:
                pass
        os.close(root_fd)


def test_sigint_during_descriptor_adoption_waits_for_finalizable_owner(
    tmp_path,
    monkeypatch,
):
    music, _data, _database_path = _configure(tmp_path, monkeypatch)
    opened = []
    original_open = relocation._open_directory
    original_adopt = relocation._DescriptorOwner.adopt
    interrupted = False

    def recording_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def interrupt_once(owner, descriptor, *, visible=False):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            signal.raise_signal(signal.SIGINT)
        return original_adopt(owner, descriptor, visible=visible)

    monkeypatch.setattr(relocation, "_open_directory", recording_open)
    monkeypatch.setattr(relocation._DescriptorOwner, "adopt", interrupt_once)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            relocation._open_music_root()
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    gc.collect()
    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])


def test_sigint_during_descriptor_close_waits_until_fd_is_closed(
    monkeypatch,
):
    owner = relocation._DescriptorOwner()
    descriptor = owner.open(lambda: os.open(os.devnull, os.O_RDONLY))
    original_close = os.close
    interrupted = False

    def interrupt_once(value):
        nonlocal interrupted
        if value == descriptor and not interrupted:
            interrupted = True
            signal.raise_signal(signal.SIGINT)
        original_close(value)

    monkeypatch.setattr(relocation.os, "close", interrupt_once)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            owner.close()
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
    assert owner._finalizer.alive is False


def test_sigint_during_descriptor_finalizer_waits_until_fd_is_closed(
    monkeypatch,
):
    owner = relocation._DescriptorOwner()
    descriptor = owner.open(lambda: os.open(os.devnull, os.O_RDONLY))
    original_close = os.close
    interrupted = False

    def interrupt_once(value):
        nonlocal interrupted
        if value == descriptor and not interrupted:
            interrupted = True
            signal.raise_signal(signal.SIGINT)
        original_close(value)

    monkeypatch.setattr(relocation.os, "close", interrupt_once)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        del owner
        gc.collect()
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_partial_descriptor_adoption_stays_owned(
    monkeypatch,
):
    owner = relocation._DescriptorOwner()
    opened = []

    def opener():
        descriptor = os.open(os.devnull, os.O_RDONLY)
        opened.append(descriptor)
        return descriptor

    def interrupt_after_ownership(owner, descriptor, *, visible=False):
        owner._descriptors.append(descriptor)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        relocation._DescriptorOwner,
        "adopt",
        interrupt_after_ownership,
    )
    with pytest.raises(KeyboardInterrupt):
        owner.open(opener, visible=True)

    assert len(opened) == 1
    assert os.fstat(opened[0])
    owner.close()
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])


def _paths(database):
    connection = sqlite3.connect(database)
    try:
        tracks = [
            os.fsdecode(row[0])
            for row in connection.execute("SELECT path FROM items ORDER BY id")
        ]
        artwork = connection.execute(
            "SELECT artpath FROM albums WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    return tracks, os.fsdecode(artwork) if artwork is not None else None


def test_relocation_is_additive_and_keeps_destination_conflicts(
    tmp_path, monkeypatch
):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)

    moving = source / "01 - New.flac"
    conflict = source / "02 - Conflict.flac"
    source_art = source / "cover.jpg"
    destination_conflict = destination / conflict.name
    destination_art = destination / source_art.name
    moving.write_bytes(b"new track")
    conflict.write_bytes(b"source version")
    source_art.write_bytes(b"same artwork")
    destination_conflict.write_bytes(b"destination version")
    destination_art.write_bytes(b"same artwork")
    _database(database, tracks=(moving, conflict), artwork=source_art)

    authority = run_lock.acquire()
    try:
        result = relocation.relocate_post_import_album(
            source,
            destination,
            kind=relocation.RelocationKind.SPLIT_GAP_FILL,
            authority=authority,
        )
    finally:
        authority.close()

    assert result.changed is True
    assert (destination / moving.name).read_bytes() == b"new track"
    assert destination_conflict.read_bytes() == b"destination version"
    assert destination_art.read_bytes() == b"same artwork"
    assert conflict.read_bytes() == b"source version"
    assert not moving.exists()
    assert not source_art.exists()
    tracks, artwork = _paths(database)
    assert tracks == [str(destination / moving.name), str(conflict)]
    assert artwork == str(destination_art)


def test_whole_album_conflict_keeps_the_source_album_intact(
    tmp_path, monkeypatch
):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_track = source / "01 - Track.flac"
    source_art = source / "cover.jpg"
    destination_track = destination / source_track.name
    source_track.write_bytes(b"source audio")
    source_art.write_bytes(b"source artwork")
    destination_track.write_bytes(b"different destination audio")
    _database(database, tracks=(source_track,), artwork=source_art)

    authority = run_lock.acquire()
    try:
        result = relocation.relocate_post_import_album(
            source,
            destination,
            kind=relocation.RelocationKind.WHOLE_ALBUM,
            authority=authority,
        )
    finally:
        authority.close()

    assert result.changed is False
    assert result.destination == source
    assert source_track.read_bytes() == b"source audio"
    assert source_art.read_bytes() == b"source artwork"
    assert destination_track.read_bytes() == b"different destination audio"
    assert not (destination / source_art.name).exists()
    assert _paths(database) == ([str(source_track)], str(source_art))


@pytest.mark.parametrize(
    ("checkpoint", "committed"),
    (
        ("after-copy-data", False),
        ("after-filesystem-publication", False),
        ("after-database-publication", True),
    ),
)
def test_hard_kill_recovery_uses_database_as_commit_decision(
    tmp_path, monkeypatch, checkpoint, committed
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()

            def hard_kill(name):
                if name == checkpoint:
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    new_track = destination / track.name
    assert track.exists() is not committed
    assert new_track.exists() is committed
    tracks, _artwork = _paths(database)
    assert tracks == [str(new_track if committed else track)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def test_copy_failure_rolls_back_without_touching_source(tmp_path, monkeypatch):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(relocation, "_copy_file", disk_full)
    authority = run_lock.acquire()
    try:
        with pytest.raises(OSError, match="no space left"):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert track.read_bytes() == b"audio"
    assert not destination.exists()
    assert _paths(database)[0] == [str(track)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def test_album_link_is_refused_without_following_it(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    secret = outside / "secret"
    secret.write_bytes(b"keep")
    (source / "link").symlink_to(outside, target_is_directory=True)
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        with pytest.raises(OSError, match="link or special entry"):
            relocation.relocate_post_import_album(
                source,
                music / "Artist" / "Album",
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert secret.read_bytes() == b"keep"
    assert track.read_bytes() == b"audio"
    assert _paths(database)[0] == [str(track)]


@pytest.mark.parametrize("replacement", ("artist", "album"))
def test_destination_ancestry_replacement_before_publication_is_refused(
    tmp_path, monkeypatch, replacement
):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.parent.mkdir()
    if replacement == "album":
        destination.mkdir()
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    original = relocation._ensure_destination_artist

    def replace_destination(root_fd, payload, namespace_fd):
        original(root_fd, payload, namespace_fd)
        if replacement == "artist":
            destination.parent.rename(music / "Artist.old")
            destination.parent.mkdir()
        else:
            destination.rename(destination.with_name("Album.old"))
            destination.mkdir()

    monkeypatch.setattr(
        relocation, "_ensure_destination_artist", replace_destination
    )
    authority = run_lock.acquire()
    try:
        with pytest.raises(relocation.PostImportRelocationAttention):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert track.read_bytes() == b"audio"
    assert not (destination / track.name).exists()
    assert _paths(database)[0] == [str(track)]


def test_recovery_refuses_a_replaced_destination_album(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()

            def hard_kill(name):
                if name == "after-filesystem-publication":
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.SPLIT_GAP_FILL,
                authority=authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    published = destination / track.name
    assert published.exists()
    old_destination = destination.with_name("Album.old")
    destination.rename(old_destination)
    destination.mkdir()
    (old_destination / track.name).rename(published)

    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.ATTENTION_REQUIRED
    assert track.read_bytes() == b"audio"
    assert published.read_bytes() == b"audio"
    assert _paths(database)[0] == [str(track)]


def test_recovery_rolls_back_an_unjournalled_artist_creation(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))
    original_write_journal = relocation._write_journal

    def kill_before_artist_receipt(namespace_fd, payload, *, initial=False):
        if (
            isinstance(payload.get("destination_artist_record"), dict)
            and isinstance(payload.get("created_artist"), dict)
        ):
            os._exit(77)
        return original_write_journal(namespace_fd, payload, initial=initial)

    monkeypatch.setattr(relocation, "_write_journal", kill_before_artist_receipt)
    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    assert track.read_bytes() == b"audio"
    assert not destination.exists()
    assert _paths(database)[0] == [str(track)]


def test_truncated_journal_temps_do_not_block_canonical_recovery(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()

            def hard_kill(name):
                if name == "after-copy-data":
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    namespace = data / ".qobuz_post_import_relocations"
    operation_id = next(namespace.glob("*.json")).stem
    (namespace / f".tmp-{operation_id}-{'a' * 24}").write_bytes(b'{"cut off"')
    orphan_id = "f" * 64 if operation_id != "f" * 64 else "e" * 64
    (namespace / f".tmp-{orphan_id}-{'b' * 24}").write_bytes(b'{"cut off"')

    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    assert track.read_bytes() == b"audio"
    assert not list(namespace.glob(".tmp-*"))
    assert not list(namespace.glob("*.json"))


def test_temp_cleanup_does_not_weaken_canonical_validation(
    tmp_path, monkeypatch
):
    _music, data, _database_path = _configure(tmp_path, monkeypatch)
    namespace = data / ".qobuz_post_import_relocations"
    namespace.mkdir()
    operation_id = "a" * 64
    (namespace / f"{operation_id}.json").write_bytes(b'{"cut off"')
    temporary = namespace / f".tmp-{operation_id}-{'b' * 24}"
    temporary.write_bytes(b'{"also cut off"')

    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.ATTENTION_REQUIRED
    assert not temporary.exists()
    assert (namespace / f"{operation_id}.json").exists()


def test_same_album_identity_alias_is_refused(tmp_path, monkeypatch):
    music, _data, _database_path = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    (source / "01 - Track.flac").write_bytes(b"audio")
    original = relocation._open_relative_directory

    def open_with_destination_alias(root_fd, relative):
        if relative == "Artist/Album":
            return original(root_fd, "Artist, Other/Album")
        return original(root_fd, relative)

    monkeypatch.setattr(
        relocation, "_open_relative_directory", open_with_destination_alias
    )
    authority = run_lock.acquire()
    try:
        with pytest.raises(
            relocation.PostImportRelocationUnavailable,
            match="same album folder",
        ):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()


def test_source_cleanup_refuses_self_unlink_and_reproves_destination(
    tmp_path, monkeypatch
):
    music, _data, _database_path = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_file = source / "cover.jpg"
    destination_file = destination / source_file.name
    workspace = music / ".source-cleanup-test"
    workspace.mkdir()
    workspace_fd = relocation._open_directory(workspace)
    source_file.write_bytes(b"art")
    destination_file.write_bytes(b"art")
    root_fd, root_owner = relocation._open_music_root()
    source_fd, source_chain = relocation._open_relative_directory(
        root_fd, "Artist, Other/Album"
    )
    destination_fd, destination_chain = relocation._open_relative_directory(
        root_fd, "Artist/Album"
    )
    try:
        source_snapshot = relocation._snapshot_tree(source_fd)
        public_snapshot = relocation._snapshot_tree(destination_fd)
    finally:
        relocation._close_all(source_chain)
        relocation._close_all(destination_chain)
    payload = {
        "source": "Artist, Other/Album",
        "destination": "Artist/Album",
        "source_snapshot": source_snapshot,
        "public_snapshot": public_snapshot,
    }

    real_unlink = relocation.os.unlink

    def remove_destination_after_source(name, *, dir_fd=None):
        real_unlink(name, dir_fd=dir_fd)
        real_unlink(destination_file)

    monkeypatch.setattr(relocation.os, "unlink", remove_destination_after_source)
    try:
        with pytest.raises(
            relocation.PostImportRelocationAttention,
            match="destination changed during source cleanup",
        ):
            relocation._unlink_exact_source_file(
                root_fd, workspace_fd, payload, "cover.jpg", 0
            )
    finally:
        relocation._close_all(root_owner)

    # A second call with both names resolving to the same directory entry must
    # refuse before unlinking anything.
    source_file.write_bytes(b"art")
    root_fd, root_owner = relocation._open_music_root()
    source_fd, source_chain = relocation._open_relative_directory(
        root_fd, "Artist, Other/Album"
    )
    try:
        self_snapshot = relocation._snapshot_tree(source_fd)
    finally:
        relocation._close_all(source_chain)
    self_payload = {
        **payload,
        "destination": payload["source"],
        "source_snapshot": self_snapshot,
        "public_snapshot": self_snapshot,
    }

    class IntactExclusion:
        def intact(self):
            return True

        def close(self):
            return None

    monkeypatch.setattr(
        relocation,
        "acquire_inode_write_exclusion",
        lambda _descriptor: IntactExclusion(),
    )
    try:
        assert (
            relocation._unlink_exact_source_file(
                root_fd, workspace_fd, self_payload, "cover.jpg", 0
            )
            is False
        )
    finally:
        relocation._close_all(root_owner)
    assert source_file.read_bytes() == b"art"
    os.close(workspace_fd)
    workspace.rmdir()


def test_source_cleanup_preserves_an_atomic_replacement(tmp_path, monkeypatch):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    parked = source / "01 - Track.parked"
    track.write_bytes(b"imported audio")
    _database(database, tracks=(track,))
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    replacement = b"replacement from sync"
    swapped = False

    def source_entry(name, directory_fd):
        if name != track.name or directory_fd is None:
            return False
        value = os.fstat(directory_fd)
        return (value.st_dev, value.st_ino) == source_identity

    def replace_source():
        nonlocal swapped
        if swapped:
            return
        swapped = True
        track.rename(parked)
        track.write_bytes(replacement)

    real_unlink = relocation.os.unlink

    def swap_before_unlink(name, *, dir_fd=None):
        if source_entry(name, dir_fd):
            replace_source()
        return real_unlink(name, dir_fd=dir_fd)

    real_rename_noreplace = relocation._rename_noreplace

    def swap_before_quarantine(source_fd, source_name, destination_fd, destination_name):
        if source_entry(source_name, source_fd):
            replace_source()
        return real_rename_noreplace(
            source_fd, source_name, destination_fd, destination_name
        )

    monkeypatch.setattr(relocation.os, "unlink", swap_before_unlink)
    monkeypatch.setattr(relocation, "_rename_noreplace", swap_before_quarantine)
    authority = run_lock.acquire()
    try:
        with pytest.raises(relocation.PostImportRelocationAttention):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert swapped is True
    assert track.read_bytes() == replacement
    assert parked.read_bytes() == b"imported audio"
    assert (destination / track.name).read_bytes() == b"imported audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_source_directory_cleanup_preserves_an_atomic_replacement(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    parked = source.with_name("Album.parked")
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"imported audio")
    _database(database, tracks=(track,))
    source_parent_identity = (source.parent.stat().st_dev, source.parent.stat().st_ino)
    swapped = False

    def source_album_entry(name, directory_fd):
        if name != source.name or directory_fd is None:
            return False
        value = os.fstat(directory_fd)
        return (value.st_dev, value.st_ino) == source_parent_identity

    def replace_source_album():
        nonlocal swapped
        if swapped:
            return
        swapped = True
        source.rename(parked)
        source.mkdir()

    real_rmdir = relocation.os.rmdir

    def swap_before_rmdir(name, *, dir_fd=None):
        if source_album_entry(name, dir_fd):
            replace_source_album()
        return real_rmdir(name, dir_fd=dir_fd)

    real_rename_noreplace = relocation._rename_noreplace

    def swap_before_quarantine(source_fd, source_name, destination_fd, destination_name):
        if source_album_entry(source_name, source_fd):
            replace_source_album()
        return real_rename_noreplace(
            source_fd, source_name, destination_fd, destination_name
        )

    monkeypatch.setattr(relocation.os, "rmdir", swap_before_rmdir)
    monkeypatch.setattr(relocation, "_rename_noreplace", swap_before_quarantine)
    authority = run_lock.acquire()
    try:
        with pytest.raises(relocation.PostImportRelocationAttention):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert swapped is True
    assert source.is_dir()
    assert parked.is_dir()
    assert not list(source.iterdir())
    assert not list(parked.iterdir())
    assert (destination / track.name).read_bytes() == b"imported audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_failed_exact_source_cleanup_retains_recovery_state(tmp_path, monkeypatch):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))
    monkeypatch.setattr(
        relocation, "acquire_inode_write_exclusion", lambda _descriptor: None
    )

    authority = run_lock.acquire()
    try:
        with pytest.raises(
            relocation.PostImportRelocationAttention,
            match="source file cleanup could not be proved",
        ):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert track.read_bytes() == b"audio"
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_incomplete_empty_directory_cleanup_retains_recovery_state(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))
    real_rmdir = relocation.os.rmdir

    def arm_directory_failure(name):
        if name != "after-database-publication":
            return

        def refuse_source_album(path, *, dir_fd=None):
            # Source directories are now retired behind the private
            # workspace boundary before their exact inode is removed.
            if path == relocation._retired_source_directory_name(0):
                raise OSError(errno.EBUSY, "directory is busy")
            return real_rmdir(path, dir_fd=dir_fd)

        monkeypatch.setattr(relocation.os, "rmdir", refuse_source_album)

    monkeypatch.setattr(relocation, "_checkpoint", arm_directory_failure)
    authority = run_lock.acquire()
    try:
        with pytest.raises(
            relocation.PostImportRelocationAttention,
            match="source directory cleanup could not be proved",
        ):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert source.is_dir()
    assert not track.exists()
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_hard_kill_before_deferred_phase_rolls_back(tmp_path, monkeypatch):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()

            def hard_kill(name):
                if name == "after-database-publication":
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
                await_handoff=True,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    assert track.read_bytes() == b"audio"
    assert not (destination / track.name).exists()
    assert _paths(database)[0] == [str(track)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def test_unsealed_awaiting_handoff_rolls_back(tmp_path, monkeypatch):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        _deferred_relocation(source, destination, authority)
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    assert track.read_bytes() == b"audio"
    assert not (destination / track.name).exists()
    assert _paths(database)[0] == [str(track)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def _deferred_relocation(source, destination, authority):
    result = relocation.relocate_post_import_album(
        source,
        destination,
        kind=relocation.RelocationKind.WHOLE_ALBUM,
        authority=authority,
        await_handoff=True,
    )
    consumer = {
        "kind": "web-single",
        "job_id": "job-1",
        "job_created_at": 1234.5,
        "album_id": "123",
    }
    undo = {
        "operation_id": result.operation_id,
        "move": result.ownership_receipt,
    }
    return result, consumer, undo


def _queue_consumer():
    return {
        "kind": "queue-completion",
        "queue_operation_id": "1" * 64,
        "item_id": "2" * 64,
        "action_id": "3" * 64,
    }


def _retained_deferred_relocation(source, destination, authority, *, expected=None):
    result = relocation.relocate_post_import_album(
        source,
        destination,
        kind=relocation.RelocationKind.WHOLE_ALBUM,
        authority=authority,
        await_handoff=True,
        retain_completion=True,
        expected=expected,
    )
    consumer = _queue_consumer()
    handoff_payload = {
        "operation_id": result.operation_id,
        "move": result.ownership_receipt,
    }
    digest = relocation.seal_post_import_relocation_handoff(
        result.operation_id,
        consumer=consumer,
        payload=handoff_payload,
        authority=authority,
    )
    return result, consumer, digest


def test_persisted_deferred_handoff_completes_during_recovery(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        result, consumer, undo = _deferred_relocation(
            source, destination, authority
        )
        digest = relocation.seal_post_import_relocation_handoff(
            result.operation_id,
            consumer=consumer,
            payload=undo,
            authority=authority,
        )
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=lambda operation_id, handoff: (
                operation_id == result.operation_id
                and handoff == {"consumer": consumer, "hash": digest}
            ),
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    assert not track.exists()
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_unavailable_deferred_handoff_owner_requires_attention(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        result, consumer, undo = _deferred_relocation(
            source, destination, authority
        )
        relocation.seal_post_import_relocation_handoff(
            result.operation_id,
            consumer=consumer,
            payload=undo,
            authority=authority,
        )
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=lambda _operation_id, _handoff: None,
        )
    finally:
        authority.close()

    assert (
        recovered.status
        is relocation.RelocationRecoveryStatus.ATTENTION_REQUIRED
    )
    assert track.read_bytes() == b"audio"
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_acknowledgement_requires_exact_hash_then_cleans_up(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        result, consumer, undo = _deferred_relocation(
            source, destination, authority
        )
        digest = relocation.seal_post_import_relocation_handoff(
            result.operation_id,
            consumer=consumer,
            payload=undo,
            authority=authority,
        )
        with pytest.raises(relocation.PostImportRelocationUnavailable):
            relocation.acknowledge_post_import_relocation(
                result.operation_id,
                "0" * 64,
                authority=authority,
            )
        assert track.exists()
        relocation.acknowledge_post_import_relocation(
            result.operation_id,
            digest,
            authority=authority,
        )
    finally:
        authority.close()

    assert not track.exists()
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_persisted_handoff_refuses_to_undo_a_restored_database(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        result, consumer, undo = _deferred_relocation(
            source, destination, authority
        )
        digest = relocation.seal_post_import_relocation_handoff(
            result.operation_id,
            consumer=consumer,
            payload=undo,
            authority=authority,
        )
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE items SET path=?",
                (os.fsencode(track),),
            )
            connection.commit()
        finally:
            connection.close()
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=lambda operation_id, handoff: (
                operation_id == result.operation_id
                and handoff == {"consumer": consumer, "hash": digest}
            ),
        )
    finally:
        authority.close()

    assert (
        recovered.status
        is relocation.RelocationRecoveryStatus.ATTENTION_REQUIRED
    )
    assert track.read_bytes() == b"audio"
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(track)]
    assert list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_retained_queue_handoff_survives_ack_kill_until_exact_release(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        expected = relocation.capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
        result, consumer, digest = _retained_deferred_relocation(
            source, destination, authority, expected=expected
        )
    finally:
        authority.close()

    child = os.fork()
    if child == 0:
        try:
            child_authority = run_lock.acquire()

            def hard_kill(name):
                if name == "after-retained-completion":
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.acknowledge_post_import_relocation(
                result.operation_id,
                digest,
                authority=child_authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77

    exact_owner = lambda operation_id, handoff: (
        operation_id == result.operation_id
        and handoff == {"consumer": consumer, "hash": digest}
    )
    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=exact_owner,
        )
        assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
        assert list(data.glob(".qobuz_post_import_relocations/*.json"))

        # Repeating the exact acknowledgement is safe while the durable queue
        # action has not yet consumed the terminal receipt.
        relocation.acknowledge_post_import_relocation(
            result.operation_id,
            digest,
            authority=authority,
        )
        with pytest.raises(relocation.PostImportRelocationUnavailable):
            relocation.release_post_import_relocation(
                result.operation_id,
                "0" * 64,
                authority=authority,
            )
        relocation.release_post_import_relocation(
            result.operation_id,
            digest,
            authority=authority,
        )
        relocation.release_post_import_relocation(
            result.operation_id,
            digest,
            authority=authority,
        )
    finally:
        authority.close()

    assert not track.exists()
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def test_reconcile_commits_and_retains_an_exact_queue_handoff(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        result, consumer, digest = _retained_deferred_relocation(
            source, destination, authority
        )
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=lambda operation_id, handoff: (
                operation_id == result.operation_id
                and handoff == {"consumer": consumer, "hash": digest}
            ),
        )
        assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
        assert list(data.glob(".qobuz_post_import_relocations/*.json"))
        relocation.release_post_import_relocation(
            result.operation_id,
            digest,
            authority=authority,
        )
    finally:
        authority.close()

    assert not track.exists()
    assert (destination / track.name).read_bytes() == b"audio"
    assert _paths(database)[0] == [str(destination / track.name)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_relocation_expectation_refuses_source_or_destination_replacement(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        expected = relocation.capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
        assert relocation.canonical_post_import_relocation_expectation(
            expected,
            source=source,
            destination=destination,
        ) == expected

        displaced = source.with_name("Displaced")
        source.rename(displaced)
        source.mkdir()
        (source / track.name).write_bytes(b"audio")
        with pytest.raises(
            relocation.PostImportRelocationUnavailable,
            match="changed since the filing action was recorded",
        ):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
                expected=expected,
            )
    finally:
        authority.close()

    assert (source / track.name).read_bytes() == b"audio"
    assert (displaced / track.name).read_bytes() == b"audio"
    assert not destination.exists()
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))


@pytest.mark.parametrize("destination_existed", (False, True))
def test_relocation_expectation_refuses_destination_change(
    tmp_path, monkeypatch, destination_existed
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))
    if destination_existed:
        destination.mkdir(parents=True)
        (destination / "appeared.flac").write_bytes(b"other")

    authority = run_lock.acquire()
    try:
        expected = relocation.capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
        if destination_existed:
            destination.rename(destination.with_name("Displaced"))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "appeared.flac").write_bytes(b"other")
        with pytest.raises(
            relocation.PostImportRelocationUnavailable,
            match="changed since the filing action was recorded",
        ):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
                expected=expected,
            )
    finally:
        authority.close()

    assert track.read_bytes() == b"audio"
    assert (destination / "appeared.flac").read_bytes() == b"other"
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))


def test_retain_completion_requires_deferred_handoff(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        with pytest.raises(ValueError, match="retained completion"):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
                retain_completion=True,
            )
    finally:
        authority.close()


def test_legacy_handoff_journal_without_retention_field_still_cleans_up(
    tmp_path, monkeypatch
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        result, consumer, undo = _deferred_relocation(
            source, destination, authority
        )
        with relocation._THREAD_LOCK, relocation._locked_namespace(
            create=False
        ) as namespace_fd:
            payload = relocation._read_journal(
                namespace_fd, result.operation_id
            )
            payload.pop("retain_completion", None)
            relocation._write_journal(namespace_fd, payload)
        digest = relocation.seal_post_import_relocation_handoff(
            result.operation_id,
            consumer=consumer,
            payload=undo,
            authority=authority,
        )
        relocation.acknowledge_post_import_relocation(
            result.operation_id,
            digest,
            authority=authority,
        )
    finally:
        authority.close()

    assert not track.exists()
    assert (destination / track.name).read_bytes() == b"audio"
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
