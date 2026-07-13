"""Web path for single-track grabs: the Tracks search mode, the Get-track
download contract, and graduation (completing the album the normal way clears
any old single mark)."""
import pytest
from test_web import _remove_job, _wait_for, client  # noqa: F401 (fixture)

from qobuz_librarian.library import hidden
from qobuz_librarian.web import jobs as jm


def _owned_path(root, path):
    """Filesystem identity record written after a single-track import."""
    from qobuz_librarian.web.app import _bind_owned_path

    owned = _bind_owned_path(root, path)
    assert owned is not None
    return owned


def _directory_cleanup_entry(path, *, created, root=None):
    st = path.stat()
    entry = {
        "device": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "modified_ns": st.st_mtime_ns,
        "changed_ns": st.st_ctime_ns,
        "created": created,
    }
    if root is not None:
        entry["relative"] = path.relative_to(root).as_posix()
    return entry


def _ownership_manifest(root, path, *, created=()):
    def identity(target):
        st = target.stat()
        return {
            "device": st.st_dev,
            "inode": st.st_ino,
            "size": st.st_size,
            "modified_ns": st.st_mtime_ns,
            "changed_ns": st.st_ctime_ns,
        }

    return {
        "version": 1,
        "sealed": True,
        "root": str(root),
        "root_identity": identity(root),
        "items": [{
            "relative": path.relative_to(root).as_posix(),
            "file": identity(path),
            "created_directories": [
                {"relative": directory.relative_to(root).as_posix(),
                 **identity(directory)}
                for directory in created
            ],
            "companions": [],
        }],
    }


@pytest.fixture
def fresh_singles(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(job_persistence, "persist", lambda _job: True)


def test_binding_requires_the_full_created_directory_record(tmp_path):
    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    album = music_root / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01 - Track.flac"
    track.write_bytes(b"audio")
    manifest = _ownership_manifest(music_root, track, created=[album])
    item = manifest["items"][0]

    assert app_mod._bind_owned_path(
        music_root,
        track,
        expected_file=item["file"],
        expected_root=manifest["root_identity"],
        created_directories=item["created_directories"],
    ) is not None

    stale = [dict(item["created_directories"][0])]
    stale[0]["changed_ns"] += 1
    assert app_mod._bind_owned_path(
        music_root,
        track,
        expected_file=item["file"],
        expected_root=manifest["root_identity"],
        created_directories=stale,
    ) is None


def test_created_artwork_and_sidecar_remain_exactly_undoable(
        tmp_path, monkeypatch):
    import copy
    import os

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch, lyrics
    from qobuz_librarian.queue import executor
    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    album = music_root / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01 - Track.flac"
    cover = album / "cover.jpg"
    track.write_bytes(b"audio with embedded lyrics")
    cover.write_bytes(b"artwork")
    manifest = _ownership_manifest(music_root, track, created=[album])
    manifest["items"][0]["companions"] = [{
        "kind": "artwork",
        "relative": cover.relative_to(music_root).as_posix(),
        "file": app_mod._owned_file_identity(cover.stat()),
    }]
    item = {
        "_resolved_post_dir": album,
        "_import_ownership": manifest,
    }

    class FakeFLAC:
        def __init__(self, _path):
            self.tags = {"lyrics": ["[00:01.00]words"]}

    def identity(path):
        return app_mod._owned_file_identity(path.stat())

    def write_sidecar(
            path, content, *, creation_out=None,
            directory_mutation_out=None, sidecar_identity_out=None,
            **_kwargs):
        sidecar = path.with_suffix(".lrc")
        sidecar.write_text(content, encoding="utf-8")
        receipt = {
            "path": os.path.abspath(sidecar),
            "file": identity(sidecar),
        }
        if isinstance(creation_out, dict):
            creation_out.update(receipt)
        if isinstance(sidecar_identity_out, dict):
            sidecar_identity_out.update(receipt)
        return True

    def replace_tags(
            _flac, path, *, identity_change_out=None,
            directory_mutation_out=None, **_kwargs):
        before = identity(path)
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(path.read_bytes() + b" stripped")
        os.replace(replacement, path)
        if isinstance(identity_change_out, dict):
            identity_change_out.update({
                "path": os.path.abspath(path),
                "before": before,
                "after": identity(path),
            })

    monkeypatch.setattr(cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "sidecar")
    monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
    monkeypatch.setattr(lyric_fetch, "FLAC", FakeFLAC)
    monkeypatch.setattr(lyric_fetch, "write_sidecar", write_sidecar)
    monkeypatch.setattr(lyric_fetch, "save_flac_tags", replace_tags)

    changes, created, directory_changes = [], [], []
    lyrics.write_post_import_sidecars(
        [album],
        identity_changes_out=changes,
        created_files_out=created,
        directory_changes_out=directory_changes,
    )

    assert len(changes) == 1
    assert len(created) == 1
    assert directory_changes
    assert app_mod._single_owned_path(manifest, album) is None
    executor._advance_import_ownership_identities(
        [item], changes, created, directory_changes)
    binding = app_mod._single_owned_path(
        manifest,
        album,
        created_files_after_import=item["_import_ownership_created_files"],
    )
    assert binding is not None
    owned, bound_track = binding
    sidecar = track.with_suffix(".lrc")
    assert bound_track == track
    assert [entry["relative"] for entry in owned["companions"]] == [
        cover.relative_to(music_root).as_posix(),
        sidecar.relative_to(music_root).as_posix()
    ]
    changed_companion = copy.deepcopy(owned)
    changed_companion["companions"][0]["file"]["size"] += 1
    assert app_mod._unlink_owned_path(music_root, changed_companion) is None
    assert track.exists() and cover.exists() and sidecar.exists()
    assert app_mod._unlink_owned_path(music_root, owned) == track
    assert not track.exists()
    assert not cover.exists()
    assert not sidecar.exists()
    assert not album.exists()


def test_post_import_rewrite_cannot_redirect_undo_through_a_hardlink(
        tmp_path, monkeypatch):
    import os

    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor
    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    album = music_root / "Artist" / "Album"
    album.mkdir(parents=True)
    owned_track = album / "01 - Owned.flac"
    other_track = album / "02 - Existing.flac"
    owned_track.write_bytes(b"shared audio")
    os.link(owned_track, other_track)
    manifest = _ownership_manifest(music_root, owned_track)
    before = dict(manifest["items"][0]["file"])

    replacement = album / ".replacement"
    replacement.write_bytes(b"rewritten other track")
    os.replace(replacement, other_track)
    change = {
        "path": os.path.abspath(other_track),
        "before": before,
        "after": app_mod._owned_file_identity(other_track.stat()),
    }
    item = {"_resolved_post_dir": album, "_import_ownership": manifest}
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music_root)

    executor._advance_import_ownership_identities([item], [change])
    binding = app_mod._single_owned_path(manifest, album)

    if binding is not None:
        owned, bound_track = binding
        assert bound_track == owned_track
        assert app_mod._unlink_owned_path(music_root, owned) == owned_track
        assert not owned_track.exists()
    else:
        assert owned_track.exists()
    assert other_track.read_bytes() == b"rewritten other track"

    second_album = music_root / "Artist" / "Other Album"
    second_album.mkdir()
    missing_owned = second_album / "01 - Owned.flac"
    existing_link = second_album / "02 - Existing.flac"
    missing_owned.write_bytes(b"shared")
    os.link(missing_owned, existing_link)
    missing_manifest = _ownership_manifest(music_root, missing_owned)
    missing_owned.unlink()

    assert app_mod._single_owned_path(
        missing_manifest, second_album) is None
    assert existing_link.read_bytes() == b"shared"


def test_undo_resumes_its_own_partial_companion_deletion(
        tmp_path, monkeypatch):
    import copy
    import json
    import os

    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    artist = music_root / "Artist"
    album = artist / "Album"
    album.mkdir(parents=True)
    track = album / "01 - Track.flac"
    sidecar = track.with_suffix(".lrc")
    track.write_bytes(b"audio")
    sidecar.write_text("lyrics", encoding="utf-8")
    created_album = _directory_cleanup_entry(
        album, created=True, root=music_root)
    owned = app_mod._bind_owned_path(
        music_root, track, created_directories=[created_album])
    companion = app_mod._bind_owned_path(
        music_root, sidecar, created_directories=[created_album])
    assert owned is not None and companion is not None
    companion["kind"] = "lyrics"
    owned["companions"] = [companion]

    persisted = []

    def persist_progress():
        persisted.append(copy.deepcopy(owned))
        return True

    class SimulatedProcessExit(BaseException):
        pass

    real_unlink = os.unlink
    interrupted = False

    def unlink_then_exit(path, *args, **kwargs):
        nonlocal interrupted
        result = real_unlink(path, *args, **kwargs)
        if path == "held" and not interrupted:
            interrupted = True
            raise SimulatedProcessExit
        return result

    monkeypatch.setattr(os, "unlink", unlink_then_exit)

    with pytest.raises(SimulatedProcessExit):
        app_mod._unlink_owned_path(
            music_root, owned, progress=persist_progress)
    assert track.exists() and not sidecar.exists() and album.is_dir()

    restarted = json.loads(json.dumps(persisted[-1]))
    assert restarted["companions"][0]["deletion"]["state"] == "held"
    cleanup_album = restarted["directory_cleanup"]["directories"][-1]
    assert app_mod._directory_cleanup_entry_matches(
        album.stat(), cleanup_album)

    assert app_mod._unlink_owned_path(music_root, restarted) == track
    assert not album.exists()
    assert artist.is_dir()


def test_undo_retries_an_exact_leaf_from_quarantine_after_unlink_failure(
        tmp_path, monkeypatch):
    import fcntl
    import os

    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    album = music_root / "Artist" / "Album"
    disc = album / "Disc 2"
    disc.mkdir(parents=True)
    track = disc / "01 - Track.flac"
    track.write_bytes(b"audio")
    created_album = _directory_cleanup_entry(
        album, created=True, root=music_root)
    created_disc = _directory_cleanup_entry(
        disc, created=True, root=music_root)
    owned = app_mod._bind_owned_path(
        music_root,
        track,
        created_directories=[created_album, created_disc],
    )
    assert owned is not None

    real_unlink = os.unlink
    real_acquire = app_mod.acquire_inode_write_exclusion
    exclusions = []
    fail_once = True

    def capture_lease(descriptor):
        exclusion = real_acquire(descriptor)
        exclusions.append(exclusion)
        return exclusion

    def reject_first_held_unlink(path, *args, **kwargs):
        nonlocal fail_once
        if path == "held":
            assert exclusions and exclusions[-1].intact()
            if fail_once:
                fail_once = False
                raise OSError("injected held-file unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        app_mod, "acquire_inode_write_exclusion", capture_lease)
    monkeypatch.setattr(os, "unlink", reject_first_held_unlink)

    assert app_mod._unlink_owned_path(music_root, owned) is None
    assert not track.exists()
    assert owned["deletion"]["state"] == "held"
    held = disc / owned["deletion"]["quarantine"] / "held"
    assert held.read_bytes() == b"audio"
    assert app_mod._ownership_identity_matches(held.stat(), owned["file"])
    assert app_mod._directory_cleanup_entry_matches(
        disc.stat(), owned["directory_cleanup"]["directories"][-1])

    assert app_mod._unlink_owned_path(music_root, owned) == track
    assert not album.exists()


def test_undo_refuses_a_leaf_held_open_for_writing(tmp_path, monkeypatch):
    import os

    from qobuz_librarian.web import app as app_mod

    album = tmp_path / "Album"
    album.mkdir()
    track = album / "01 - Track.flac"
    original = b"ORIGINAL"
    track.write_bytes(original)
    owned = app_mod._bind_owned_path(album, track)
    assert owned is not None

    writer_fd = os.open(track, os.O_RDWR)
    real_rename = app_mod._ownership_rename_noreplace
    edit_reached = False

    def edit_at_rename(first_fd, first, second_fd, second):
        nonlocal edit_reached
        if not edit_reached and first == track.name and second == "held":
            edit_reached = True
            before = os.fstat(writer_fd)
            os.pwrite(writer_fd, b"EDITED!!", 0)
            os.ftruncate(writer_fd, len(original))
            os.utime(
                track,
                ns=(before.st_atime_ns, before.st_mtime_ns),
                follow_symlinks=False,
            )
        return real_rename(first_fd, first, second_fd, second)

    monkeypatch.setattr(
        app_mod, "_ownership_rename_noreplace", edit_at_rename)
    try:
        assert app_mod._unlink_owned_path(album, owned) is None
    finally:
        os.close(writer_fd)

    assert edit_reached is False
    assert track.read_bytes() == original
    assert "deletion" not in owned
    assert not any(path.name.startswith(".ql-undo-file-") for path in album.iterdir())


def test_undo_finishes_when_a_created_folder_contains_an_unowned_file(
        tmp_path):
    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    album = music_root / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01 - Track.flac"
    booklet = album / "booklet.pdf"
    track.write_bytes(b"audio")
    booklet.write_bytes(b"booklet")
    created_album = _directory_cleanup_entry(
        album, created=True, root=music_root)
    owned = app_mod._bind_owned_path(
        music_root, track, created_directories=[created_album])
    assert owned is not None

    assert app_mod._unlink_owned_path(music_root, owned) == track
    assert not track.exists()
    assert booklet.read_bytes() == b"booklet"
    assert owned["directory_cleanup"]["complete"] is True


def test_undo_resumes_every_created_ancestor_after_nested_cleanup_crash(
        tmp_path):
    import copy

    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    disc = music_root / "Artist" / "Album" / "Disc 2"
    disc.mkdir(parents=True)
    track = disc / "01 - Track.flac"
    track.write_bytes(b"audio")
    created = [
        _directory_cleanup_entry(
            directory, created=True, root=music_root)
        for directory in (disc.parents[1], disc.parent, disc)
    ]
    owned = app_mod._bind_owned_path(
        music_root, track, created_directories=created)
    assert owned is not None

    class SimulatedProcessExit(BaseException):
        pass

    durable = None

    def persist_progress():
        nonlocal durable
        durable = copy.deepcopy(owned)
        deepest = owned["directory_cleanup"]["directories"][-1]
        if deepest.get("deletion", {}).get("state") == "held":
            raise SimulatedProcessExit
        return True

    with pytest.raises(SimulatedProcessExit):
        app_mod._unlink_owned_path(
            music_root, owned, progress=persist_progress)
    assert durable is not None
    assert not disc.exists()

    assert app_mod._unlink_owned_path(music_root, durable) == track
    assert not (music_root / "Artist").exists()
    assert durable["directory_cleanup"]["complete"] is True


def test_undo_never_removes_a_directory_swapped_at_quarantine(
        tmp_path, monkeypatch):
    import os

    from qobuz_librarian.web import app as app_mod

    music_root = tmp_path / "music"
    artist = music_root / "Artist"
    album = artist / "Album"
    album.mkdir(parents=True)
    track = album / "01 - Track.flac"
    track.write_bytes(b"audio")
    created_album = _directory_cleanup_entry(
        album, created=True, root=music_root)
    owned = app_mod._bind_owned_path(
        music_root, track, created_directories=[created_album])
    assert owned is not None

    parked = artist / "parked-owned-album"
    real_rename = app_mod._ownership_rename_noreplace
    swapped = False

    def swap_public_directory(first_fd, first, second_fd, second):
        nonlocal swapped
        if first == album.name and not swapped:
            os.rename(first, parked.name, src_dir_fd=first_fd, dst_dir_fd=first_fd)
            os.mkdir(first, dir_fd=first_fd)
            swapped = True
        return real_rename(first_fd, first, second_fd, second)

    monkeypatch.setattr(
        app_mod, "_ownership_rename_noreplace", swap_public_directory)

    assert app_mod._unlink_owned_path(music_root, owned) is None
    assert swapped is True
    assert parked.is_dir()
    assert album.is_dir()


def test_undo_never_unlinks_a_late_public_replacement(tmp_path, monkeypatch):
    import os

    from qobuz_librarian.web import app as app_mod

    album = tmp_path / "Album"
    album.mkdir()
    track = album / "01 - Track.flac"
    track.write_bytes(b"owned")
    owned = _owned_path(album, track)
    real_rename = app_mod._ownership_rename_noreplace
    injected = False

    def swap_before_quarantine(first_fd, first, second_fd, second):
        nonlocal injected
        if not injected and first == track.name:
            os.rename(
                first,
                "parked",
                src_dir_fd=first_fd,
                dst_dir_fd=first_fd,
            )
            replacement_fd = os.open(
                first, os.O_CREAT | os.O_EXCL | os.O_WRONLY, dir_fd=first_fd)
            os.write(replacement_fd, b"injected")
            os.close(replacement_fd)
            injected = True
        return real_rename(first_fd, first, second_fd, second)

    monkeypatch.setattr(
        app_mod, "_ownership_rename_noreplace", swap_before_quarantine)

    assert app_mod._unlink_owned_path(album, owned) is None
    assert track.read_bytes() == b"injected"
    assert (album / "parked").read_bytes() == b"owned"


def test_single_ownership_is_persisted_before_library_refresh(
        client, monkeypatch, fresh_singles, tmp_path):
    import copy

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod
    import qobuz_librarian.web.flows as flows_mod
    from qobuz_librarian.web import job_persistence

    music_root = tmp_path / "music"
    album_dir = music_root / "Allie X" / "Girl With No Face (2024)"
    album_dir.mkdir(parents=True)
    track_path = album_dir / "03 - Black Eye.flac"
    track_path.write_bytes(b"downloaded audio")
    album = {
        "id": "alb1", "title": "Girl With No Face", "year": 2024,
        "artist": {"name": "Allie X"},
        "tracks": {"items": [
            {"id": "trk7", "title": "Black Eye", "track_number": 3},
            {"id": "trk8", "title": "Galina", "track_number": 4},
        ]},
    }
    monkeypatch.setattr(app_mod.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda *_args: album)
    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *a, **k: ([], None))

    def fake_exec(queue, *args, **kwargs):
        item = queue[0]
        item.update({
            "n_ok": 1,
            "n_fail": 0,
            "imported": True,
            "_resolved_post_dir": album_dir,
            "_import_ownership": _ownership_manifest(
                music_root, track_path, created=[album_dir]),
        })

    events = []

    def capture(job):
        events.append(("persist", copy.deepcopy(job.single)))

    def fail_refresh(*args, **kwargs):
        events.append(("refresh", None))
        raise OSError("forced refresh failure")

    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)
    monkeypatch.setattr(job_persistence, "persist", capture)
    monkeypatch.setattr(flows_mod, "_refresh_after_local_album_change", fail_refresh)
    monkeypatch.setattr(
        app_mod.cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False, raising=False)

    jm.start_worker()
    response = client.post(
        "/download",
        data={"album_id": "alb1", "track_id": "trk7"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)
    job = [
        candidate for candidate in list(jm.registry._jobs.values())
        if getattr(candidate, "album_id", None) == "alb1"
    ][0]
    try:
        assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
        refresh_index = next(
            index for index, event in enumerate(events) if event[0] == "refresh")
        persisted_index, persisted = next(
            (index, event[1])
            for index, event in enumerate(events)
            if event[0] == "persist"
            and isinstance(event[1], dict)
            and event[1].get("owned_path") is not None
        )
        assert persisted_index < refresh_index
        assert persisted["album_id"] == "alb1"
        assert persisted["track_id"] == "trk7"
        assert persisted["dir"] == str(album_dir)
        detail = client.get(f"/jobs/{job.id}")
        assert detail.status_code == 200
        assert "Undo (removes track)" in detail.text
    finally:
        _remove_job(job)


def test_get_track_downloads_one_without_hiding_album_gaps_by_default(
        client, monkeypatch, fresh_singles):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face", "year": 2024,
        "artist": {"name": "Allie X"},
        "tracks": {"items": [
            {"id": "trk7", "title": "Black Eye", "track_number": 3},
            {"id": "trk8", "title": "Galina", "track_number": 4},
            {"id": "trk9", "title": "Off With Her Tits", "track_number": 5}]}})
    # own none of it, so the grabbed track leaves the album partial -> a single
    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *a, **k: ([], None))

    def fake_exec(queue, *a, **k):
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    monkeypatch.setattr(app_mod.cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False, raising=False)
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk7"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    jobs = [j for j in list(jm.registry._jobs.values())
            if getattr(j, "album_id", None) == "alb1"]
    assert len(jobs) == 1
    job = jobs[0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
        assert "stays out of scans" not in job.summary
    finally:
        _remove_job(job)


def test_get_track_can_hide_album_gaps_when_setting_is_enabled(
        client, monkeypatch, fresh_singles):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(app_mod.cfg, "SUPPRESS_SINGLE_TRACK_GAPS", True, raising=False)
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face", "year": 2024,
        "artist": {"name": "Allie X"},
        "tracks": {"items": [
            {"id": "trk7", "title": "Black Eye", "track_number": 3},
            {"id": "trk8", "title": "Galina", "track_number": 4}]}})
    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *a, **k: ([], None))

    def fake_exec(queue, *a, **k):
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk7"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    job = [j for j in list(jm.registry._jobs.values())
           if getattr(j, "album_id", None) == "alb1"][0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is True
    finally:
        _remove_job(job)


def test_undo_removes_the_grabbed_track_and_clears_the_mark(client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod
    import qobuz_librarian.web.app as app_mod
    import qobuz_librarian.web.flows as flows_mod

    d = tmp_path / "Allie X" / "Girl With No Face (2024)"
    d.mkdir(parents=True)
    f = d / "03 - Black Eye.flac"
    f.write_bytes(b"flac")
    cover = d / "cover.jpg"
    cover.write_bytes(b"older artwork")
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")
    refresh_calls = []

    job = jm.Job(title="Black Eye", artist="Allie X", album_id="alb1")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb1", "track_id": "trk7", "dir": str(d),
                  "isrc": "ISRC1", "track_no": 3, "title": "Black Eye",
                  "artist": "Allie X", "album": "Girl With No Face",
                  "marked": True, "new_folder": True,
                  "owned_path": _owned_path(d, f)}
    jm.registry.add(job)
    monkeypatch.setattr(app_mod, "_get_optional_token", lambda: "tok")
    monkeypatch.setattr(
        flows_mod,
        "_refresh_after_local_album_change",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(scanner_mod, "read_album_dir",
                        lambda _d: [{"path": str(f), "isrc": "ISRC1", "track": 3}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert not f.exists()  # the grabbed track is gone
        assert cover.read_bytes() == b"older artwork"
        assert d.is_dir()
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
        assert job.single.get("removed") is True
        assert len(refresh_calls) == 1
        args, kwargs = refresh_calls[0]
        assert args[0]["title"] == "Girl With No Face"
        assert args[0]["artist"]["name"] == "Allie X"
        assert args[1]["dir"] == str(d)
        assert kwargs["fallback_artist"] == "Allie X"
        assert kwargs["token"] == "tok"
        assert kwargs["upgrade"] is True
        assert kwargs["downsample"] is True
    finally:
        _remove_job(job)


def test_undo_refuses_a_replacement_when_the_inode_is_reused(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod

    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    track = d / "01 - Track.flac"
    track.write_bytes(b"downloaded copy")
    owned = _owned_path(d, track)

    track.unlink()
    replacement = b"my curated replacement audio"
    track.write_bytes(replacement)
    replacement_stat = track.stat()
    # Model the normal delete-then-create case where the filesystem recycles
    # the old inode. The rest of the original fingerprint must still expose
    # that these are different bytes.
    owned["file"]["device"] = replacement_stat.st_dev
    owned["file"]["inode"] = replacement_stat.st_ino

    job = jm.Job(title="Track", artist="Artist", album_id="album")
    job.status = jm.JobStatus.DONE
    job.single = {
        "dir": str(d), "track_id": "track", "title": "Track",
        "artist": "Artist", "album": "Album", "marked": False,
        "owned_path": owned,
    }
    jm.registry.add(job)
    forgotten = []
    monkeypatch.setattr(
        beets_mod, "forget_beets_entries",
        lambda paths: forgotten.extend(paths),
    )
    try:
        response = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert response.status_code in (200, 303)
        assert track.read_bytes() == replacement
        assert not job.single.get("removed")
        assert forgotten == []
    finally:
        _remove_job(job)




def test_undo_already_gone_clears_single_mark_and_refreshes_state(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.library.scanner as scanner_mod
    import qobuz_librarian.web.app as app_mod
    import qobuz_librarian.web.flows as flows_mod

    d = tmp_path / "Allie X" / "Girl With No Face (2024)"
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")
    refresh_calls = []

    job = jm.Job(title="Black Eye", artist="Allie X", album_id="alb1")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb1", "track_id": "trk7", "dir": str(d),
                  "isrc": "ISRC1", "track_no": 3, "title": "Black Eye",
                  "artist": "Allie X", "album": "Girl With No Face",
                  "marked": True, "new_folder": False}
    jm.registry.add(job)
    monkeypatch.setattr(app_mod, "_get_optional_token", lambda: "tok")
    monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [])
    monkeypatch.setattr(
        flows_mod,
        "_refresh_after_local_album_change",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)),
    )
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
        assert job.single.get("removed") is True
        assert len(refresh_calls) == 1
    finally:
        _remove_job(job)


def test_undo_refuses_a_bound_track_missing_before_undo(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.web.app as app_mod
    import qobuz_librarian.web.flows as flows_mod

    d = tmp_path / "Allie X" / "Girl With No Face (2024)"
    d.mkdir(parents=True)
    track = d / "03 - Black Eye.flac"
    track.write_bytes(b"downloaded copy")
    owned = _owned_path(d, track)
    track.unlink()
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")
    refresh_calls = []
    forgotten = []

    job = jm.Job(title="Black Eye", artist="Allie X", album_id="alb1")
    job.status = jm.JobStatus.DONE
    job.single = {
        "album_id": "alb1", "track_id": "trk7", "dir": str(d),
        "title": "Black Eye", "artist": "Allie X",
        "album": "Girl With No Face", "marked": True,
        "owned_path": owned,
    }
    jm.registry.add(job)
    monkeypatch.setattr(app_mod, "_get_optional_token", lambda: "tok")
    monkeypatch.setattr(
        flows_mod,
        "_refresh_after_local_album_change",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        beets_mod, "forget_beets_entries",
        lambda paths: forgotten.extend(paths),
    )
    try:
        response = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert response.status_code in (200, 303)
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is True
        assert not job.single.get("removed")
        assert "Couldn't safely verify" in job.summary
        assert refresh_calls == []
        assert forgotten == []
    finally:
        _remove_job(job)


def test_undo_refuses_a_replaced_parent_symlink(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod

    d = tmp_path / "Artist" / "Box Set (2020)"
    disc = d / "Disc 1"
    disc.mkdir(parents=True)
    grabbed = disc / "03 - Grabbed.flac"
    grabbed.write_bytes(b"flac")
    owned = _owned_path(d, grabbed)

    # Move the recorded directory, then put a symlink at its old name. The leaf
    # is still the same inode, so checking only the file would delete outside
    # the album tree; the complete no-follow directory chain must also match.
    outside = tmp_path / "outside-disc"
    disc.rename(outside)
    disc.symlink_to(outside, target_is_directory=True)

    job = jm.Job(title="Grabbed", artist="Artist", album_id="alb9")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb9", "track_id": "trk3", "dir": str(d),
                  "isrc": "ISRCG", "track_no": 3, "title": "Grabbed",
                  "artist": "Artist", "album": "Box Set",
                  "marked": False, "new_folder": False,
                  "owned_path": owned}
    jm.registry.add(job)
    monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [
        {"path": str(disc / grabbed.name), "isrc": "ISRCG", "track": 3}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert (outside / grabbed.name).read_bytes() == b"flac"
        assert not job.single.get("removed")
    finally:
        _remove_job(job)


def test_undo_no_isrc_removes_the_grabbed_disc_not_a_same_numbered_twin(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    music_root = tmp_path / "music"
    d = music_root / "By Genre" / "Classical" / "Artist" / "Box Set (2020)"
    cd1 = d / "Disc 1"
    cd2 = d / "Disc 2"
    cd1.mkdir(parents=True)
    cd1_twin = cd1 / "03 - Disc One Three.flac"
    cd2_grabbed = cd2 / "03 - Disc Two Three.flac"
    cd1_twin.write_bytes(b"cd1")

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(app_mod.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "albx", "title": "Box Set", "year": 2020,
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "cd1t3", "title": "Disc One Three",
             "track_number": 3, "media_number": 1},
            {"id": "cd2t3", "title": "Disc Two Three",
             "track_number": 3, "media_number": 2}]}})
    existing = [{
        "path": str(cd1_twin), "title": "Disc One Three", "isrc": "",
        "tracknumber": 3, "discnumber": 1,
    }]
    monkeypatch.setattr(
        cat_mod, "find_existing_tracks", lambda *a, **k: (existing, d))

    def fake_exec(queue, *a, **k):
        cd2.mkdir()
        cd2_grabbed.write_bytes(b"cd2")
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
        queue[0]["_resolved_post_dir"] = str(d)
        queue[0]["_import_ownership"] = _ownership_manifest(
            music_root,
            cd2_grabbed,
            created=[cd2],
        )
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    r = client.post("/download", data={"album_id": "albx", "track_id": "cd2t3"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    job = [j for j in list(jm.registry._jobs.values())
           if getattr(j, "album_id", None) == "albx"][0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert job.single.get("disc_no") == 2
        assert job.single.get("owned_path")

        sibling_album = d.parent / "Other Album"
        sibling_album.mkdir()
        monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
        client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert not cd2_grabbed.exists()
        assert not cd2.exists()
        assert d.is_dir()
        assert sibling_album.is_dir()
        assert cd1.is_dir()
        assert cd1_twin.exists()
    finally:
        _remove_job(job)


def test_undo_with_no_isrc_or_track_number_deletes_nothing(
        client, monkeypatch, fresh_singles, tmp_path):
    # Neither an ISRC nor a track number to match on: two missing values must not
    # read as equal and delete an arbitrary track.
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod

    d = tmp_path / "Artist" / "Album (2024)"
    d.mkdir(parents=True)
    t = d / "01 - A.flac"
    t.write_bytes(b"flac")

    job = jm.Job(title="A", artist="Artist", album_id="alb8")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb8", "track_id": "t1", "dir": str(d),
                  "isrc": "", "track_no": None, "title": "A",
                  "artist": "Artist", "album": "Album",
                  "marked": False, "new_folder": False}
    jm.registry.add(job)
    monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [
        {"path": str(t), "isrc": "", "tracknumber": 1}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert t.exists()
    finally:
        _remove_job(job)
