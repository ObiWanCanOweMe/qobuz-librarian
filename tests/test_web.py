"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""
import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

import httpx
import pytest

from qobuz_librarian.web import jobs as jm

# ── jobs.py: Job ──────────────────────────────────────────────────────────────


def test_log_lines_capped_with_truncation_marker():
    job = jm.Job()
    total = jm.Job.LOG_CAP + jm.Job._LOG_SLACK + 1
    for i in range(total):
        job.push_line(f"line{i}")
    assert len(job.log_lines) == jm.Job.LOG_CAP
    assert job.log_lines[0] == jm.Job._TRUNCATION_MARKER
    assert job.log_lines[-1] == f"line{total - 1}"


# ── jobs.py: worker loop ──────────────────────────────────────────────────────

def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _repair_recovery_record(location, receipt=None):
    return {
        "version": 1,
        "kind": "repair-backup",
        "status": "retained",
        "location": str(location),
        "album_dir": "/music/Artist/Album",
        "stage": "refill",
        "reason": "Repair stopped while downloading the replacement.",
        "complete": True,
        "requested": 1,
        "backed_up": 1,
        "receipt": receipt,
    }


def test_staging_lock_serialises_lane_album_work():
    """Both lanes interleave at the album level: only one rip+import at a
    time, even with two workers running. Guards against /staging races and
    beets' SQLite write lock."""
    import threading

    jm.start_worker()
    inside = threading.Event()
    release = threading.Event()
    second_inside = threading.Event()

    holder = jm.Job(title="lock holder")
    holder.kind = "scan"

    def _hold(j):
        with jm.staging_lock():
            inside.set()
            release.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold))
    assert inside.wait(timeout=5)

    contender = jm.Job(title="lock contender")
    contender_started = threading.Event()

    def _grab(j):
        contender_started.set()   # worker picked up the job; now it blocks on the lock
        with jm.staging_lock():
            second_inside.set()

    jm.submit(contender, _grab)
    # Wait until the worker has actually entered _grab (it is now blocking on
    # staging_lock, which the holder still owns).  Only then assert it can't
    # proceed — otherwise a slow scheduler means the assertion is vacuous.
    assert contender_started.wait(timeout=5), "download-lane worker never picked up contender"
    assert not second_inside.wait(timeout=0.3)
    release.set()
    assert second_inside.wait(timeout=5)


def test_staging_entry_rechecks_recovery_after_waiting_between_worker_lanes(
        monkeypatch):
    import threading

    recovery_owner = {"job_id": None, "album_id": None}

    def _entry_allowed(job):
        if recovery_owner["job_id"] is None:
            return True
        return (
            job is not None
            and job.id == recovery_owner["job_id"]
            and job.album_id == recovery_owner["album_id"]
        )

    jm.configure_staging_entry_guard(_entry_allowed)
    monkeypatch.setattr(jm.job_persistence, "persist", lambda _job: True)
    jm.start_worker()
    holder_inside = threading.Event()
    release_holder = threading.Event()
    blocked_started = threading.Event()
    blocked_entered = threading.Event()

    holder = jm.Job(title="scan lane holder")

    def _hold(_job):
        with jm.staging_lock():
            holder_inside.set()
            release_holder.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold))
    assert holder_inside.wait(timeout=5)

    blocked = jm.Job(id="other-job", title="already admitted", album_id="other")

    def _blocked(_job):
        blocked_started.set()
        with jm.staging_lock():
            blocked_entered.set()

    jm.submit(blocked, _blocked)
    assert blocked_started.wait(timeout=5)
    recovery_owner.update(job_id="exact-resume", album_id="album-1")
    release_holder.set()
    assert _wait_for(lambda: blocked.status is jm.JobStatus.FAILED)
    assert not blocked_entered.is_set()

    resumed = jm.Job(
        id="exact-resume", title="exact resume", album_id="album-1")
    resumed_entered = threading.Event()

    def _resume(_job):
        with jm.staging_lock():
            resumed_entered.set()

    jm.submit(resumed, _resume)
    assert _wait_for(lambda: resumed.status is jm.JobStatus.DONE)
    assert resumed_entered.is_set()

    jm.configure_staging_entry_guard(None)
    lock = jm.staging_lock()
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_scan_job_parks_for_review_then_executes():
    jm.start_worker()
    executed = {}

    def scan(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.add_candidate("album", "Album B", "Artist", payload={"id": 2})

    def execute(j, chosen):
        executed["ids"] = [c["payload"]["id"] for c in chosen]

    job = jm.Job(title="scan")
    jm.submit_scan(job, scan, execute)
    assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
    assert len(job.candidates) == 2
    assert jm.approve(job, ["c1"])
    assert _wait_for(lambda: job.status == jm.JobStatus.DONE)
    assert executed["ids"] == [2]


def test_failed_job_resubmit_reuses_exact_registry_job_and_is_idempotent(
        monkeypatch):
    import queue

    from qobuz_librarian.web import jobs as jobs_mod

    registry = jobs_mod.JobRegistry()
    work = queue.Queue()
    job = jobs_mod.Job(id="exact-job", title="Interrupted")
    job.status = jobs_mod.JobStatus.FAILED
    job.error = "Interrupted by a restart."
    registry.add(job)

    saved = []
    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_download_queue", work)
    monkeypatch.setattr(
        jobs_mod.job_persistence,
        "persist",
        lambda value: saved.append(value.status) or True,
    )
    run = lambda _job: None

    assert jobs_mod.resubmit_failed(job, run) is True
    assert job.status is jobs_mod.JobStatus.PENDING
    assert job.error is None
    assert work.get_nowait() == (job, run)
    assert saved == [jobs_mod.JobStatus.PENDING]
    assert jobs_mod.resubmit_failed(job, run) is False
    assert work.empty()

    impostor = jobs_mod.Job(id=job.id)
    impostor.status = jobs_mod.JobStatus.FAILED
    assert jobs_mod.resubmit_failed(impostor, run) is False


def test_submit_refuses_job_without_durable_admission(monkeypatch):
    import queue

    from qobuz_librarian.web import jobs as jobs_mod

    registry = jobs_mod.JobRegistry()
    work = queue.Queue()
    job = jobs_mod.Job(id="unsaved-job", title="Album", album_id="album-1")
    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_download_queue", work)
    monkeypatch.setattr(
        jobs_mod.job_persistence,
        "admit",
        lambda _job: False,
        raising=False,
    )

    assert jobs_mod.submit(job, lambda _job: None) is None
    assert registry.get(job.id) is None
    assert work.empty()


def test_submit_scan_refuses_job_without_durable_admission(monkeypatch):
    import queue

    from qobuz_librarian.web import jobs as jobs_mod

    registry = jobs_mod.JobRegistry()
    work = queue.Queue()
    job = jobs_mod.Job(id="unsaved-scan", title="Library scan")
    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_scan_queue", work)
    monkeypatch.setattr(jobs_mod.job_persistence, "admit", lambda _job: False)

    assert jobs_mod.submit_scan(job, lambda _job: None, lambda *_: None) is None
    assert registry.get(job.id) is None
    assert work.empty()


def test_review_admission_failure_keeps_original_review(monkeypatch):
    import queue

    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as jobs_mod

    registry = jobs_mod.JobRegistry()
    work = queue.Queue()
    job = jobs_mod.Job(id="saved-review", title="Library scan")
    job.execute_kind = "library"
    job.status = jobs_mod.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda *_: None
    job.add_candidate("album", "Picked", "Artist", selected=True)
    job.add_candidate("album", "Waiting", "Artist", selected=False)
    registry.add(job)
    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_scan_queue", work)
    monkeypatch.setattr(
        jobs_mod.job_persistence,
        "admit_review_transition",
        lambda *_args, **_kwargs: False,
    )

    result = jobs_mod.approve(
        job,
        split_review=lambda review: webapp._build_unapproved_review(review, ""),
    )

    assert result is None
    assert job.status is jobs_mod.JobStatus.AWAITING_REVIEW
    assert [candidate["title"] for candidate in job.candidates] == [
        "Picked", "Waiting"
    ]
    assert registry.all() == [job]
    assert work.empty()


def test_worker_rechecks_durable_admission_before_running(monkeypatch):
    import threading

    from qobuz_librarian.web import jobs as jobs_mod

    stop = threading.Event()
    ran = threading.Event()
    job = jobs_mod.Job(id="lost-before-run", title="Album", album_id="album-1")
    registry = jobs_mod.JobRegistry()
    registry.add(job)

    class OneItemQueue:
        def get(self, timeout):
            stop.set()
            return job, lambda _job: ran.set()

        def task_done(self):
            pass

    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_stop_event", stop)
    monkeypatch.setattr(
        jobs_mod.job_persistence,
        "admit",
        lambda _job: False,
        raising=False,
    )

    jobs_mod._worker_loop(OneItemQueue())

    assert not ran.is_set()
    assert job.status is jobs_mod.JobStatus.FAILED
    assert "data folder" in (job.error or "")


def test_worker_durably_finalizes_an_arbitrary_base_exception(monkeypatch):
    from qobuz_librarian.web import jobs as jobs_mod

    class SimulatedFatalBoundary(BaseException):
        pass

    stop = threading.Event()
    hook_calls = []
    persist_calls = []
    stream_ends = []
    job = jobs_mod.Job(id="fatal-boundary", title="Migration")
    registry = jobs_mod.JobRegistry()
    registry.add(job)

    class OneItemQueue:
        def get(self, timeout):
            stop.set()

            def fail(_job):
                raise SimulatedFatalBoundary("simulated fatal boundary")

            return job, fail

        def task_done(self):
            pass

    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_stop_event", stop)
    monkeypatch.setattr(jobs_mod.job_persistence, "admit", lambda _job: True)
    monkeypatch.setattr(
        jobs_mod.job_persistence,
        "persist",
        lambda current: persist_calls.append(
            (current.status, current.finished_at)) or True,
    )
    monkeypatch.setattr(
        jobs_mod, "_fire_post_job_hook", lambda current: hook_calls.append(current.id))
    monkeypatch.setattr(job, "end_stream", lambda: stream_ends.append(job.id))

    jobs_mod._worker_loop(OneItemQueue())

    assert job.status is jobs_mod.JobStatus.FAILED
    assert job.finished_at is not None
    assert persist_calls == [(jobs_mod.JobStatus.FAILED, job.finished_at)]
    assert stream_ends == [job.id]
    assert _wait_for(lambda: hook_calls == [job.id])


def test_pending_durable_recovery_owner_cannot_be_canceled(monkeypatch):
    job = jm.Job(id="exact-resume", title="Interrupted", album_id="album-1")
    job.status = jm.JobStatus.PENDING
    monkeypatch.setattr(jm, "_durable_recovery_job_id", job.id)

    assert jm.request_cancel(job) is False
    assert job.status is jm.JobStatus.PENDING
    assert job.cancel_requested is False


def test_late_cancel_does_not_discard_a_parked_review():
    # A cancel flag arriving just as a scan parks its results must not flip
    # AWAITING_REVIEW to CANCELED — the found candidates would be lost.
    # cancel_review is the explicit path for dismissing a parked review.
    job = jm.Job(title="scan")
    job.status = jm.JobStatus.RUNNING
    job.cancel_requested = True

    def fn(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.status = jm.JobStatus.AWAITING_REVIEW

    jm._run_task(job, fn)
    assert job.status == jm.JobStatus.AWAITING_REVIEW
    assert len(job.candidates) == 1


def test_per_artist_rescan_supersedes_only_that_artists_parked_review(
        monkeypatch):
    # Two artists each have a scan parked for review. Submitting a *different*
    # artist's scan of the same kind must leave both parked reviews untouched —
    # the dedup keys on artist+kind, not kind alone. Keying on kind alone would
    # silently throw away the first artist's un-reviewed candidates. Re-scanning
    # the *same* artist still supersedes that artist's now-stale review.
    from qobuz_librarian.web import app as app_mod

    class Authority:
        @staticmethod
        def intact():
            return True

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", Authority())

    def _park(artist):
        j = jm.Job(title="Artist scan", artist=artist)
        j.execute_kind = "album"
        j.status = jm.JobStatus.AWAITING_REVIEW
        jm.registry.add(j)
        return j

    a = _park("Artist A")
    b = _park("Artist B")
    noop_scan, noop_exec = (lambda j: None), (lambda j, chosen: None)

    fresh = jm.Job(title="Artist scan", artist="Artist C")
    fresh.execute_kind = "album"
    app_mod._submit_scan_deduped(fresh, noop_scan, noop_exec, "album")
    assert a.status == jm.JobStatus.AWAITING_REVIEW
    assert b.status == jm.JobStatus.AWAITING_REVIEW

    rescan = jm.Job(title="Artist scan", artist="Artist A")
    rescan.execute_kind = "album"
    app_mod._submit_scan_deduped(rescan, noop_scan, noop_exec, "album")
    assert a.status == jm.JobStatus.CANCELED
    assert b.status == jm.JobStatus.AWAITING_REVIEW


def test_download_dedup_respects_new_edition_and_single_track_intent():
    # Folding a /download onto an in-flight job must respect intent, not just the
    # album id: "get this edition too" is a deliberate extra copy and a one-track
    # grab is its own thing — neither should be swallowed by an unrelated job for
    # the same album, and a full-album download must not fold onto a one-track grab.
    from qobuz_librarian.web import app as app_mod

    full = jm.Job(title="Album X", artist="Artist", album_id="X")
    full.status = jm.JobStatus.RUNNING
    jm.registry.add(full)

    assert app_mod._duplicate_download_job("X") is full
    assert app_mod._duplicate_download_job("X", as_new_edition=True) is None
    assert app_mod._duplicate_download_job("X", track_id="42") is None

    grab = jm.Job(title="One track", artist="Artist", album_id="Y")
    grab.single = {"album_id": "Y", "track_id": "7"}
    grab.status = jm.JobStatus.RUNNING
    jm.registry.add(grab)

    assert app_mod._duplicate_download_job("Y", track_id="7") is grab
    assert app_mod._duplicate_download_job("Y", track_id="8") is None
    assert app_mod._duplicate_download_job("Y") is None


def test_new_release_review_never_owns_the_library_surface():
    # New-release results live on their own job page; a parked check must not
    # displace the Missing Albums / Gap Fill review on /library.
    from qobuz_librarian.web import app as app_mod

    library = jm.Job(title="Library scan")
    library.execute_kind = "library"
    library.status = jm.JobStatus.AWAITING_REVIEW
    library.created_at = 100.0
    jm.registry.add(library)

    check = jm.Job(title="New-release check")
    check.execute_kind = "new_releases"
    check.status = jm.JobStatus.AWAITING_REVIEW
    check.created_at = 200.0  # newer, would win under most-recent rules
    jm.registry.add(check)

    assert app_mod._library_current_job() is library


def test_parked_review_candidate_does_not_swallow_a_download():
    # An album that merely appears among a parked review's candidates is not
    # queued for anything — refusing an explicit /download with "already
    # queued" over it would be false. Approve re-checks the disk later, so
    # downloading now cannot double up.
    from qobuz_librarian.web import app as app_mod

    review = jm.Job(title="Library scan")
    review.execute_kind = "library"
    review.status = jm.JobStatus.AWAITING_REVIEW
    review.add_candidate(kind="album", title="Wanted", artist="Artist",
                         payload={"album_id": "Z"}, selected=False)
    jm.registry.add(review)

    assert app_mod._duplicate_download_job("Z") is None
    # Once approved and running, the same album folds again.
    review.status = jm.JobStatus.RUNNING
    assert app_mod._duplicate_download_job("Z") is review


def test_direct_new_album_download_uses_durable_queue_lane(
        monkeypatch, tmp_path):
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.queue.executor as executor_mod
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import flows

    tracks = [
        {"id": "t1", "title": "One", "media_number": 1,
         "track_number": 1},
        {"id": "t2", "title": "Two", "media_number": 1,
         "track_number": 2},
    ]
    album = {
        "id": "new1",
        "title": "New Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96,
        "tracks": {"items": tracks},
    }
    seen = {}
    drained = [True]
    outcome = ["downloaded"]
    executor_error = [None]

    class Authority:
        @staticmethod
        def intact():
            return True

    authority = Authority()
    recovery_status = [StartupRecoveryStatus.CLEAR]
    recoveries = []

    def record_recovery(current):
        recoveries.append(current)
        return StartupRecoveryResult(recovery_status[0])

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", authority)
    monkeypatch.setattr(app_mod, "_record_startup_recovery", record_recovery)
    monkeypatch.setattr(app_mod, "_durable_completion_status", lambda _j: False)
    monkeypatch.setattr(app_mod, "_durable_recovery_matches_job", lambda _j: True)

    monkeypatch.setattr(app_mod.cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(app_mod.cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(catalog_mod, "find_existing_tracks",
                        lambda *_a, **_k: ([], None))
    monkeypatch.setattr(catalog_mod, "compute_missing",
                        lambda *_a, **_k: (tracks, []))
    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_a, **_k: pytest.fail("exact new album used legacy processor"),
    )

    def fake_execute(queue, args, token, **kwargs):
        item = queue[0]
        seen["item"] = item
        assert token == "tok"
        assert kwargs == {"consolidate_duplicates": False}
        assert item["album"] is album
        assert item["album_dir"] is None
        assert item["missing"] is tracks
        assert item["present"] == []
        if executor_error[0] is not None:
            raise executor_error[0]
        return ([{
            "result": outcome[0],
            "imported": drained[0],
            "n_ok": 2,
            "n_fail": 0,
            "dir": tmp_path,
        }], drained[0])

    monkeypatch.setattr(executor_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates",
                        lambda *_a, **_k: 0)
    monkeypatch.setattr(hidden_mod, "unmark_single", lambda *_a, **_k: None)

    job = jm.Job(title="New Album", artist="Artist", album_id="new1")
    app_mod._make_download_run(album, "tok")(job)

    assert seen["item"]["upgrade_only"] is False
    assert seen["item"]["auto_upgrade"] is False
    assert job.summary == "2 tracks downloaded."
    assert recoveries == [authority]

    drained[0] = False
    outcome[0] = "retry"
    recovery_status[0] = StartupRecoveryStatus.RESUME_REQUIRED
    interrupted = jm.Job(
        title="New Album", artist="Artist", album_id="new1")

    app_mod._make_download_run(album, "tok")(interrupted)

    assert recoveries == [authority, authority]
    assert interrupted.status is jm.JobStatus.FAILED
    assert interrupted.attention == ""
    assert "use Retry" in interrupted.error

    outcome[0] = "attention"
    recovery_status[0] = StartupRecoveryStatus.ATTENTION_REQUIRED
    unsettled = jm.Job(
        title="New Album", artist="Artist", album_id="new1")

    app_mod._make_download_run(album, "tok")(unsettled)

    assert recoveries == [authority, authority, authority]
    assert unsettled.status is jm.JobStatus.FAILED
    assert unsettled.attention == "recovery"

    executor_error[0] = ValueError("primary executor failure")
    monkeypatch.setattr(
        app_mod,
        "_record_startup_recovery",
        lambda _current: (_ for _ in ()).throw(
            RuntimeError("secondary refresh failure")
        ),
    )
    with pytest.raises(ValueError, match="primary executor failure"):
        app_mod._make_download_run(album, "tok")(
            jm.Job(title="New Album", artist="Artist", album_id="new1")
        )


def test_direct_album_download_refreshes_saved_quality_state(
        monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb1",
        "title": "Album",
        "artist": {"name": "Artist"},
    }
    calls = []

    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_a, **_k: {
            "imported": True,
            "n_ok": 1,
            "n_fail": 0,
            "result": "downloaded",
            "dir": album_dir,
        },
    )
    monkeypatch.setattr(
        flows,
        "_refresh_after_local_album_change",
        lambda *a, **kw: calls.append((a, kw)),
    )

    job = jm.Job(title="Album", artist="Artist", album_id="alb1")
    app_mod._make_download_run(album, "tok")(job)

    assert job.status != jm.JobStatus.FAILED
    assert len(calls) == 1
    assert calls[0][0][0] is album
    assert Path(calls[0][0][1]["dir"]) == album_dir
    assert calls[0][1] == {
        "fallback_artist": "Artist",
        "token": "tok",
        "args": calls[0][1]["args"],
        "upgrade": True,
        "downsample": True,
    }


def test_direct_single_track_download_refreshes_saved_quality_state(
        monkeypatch, tmp_path):
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.builder as builder_mod
    import qobuz_librarian.queue.executor as executor_mod
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb1",
        "title": "Album",
        "year": 2024,
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "t1", "title": "One", "track_number": 1},
            {"id": "t2", "title": "Two", "track_number": 2},
        ]},
    }
    track = album["tracks"]["items"][0]
    calls = []

    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *_a, **_k: ([], None))
    monkeypatch.setattr(
        builder_mod,
        "_build_queue_item",
        lambda **kwargs: {
            "album": kwargs["album"],
            "missing": kwargs["missing"],
            "n_ok": 0,
            "n_fail": 0,
            "imported": False,
        },
    )

    def fake_execute(queue, *_a, **_k):
        queue[0]["n_ok"] = 1
        queue[0]["n_fail"] = 0
        queue[0]["imported"] = True
        queue[0]["_resolved_post_dir"] = str(album_dir)

    monkeypatch.setattr(executor_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(
        flows,
        "_refresh_after_local_album_change",
        lambda *a, **kw: calls.append((a, kw)),
    )
    monkeypatch.setattr(app_mod.cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False, raising=False)

    job = jm.Job(title="One", artist="Artist", album_id="alb1")
    app_mod._make_single_track_run(album, track, "tok")(job)

    assert job.status != jm.JobStatus.FAILED
    assert len(calls) == 1
    assert calls[0][0][0] is album
    assert Path(calls[0][0][1]["dir"]) == album_dir
    assert calls[0][1] == {
        "fallback_artist": "Artist",
        "token": "tok",
        "args": calls[0][1]["args"],
        "upgrade": True,
        "downsample": True,
    }


def test_cancel_while_queued_finalizes_and_worker_skips_it():
    # A scan queued behind a busy lane, cancelled before it starts, is finalized
    # to CANCELED at once (it doesn't linger as "Queued" until the job ahead of
    # it finishes), and when the lane frees the worker drops it rather than
    # running it.
    import threading

    jm.start_worker()
    release = threading.Event()
    holding = threading.Event()

    holder = jm.Job(title="lane holder")
    holder.kind = "scan"

    def _hold(j):
        holding.set()
        release.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold))
    assert holding.wait(timeout=5)

    ran = threading.Event()
    queued = jm.Job(title="queued scan")
    queued.kind = "scan"
    jm.registry.add(queued)
    jm._scan_queue.put((queued, lambda j: ran.set()))

    assert queued.status == jm.JobStatus.PENDING
    assert jm.request_cancel(queued) is True
    assert queued.status == jm.JobStatus.CANCELED
    assert queued not in jm.registry.pending_and_running()

    release.set()
    assert _wait_for(lambda: holder.status == jm.JobStatus.DONE)
    assert not ran.wait(timeout=0.5)
    assert queued.status == jm.JobStatus.CANCELED


def test_pending_cancel_wins_before_the_worker_claims():
    """A cancel already changing a queued job must finish before the worker
    can claim and run that same job."""
    import queue
    import threading

    cancel_paused = threading.Event()
    allow_cancel = threading.Event()
    worker_waiting = threading.Event()
    ran = threading.Event()

    class PausingJob(jm.Job):
        @property
        def cancel_requested(self):
            return getattr(self, "_cancel_requested", False)

        @cancel_requested.setter
        def cancel_requested(self, value):
            if value and threading.current_thread().name == "cancel-race-request":
                cancel_paused.set()
                allow_cancel.wait(timeout=5)
            self._cancel_requested = value

    class TrackedRLock:
        def __init__(self):
            self._lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "cancel-race-worker":
                worker_waiting.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_exc):
            self._lock.release()

    job = PausingJob(title="claim race")
    job._lock = TrackedRLock()
    work = queue.Queue()
    work.put((job, lambda _job: ran.set()))
    jm._stop_event.clear()

    cancel = threading.Thread(
        target=jm.request_cancel, args=(job,), name="cancel-race-request"
    )
    worker = threading.Thread(
        target=jm._worker_loop, args=(work,), name="cancel-race-worker"
    )
    cancel.start()
    assert cancel_paused.wait(timeout=5)
    worker.start()
    try:
        assert worker_waiting.wait(timeout=1)
        assert not ran.is_set()
    finally:
        allow_cancel.set()
        cancel.join(timeout=5)
        assert _wait_for(lambda: work.unfinished_tasks == 0)
        jm._stop_event.set()
        worker.join(timeout=2)
        jm._stop_event.clear()

    assert not cancel.is_alive()
    assert not worker.is_alive()
    assert not ran.is_set()
    assert job.status == jm.JobStatus.CANCELED


def test_approve_flips_status_then_passes_chosen_to_execute(monkeypatch):
    job = jm.Job(title="scan-approve")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    got_chosen = []
    job._execute_fn = lambda j, chosen: got_chosen.append(chosen)
    job.add_candidate("album", "A", "Artist", payload={"id": 1})

    status_at_put = []
    enqueued = []

    def _spy_put(item):
        status_at_put.append(item[0].status)
        enqueued.append(item[1])

    monkeypatch.setattr(jm._scan_queue, "put", _spy_put)

    assert jm.approve(job, ["c0"]) is True
    # Status flips to PENDING before the execute step is enqueued, so a second
    # concurrent approve can't double-enqueue the download.
    assert status_at_put == [jm.JobStatus.PENDING]
    # Running the enqueued step hands execute_fn exactly the kept candidate.
    enqueued[0](job)
    assert [c["payload"] for c in got_chosen[0]] == [{"id": 1}]
    # A second approve no longer sees AWAITING_REVIEW, so it's rejected.
    assert jm.approve(job, ["c0"]) is False


def test_approve_keeps_review_when_the_eligible_selection_went_stale(
        monkeypatch):
    job = jm.Job(title="scan-approve")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda _job, _chosen: None
    missing = job.add_candidate(
        "album", "Missing", "Artist", payload={"gap_fill": 0}, selected=False)
    job.add_candidate(
        "album", "Gap", "Artist", payload={"gap_fill": 1}, selected=True)

    admitted = []
    enqueued = []
    monkeypatch.setattr(
        jm.job_persistence,
        "admit_review_transition",
        lambda *_args, **_kwargs: admitted.append(True) or True,
    )
    monkeypatch.setattr(jm._scan_queue, "put", enqueued.append)

    result = jm.approve(
        job,
        None,
        selection_filter=lambda candidate: candidate["cid"] == missing,
    )

    assert result is jm.APPROVAL_NO_SELECTION
    assert job.status is jm.JobStatus.AWAITING_REVIEW
    assert admitted == []
    assert enqueued == []


# ── app.py: HTTP routes ───────────────────────────────────────────────────────

class _SameThreadASGIClient:
    """Small sync wrapper around HTTPX's ASGI transport.

    Starlette's TestClient uses a cross-thread AnyIO portal. That portal can
    hang in some local Python environments before the app sees a request, so
    these route tests drive the async FastAPI routes on the calling thread.
    """

    def __init__(self, app):
        self.app = app
        self.base_url = "http://testserver"
        self.cookies = httpx.Cookies()
        self.headers = httpx.Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def request(self, method, url, **kwargs):
        extra_headers = kwargs.pop("headers", None)
        headers = httpx.Headers(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        follow_redirects = kwargs.pop("follow_redirects", True)

        async def _send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=self.base_url,
                cookies=self.cookies,
                headers=headers,
                follow_redirects=follow_redirects,
            ) as ac:
                response = await ac.request(method, url, **kwargs)
                self.cookies.update(ac.cookies)
                return response

        return asyncio.run(_send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def stream(self, method, url, **kwargs):
        return _ResponseContext(self.request(method, url, **kwargs))


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *_exc):
        try:
            self.response.close()
        except RuntimeError:
            pass
        return False


class _InlineExecutorLoop:
    async def run_in_executor(self, _executor, fn, *args):
        return fn(*args)


class _InlineExecutorAsyncio:
    def __init__(self, real_asyncio):
        self._real_asyncio = real_asyncio

    def get_running_loop(self):
        return _InlineExecutorLoop()

    def __getattr__(self, name):
        return getattr(self._real_asyncio, name)


def _run_web_executors_inline(monkeypatch, app_mod):
    monkeypatch.setattr(app_mod, "asyncio", _InlineExecutorAsyncio(asyncio))


@pytest.fixture
def client(monkeypatch):
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as app_mod

    class TestAuthority:
        def __init__(self):
            self.closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", TestAuthority())
    monkeypatch.setattr(app_mod, "_CLI_MODE", False)
    monkeypatch.setattr(app_mod, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(app_mod, "_LOCK_UNENFORCEABLE", False)
    monkeypatch.setattr(app_mod, "_SHUTTING_DOWN", False)
    # This lightweight client bypasses the application lifespan. Treat its
    # in-memory registry as already restored unless a test exercises restore.
    monkeypatch.setattr(app_mod, "_JOBS_RESTORED", True)
    clear_recovery = StartupRecoveryResult(StartupRecoveryStatus.CLEAR)
    monkeypatch.setattr(app_mod, "_STARTUP_RECOVERY_RESULT", clear_recovery)
    monkeypatch.setattr(app_mod, "_STARTUP_RECOVERY_UNKNOWN", False)

    def _record_clear(_authority):
        app_mod._STARTUP_RECOVERY_RESULT = clear_recovery
        return clear_recovery

    monkeypatch.setattr(app_mod, "_record_startup_recovery", _record_clear)
    _run_web_executors_inline(monkeypatch, app_mod)
    with _SameThreadASGIClient(app_mod.app) as c:
        c.get("/queue")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c


def test_lyric_retry_reports_job_admission_failure(client, monkeypatch):
    monkeypatch.setattr(jm.job_persistence, "admit", lambda _job: False)

    response = client.post("/lyric-retry", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/queue?error=")
    assert "data%20folder" in response.headers["location"]
    assert jm.registry.all() == []

    response = client.post("/lyrics", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/lyrics?error=")
    assert "data%20folder" in response.headers["location"]
    assert jm.registry.all() == []


def test_search_uses_a_generous_result_limit(client, monkeypatch):
    # The front-page search was capped at 8, so a major artist surfaced almost
    # nothing (the owner's first complaint). The handler must pass the configured
    # limit through to Qobuz, and that default must be generous.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(search_mod, "search_albums", fake)
    r = client.post("/search", data={"q": "Paul McCartney", "kind": "album"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert seen.get("limit") == cfg.SEARCH_LIMIT
    assert cfg.SEARCH_LIMIT >= 20


def test_artist_search_lists_qobuz_artists(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["query"] = q
        seen["limit"] = limit
        return [{"id": "artist1", "name": "Paysage d'Hiver", "albums_count": 12}]

    monkeypatch.setattr(search_mod, "search_artists", fake)

    r = client.post("/search", data={"q": "Paysage", "kind": "artist"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert seen == {"query": "Paysage", "limit": cfg.ARTIST_LOOKUP_LIMIT}
    assert "Paysage" in r.text
    assert "View albums" in r.text
    assert 'name="artist_id" value="artist1"' in r.text


def test_artist_search_selected_artist_shows_discography(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: None)
    seen = {}

    def fake_catalog(artist_id, token, limit=None, fresh=False):
        seen["artist_id"] = artist_id
        seen["limit"] = limit
        return ([{
            "id": "album1",
            "title": "Das Tor",
            "artist": {"name": "Paysage d'Hiver"},
            "year": 2013,
            "tracks_count": 10,
            "maximum_bit_depth": 16,
        }], 1)

    monkeypatch.setattr(search_mod, "get_artist_albums", fake_catalog)

    r = client.post(
        "/search",
        data={
            "q": "Paysage",
            "kind": "artist",
            "artist_id": "artist1",
            "artist_name": "Paysage d'Hiver",
        },
        headers={"HX-Request": "true"},
    )

    assert r.status_code == 200
    assert seen == {"artist_id": "artist1", "limit": cfg.ARTIST_CATALOG_LIMIT}
    assert "Paysage d&#39;Hiver" in r.text
    assert "1 album on Qobuz" in r.text
    assert "Das Tor" in r.text
    assert "Download" in r.text


def test_dashboard_artist_query_renders_initial_results(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["query"] = q
        seen["limit"] = limit
        return [{"id": "artist1", "name": "Paysage d'Hiver", "albums_count": 12}]

    monkeypatch.setattr(search_mod, "search_artists", fake)

    r = client.get("/?kind=artist&q=Paysage")

    assert r.status_code == 200
    assert seen == {"query": "Paysage", "limit": cfg.ARTIST_LOOKUP_LIMIT}
    assert "Paysage d&#39;Hiver" in r.text
    assert "View albums" in r.text


def test_album_search_keeps_upgrades_out_of_search(client, monkeypatch, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {
        "id": "album1",
        "title": "Das Tor",
        "artist": {"name": "Paysage d'Hiver"},
        "year": 2013,
        "tracks_count": 10,
        "maximum_bit_depth": 24,
    }
    owned = tmp_path / "Das Tor (2013)"
    owned.mkdir()
    (owned / "01 - Das Tor.flac").write_bytes(b"\x00")  # real audio => in library

    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [album])
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: owned)

    r = client.post("/search", data={"q": "Das Tor", "kind": "album"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "In library" in r.text
    assert "quality-upgrade" not in r.text
    assert ">Upgrade<" not in r.text


def test_new_release_check_refused_without_baseline(client, monkeypatch):
    # "Check for new releases" is a library-walk-and-compare — useless until a full
    # library scan has built the baseline. Without one it must NOT start a crawl
    # (the old bug: it ran an empty crawl AND flipped baseline_complete=True, which
    # then stopped an interrupted library scan from resuming). It refuses instead.
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import flows
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(flows, "scan_new_releases", lambda *a, **k: None)
    assert new_releases.is_baseline_complete() is False      # fresh state, no baseline
    r = client.post("/library", data={"mode": "new_releases"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/library")
    assert app_mod._existing_new_release_check() is None     # no crawl was started


def test_library_scan_state_explains_empty_music_root(tmp_path, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod.cfg, "MUSIC_ROOT", tmp_path)
    state = app_mod._library_scan_state()

    assert state["ready"] is False
    assert str(tmp_path) in state["message"]
    assert "MUSIC_ROOT" not in state["message"]
    assert "QL_MUSIC_DIR" not in state["message"]
    assert "artist" in state["message"].lower()


def test_qobuz_ready_false_when_saved_token_is_rejected(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "bad-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", False)

    assert app_mod._qobuz_ready() is False


def test_qobuz_ready_allows_unproven_saved_token(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "maybe-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)

    assert app_mod._qobuz_ready() is True


def test_recent_empty_hint_matches_qobuz_account_state(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    assert app_mod._recent_empty_hint() == "Set up Qobuz before searching."

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "maybe-token", "user_id": "user"})
    assert app_mod._recent_empty_hint() == "Search above to find an artist, album, or track."


def test_empty_library_scan_message_uses_plain_music_library_wording(monkeypatch, caplog):
    from qobuz_librarian.web import flows

    monkeypatch.setattr(flows, "list_library_artists", lambda: [])
    caplog.set_level("INFO", logger="qobuz_librarian")
    job = jm.Job(title="scan")

    flows.scan_library(job, "tok")

    assert "music library" in job.summary
    assert "artist folders" in job.summary
    assert "MUSIC_ROOT" not in job.summary
    assert "QL_MUSIC_DIR" not in job.summary
    out = caplog.text
    assert "MUSIC_ROOT" not in out
    assert "QL_MUSIC_DIR" not in out


def test_settings_save_defers_apply_when_job_is_active(tmp_path, monkeypatch):
    """An in-flight job must not see cfg.* flip mid-run."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    with ss._pending_lock:
        ss._pending_apply = None

    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})
    assert ok is True
    assert (tmp_path / "s.json").exists()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is False  # not yet applied

    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True  # idempotent


def test_parked_review_does_not_defer_settings(tmp_path, monkeypatch):
    """A parked review can sit for weeks — a save made next to one must apply
    right away, not wait in the pending slot for a job that may never run."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    review.execute_kind = "downsample"
    with ss._pending_lock:
        ss._pending_apply = None

    assert ss._any_active_job() is False
    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})
    assert ok is True
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True  # applied immediately
    with ss._pending_lock:
        assert ss._pending_apply is None


def test_quality_change_flags_the_stale_upgrade_review(
        client, tmp_path, monkeypatch):
    """Lowering/raising the download quality leaves a saved Upgrade
    review promising dead targets — the save must say a refresh updates it.
    An unchanged save stays quiet."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr("qobuz_librarian.quality.upgrade_state.load",
                        lambda: {"candidates": [{"title": "x"}]})
    with ss._pending_lock:
        ss._pending_apply = None

    r = client.post("/settings/behavior", data={"STREAMRIP_QUALITY": "2"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "quality_note=1" in r.headers["location"]
    r2 = client.post("/settings/behavior", data={"STREAMRIP_QUALITY": "2"},
                     follow_redirects=False)
    assert "quality_note" not in r2.headers["location"]


def test_settings_save_only_pins_changed_fields(tmp_path, monkeypatch):
    """Saving the Settings form must not freeze untouched fields into the
    settings file — the file wins over env on load, so writing a field that
    merely matched its current value would silently stop that env var from
    ever applying again. Only real changes (and fields saved before) persist."""
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "PREFER_HIRES", True)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    with ss._pending_lock:
        ss._pending_apply = None

    # The form posts every field; only LYRICS_ENABLED actually changed.
    ok, _ = ss.save({"LYRICS_ENABLED": False, "PREFER_HIRES": True,
                     "STREAMRIP_QUALITY": "4"})
    assert ok is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk == {"LYRICS_ENABLED": False}

    # A field that was saved before stays in the file even when a later save
    # posts it unchanged — the user set it deliberately, so it keeps winning.
    ok, _ = ss.save({"LYRICS_ENABLED": False, "PREFER_HIRES": False})
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk == {"LYRICS_ENABLED": False, "PREFER_HIRES": False}


def test_review_changed_nudge_names_its_origin():
    """The review-sync fan-out carries the originating tab's id, so that tab
    can skip reloading a page its own action already updated — reloading the
    originator swapped the DOM mid-interaction and ate rapid ticks."""
    from qobuz_librarian.web import jobs as job_mgr

    job = job_mgr.Job(title="t")
    sub = job.subscribe()
    try:
        job.notify_review_changed("tab42")
        line = sub.get(timeout=1)
        assert line == job_mgr.REVIEW_CHANGED + "tab42"
        job.notify_review_changed()
        assert sub.get(timeout=1) == job_mgr.REVIEW_CHANGED
    finally:
        job.unsubscribe(sub)


# ── CSRF middleware ───────────────────────────────────────────────────────────

def test_csrf_post_without_token_is_rejected():
    """One representative POST verifies CSRF-missing → 403."""
    from qobuz_librarian.web import app as app_mod
    with _SameThreadASGIClient(app_mod.app) as c:
        c.get("/queue")
        r = c.post("/search", data={"q": "anything"})
        assert r.status_code == 403


# ── run-lock busy → destructive routes 503, read-only stay open ───────

def test_lock_busy_refuses_destructive_routes(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    _run_web_executors_inline(monkeypatch, webapp)
    with _SameThreadASGIClient(webapp.app) as c:
        c.get("/queue")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        monkeypatch.setattr(webapp, "_LOCK_BUSY_PID", 4321)

        dash = c.get("/")
        assert dash.status_code == 200
        assert "Another Qobuz Librarian run is active." in dash.text
        assert "4321" not in dash.text

        for path, data in [
            ("/download", {"album_id": "1"}),
            ("/library", {}),
            ("/downsample", {}),
            ("/repair", {}),
            ("/lyrics", {}),
            ("/lyric-retry", {}),
            ("/jobs/whatever/approve", {}),
        ]:
            r = c.post(path, data=data, follow_redirects=False)
            assert r.status_code == 503, f"{path} should 503 when lock busy"
            # The full-page response should render the base shell, not a bare
            # <pre>, so a non-htmx caller still has navigation back.
            assert "ql-app-shell" in r.text, f"{path} should render base.html shell"
            assert "Another Qobuz Librarian run is active." in r.text
            assert "pid 4321" not in r.text
            assert "run-lock" not in r.text
            assert ">Try again</button>" in r.text
            assert ">Back to Search</a>" in r.text


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("attention_required", "could not be verified safely"),
        ("resume_required", "interrupted download"),
    ],
)
def test_durable_startup_recovery_pauses_mutations_but_not_browsing(
        client, monkeypatch, status, message):
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(
        webapp,
        "_STARTUP_RECOVERY_RESULT",
        StartupRecoveryResult(StartupRecoveryStatus(status)),
    )

    assert client.get("/").status_code == 200
    blocked = client.post(
        "/download", data={"album_id": "1"}, follow_redirects=False)
    assert blocked.status_code == 503
    assert message in blocked.text


@pytest.mark.parametrize(
    ("origin_value", "expected", "unexpected"),
    [
        ("cli", "Switch to terminal mode", "Queue or History"),
        ("web-job", "Queue or History", "Switch to terminal mode"),
    ],
)
def test_resume_guidance_uses_saved_completion_origin(
        client, monkeypatch, origin_value, expected, unexpected):
    from qobuz_librarian.completion import (
        CompletionExpectation,
        CompletionInput,
        CompletionOrigin,
        CompletionOriginKind,
        CompletionScope,
        QualityTarget,
        RecoveryOwner,
    )
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryAction,
        StartupRecoveryItem,
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp

    operation_id = "a" * 64
    item_id = "b" * 64
    owner = RecoveryOwner(operation_id, item_id)
    slot = "qobuz:track-1"
    completion_input = CompletionInput(
        owner=owner,
        origin=CompletionOrigin(CompletionOriginKind(origin_value), "source"),
        expectation=CompletionExpectation(
            album_id="1",
            scope=CompletionScope.ALBUM,
            catalogue_slots=(slot,),
            requested_slots=(slot,),
            quality_targets=(QualityTarget(slot, 16, 44_100),),
        ),
        effective_tier=2,
    )
    mode = "web-job:resume-source"
    queued_item = queue_state.JournalItem(
        item_id,
        queue_state.QueuePhase.PENDING,
        {"album": {"id": "1"}},
        completion_input=completion_input.to_record(),
    )
    saved = queue_state.QueueJournal(
        operation_id,
        mode,
        "2026-07-13T00:00:00+00:00",
        (queued_item,),
    )
    monkeypatch.setattr(
        queue_state,
        "load_queue_journal",
        lambda operation_id: queue_state.QueueLoad(
            queue_state.QueueLoadStatus.READY,
            saved if operation_id == saved.operation_id else None,
        ),
    )
    monkeypatch.setattr(
        webapp,
        "_STARTUP_RECOVERY_RESULT",
        StartupRecoveryResult(
            StartupRecoveryStatus.RESUME_REQUIRED,
            (
                StartupRecoveryItem(
                    operation_id,
                    item_id,
                    mode,
                    queue_state.QueuePhase.PENDING,
                    StartupRecoveryAction.PENDING,
                ),
            ),
        ),
    )

    blocked = client.post("/download", data={"album_id": "1"})

    assert blocked.status_code == 503
    assert expected in blocked.text
    assert unexpected not in blocked.text


def test_recovery_refreshes_are_serialised(monkeypatch):
    import threading

    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp

    first_entered = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    state_lock = threading.Lock()

    def fake_recover(_authority):
        nonlocal calls
        with state_lock:
            calls += 1
            call = calls
        if call == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return StartupRecoveryResult(StartupRecoveryStatus.CLEAR)

    monkeypatch.setattr(webapp, "_recover_startup_queue", fake_recover)
    monkeypatch.setattr(
        webapp.job_mgr,
        "set_durable_recovery_job_id",
        lambda _job_id: None,
    )

    first = threading.Thread(
        target=webapp._record_startup_recovery,
        args=(object(),),
    )

    def run_second():
        second_started.set()
        webapp._record_startup_recovery(object())

    second = threading.Thread(target=run_second)
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    assert second_started.wait(timeout=5)
    assert not second_entered.wait(timeout=0.2)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == 2
    assert second_entered.is_set()


def test_web_startup_settles_exact_cli_queue_completion_without_job_row(
        monkeypatch):
    from qobuz_librarian.completion import (
        CompletionOrigin,
        CompletionOriginKind,
        RecoveryOwner,
    )
    from qobuz_librarian.queue import startup_recovery
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    accepted = []

    def fake_recover(*, authority, acknowledge_completion):
        assert authority is marker
        accepted.append(acknowledge_completion(
            CompletionOrigin(CompletionOriginKind.CLI, "download-queue"),
            RecoveryOwner("operation", "item"),
            album_id="42",
            completion_hash="a" * 64,
            planned={"album": {"id": "42"}},
            post_dir="/music/Artist/Album",
        ))
        status = (
            startup_recovery.StartupRecoveryStatus.CLEAR
            if accepted[-1]
            else startup_recovery.StartupRecoveryStatus.ATTENTION_REQUIRED
        )
        return startup_recovery.StartupRecoveryResult(status)

    marker = object()
    monkeypatch.setattr(startup_recovery, "recover_startup_state", fake_recover)
    monkeypatch.setattr(job_persistence, "init", lambda: None)
    monkeypatch.setattr(
        job_persistence,
        "acknowledge_durable_completion",
        lambda *_a, **_k: pytest.fail("CLI completion touched jobs.db"),
    )

    result = webapp._recover_startup_queue(marker)

    assert accepted == [True]
    assert result.status is startup_recovery.StartupRecoveryStatus.CLEAR


def test_error_page_offers_a_way_back(client):
    r = client.get("/not-a-real-page")

    assert r.status_code == 404
    assert "Page not found" in r.text
    assert ">Back to Search</a>" in r.text


def test_tool_pages_offer_a_primary_action(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    # The Library page only renders its scan button once the library folder is
    # ready (has artist folders with audio); give it one so the button shows.
    monkeypatch.setattr(
        "qobuz_librarian.library.scanner.list_library_artists",
        lambda: ["Some Artist"],
    )
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE",
        True,
    )
    monkeypatch.setattr(
        "qobuz_librarian.integrations.lyric_fetch.AVAILABLE",
        True,
    )
    for path in ("/library", "/downsample", "/repair", "/lyrics"):
        r = client.get(path)
        assert r.status_code == 200
        assert "ql-btn-primary" in r.text


def test_primary_nav_lists_every_destination(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    labels = [
        "Search",
        "Library",
        "Upgrade",
        "Downsample",
        "Repair",
        "Lyrics",
        "Queue / History",
        "Settings",
    ]
    positions = [r.text.find(label) for label in labels]
    assert all(pos >= 0 for pos in positions)
    assert positions == sorted(positions)
    assert ">Dashboard<" not in r.text
    assert ">Tools<" not in r.text
    assert ">Migrate<" not in r.text


def test_nav_shows_qobuz_setup_when_credentials_are_missing(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})

    r = client.get("/")

    assert r.status_code == 200
    assert "Set up Qobuz in Settings" in r.text
    assert 'href="/settings"' in r.text
    assert "Your Qobuz token was rejected" not in r.text


def test_dashboard_qobuz_setup_card_stays_until_qobuz_is_connected(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})

    r = client.get("/")

    assert r.status_code == 200
    assert 'data-qobuz-setup-card' in r.text
    assert 'data-qobuz-setup-dismiss' not in r.text
    assert "Qobuz credentials" in r.text
    assert "Open Settings" in r.text
    assert "Downsample" in r.text
    assert "Lyrics" in r.text
    assert "Set up Qobuz in Settings" in r.text


def test_dashboard_search_modes_are_artist_album_track(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    assert "<span>Artist</span>" in r.text
    assert "<span>Album</span>" in r.text
    assert "<span>Track</span>" in r.text
    assert "<span>Artists</span>" not in r.text
    assert "<span>Albums</span>" not in r.text
    assert "<span>Tracks</span>" not in r.text
    assert r.text.count('hx-post="/search"') == 1
    assert 'hx-include="closest form"' not in r.text


def test_search_page_does_not_show_empty_dashboard_cards(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    assert "<h1>Search</h1>" in r.text
    assert 'class="ql-search-form"' in r.text
    assert "Needs review" not in r.text
    assert "Running and queued" not in r.text
    assert "Recent downloads" not in r.text
    assert "Latest completed downloads" not in r.text
    assert "No scans waiting for review." not in r.text
    assert "Nothing running or queued." not in r.text


def test_search_page_does_not_render_review_jobs_as_front_page_cards(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=False)
    try:
        r = client.get("/")

        assert r.status_code == 200
        assert 'class="ql-search-form"' in r.text
        assert "Library scan" not in r.text
        assert "1 missing album or Gap Fill candidate found." not in r.text
        assert f'href="/jobs/{job.id}"' not in r.text
    finally:
        _remove_job(job)


def test_nav_review_badges_clear_when_tabs_are_opened(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    review_badges.mark_ready("library", now=100.0)
    review_badges.mark_ready("upgrade", now=101.0)
    review_badges.mark_ready("downsample", now=102.0)

    r = client.get("/")

    assert r.status_code == 200
    assert 'data-review-badge="library"' in r.text
    assert 'data-review-badge="upgrade"' in r.text
    assert 'data-review-badge="downsample"' in r.text

    assert client.get("/upgrade").status_code == 200
    r = client.get("/")

    assert 'data-review-badge="library"' in r.text
    assert 'data-review-badge="upgrade"' not in r.text
    assert 'data-review-badge="downsample"' in r.text

    assert client.get("/library").status_code == 200
    assert client.get("/downsample").status_code == 200
    r = client.get("/")

    assert 'data-review-badge="library"' not in r.text
    assert 'data-review-badge="upgrade"' not in r.text
    assert 'data-review-badge="downsample"' not in r.text


def test_upgrade_disabled_hides_nav_and_badge(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    review_badges.mark_ready("upgrade", now=100.0)

    r = client.get("/")

    assert r.status_code == 200
    assert 'href="/upgrade"' not in r.text
    assert 'data-review-badge="upgrade"' not in r.text


def test_upgrade_nav_hidden_without_qobuz_credentials(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})

    r = client.get("/")

    assert r.status_code == 200
    assert 'href="/upgrade"' not in r.text


def test_upgrade_disabled_page_redirects_cleanly(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/upgrade", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_upgrade_page_reviews_saved_baseline_candidates(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    r = client.get("/upgrade")

    assert r.status_code == 200
    assert "upgrade candidate" in r.text
    assert "1 upgrade candidate" in r.text
    assert 'action="/upgrade/review"' in r.text
    assert "Review candidates" in r.text
    assert "Start upgrade scan" not in r.text
    assert "Quality upgrade scan" not in r.text


def test_upgrade_review_post_uses_saved_state_without_scanning(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("Upgrade review must not start a scan")

    monkeypatch.setattr("qobuz_librarian.web.flows.scan_upgrades", fail_scan)

    r = client.post("/upgrade/review", follow_redirects=False)

    assert r.status_code == 303
    job_id = r.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    assert job is not None
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert job.execute_kind == "upgrade"
    assert job.review_verb == "Upgrade"
    assert len(job.candidates) == 1
    assert job.candidates[0]["selected"] is False


def test_upgrade_review_post_reuses_existing_saved_review_job(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    second = client.post("/upgrade/review", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1

def test_saved_review_signature_ignores_candidate_order():
    from qobuz_librarian.web import app as webapp

    first = {
        "complete": True,
        "candidates": [
            {
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1"},
            },
            {
                "title": "Third",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up2"},
            },
        ],
    }
    second = {**first, "candidates": list(reversed(first["candidates"]))}

    assert (
        webapp._saved_review_signature("upgrade", first)
        == webapp._saved_review_signature("upgrade", second)
    )


def test_saved_review_creation_is_atomic_for_parallel_posts(monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    real_add = job_mgr.registry.add

    def slow_add(job):
        time.sleep(0.02)
        real_add(job)

    monkeypatch.setattr(job_mgr.registry, "add", slow_add)
    state = {
        "complete": True,
        "candidates": [
            {
                "title": "First",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1"},
            },
            {
                "title": "Second",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up2"},
            },
        ],
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        jobs = list(ex.map(
            lambda _i: webapp._review_job_from_upgrade_state(state),
            range(8),
        ))

    assert len({j.id for j in jobs}) == 1
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1

    for candidate in jobs[0].candidates:
        candidate["selected"] = True
    jobs[0].status = job_mgr.JobStatus.RUNNING
    remaining = {
        "complete": True,
        "candidates": [{
            **state["candidates"][1],
            "detail": "fresh saved-state detail",
        }],
    }
    claimed = webapp._review_job_from_upgrade_state(remaining)
    assert claimed is jobs[0]
    assert len([
        j for j in job_mgr.registry.all()
        if j.execute_kind == "upgrade" and j.status in job_mgr.ACTIVE
    ]) == 1


def test_upgrade_saved_review_respects_hidden_candidates(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [
                {
                    "title": "Dummy",
                    "artist": "Portishead",
                    "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                    "payload": {"album_id": "up1", "year": "1994", "cover": ""},
                },
                {
                    "title": "Third",
                    "artist": "Portishead",
                    "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                    "payload": {"album_id": "up2", "year": "2008", "cover": ""},
                },
            ],
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Dummy")
    client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
    client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})

    store = hidden.load()
    assert hidden.is_hidden(hidden.SCOPE_UPGRADE, "Portishead", "Third", store)
    assert [c["title"] for c in job.candidates] == ["Dummy"]

    r = client.get("/upgrade")
    assert r.status_code == 200
    assert "1 upgrade candidate" in r.text
    assert "2 upgrade candidates" not in r.text

    second = client.post("/upgrade/review", follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1


def test_saved_review_snapshot_is_serialized_with_hide(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    state = {
        "updated_at": time.time(),
        "complete": True,
        "candidates": [
            {
                "title": "Keep",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1"},
            },
            {
                "title": "Hide",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up2"},
            },
        ],
    }
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load", lambda: state)
    first = client.post("/upgrade/review", follow_redirects=False)
    job = job_mgr.registry.get(
        first.headers["location"].removeprefix("/jobs/"))
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Keep")
    client.post(f"/jobs/{job.id}/select",
                data={"cid": keep, "checked": "1"})

    factory_entered = threading.Event()
    release_factory = threading.Event()
    real_factory = webapp._review_job_from_upgrade_state

    def delayed_factory(snapshot):
        factory_entered.set()
        assert release_factory.wait(2)
        return real_factory(snapshot)

    monkeypatch.setattr(
        webapp, "_review_job_from_upgrade_state", delayed_factory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        review = executor.submit(
            client.post, "/upgrade/review", follow_redirects=False)
        assert factory_entered.wait(2)
        acquired = webapp._SAVED_REVIEW_LOCK.acquire(blocking=False)
        if acquired:
            webapp._SAVED_REVIEW_LOCK.release()
        assert acquired is False
        hide = executor.submit(
            client.post,
            f"/jobs/{job.id}/hide",
            data={"artist": "Portishead"},
        )
        release_factory.set()
        assert review.result(timeout=3).status_code == 303
        assert hide.result(timeout=3).status_code == 200

    assert [candidate["title"] for candidate in job.candidates] == ["Keep"]
    assert hidden.is_hidden(
        hidden.SCOPE_UPGRADE, "Portishead", "Hide", hidden.load())


def test_upgrade_saved_review_restore_updates_existing_job(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [
                {
                    "title": "Dummy",
                    "artist": "Portishead",
                    "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                    "payload": {"album_id": "up1", "year": "1994", "cover": ""},
                },
                {
                    "title": "Third",
                    "artist": "Portishead",
                    "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                    "payload": {"album_id": "up2", "year": "2008", "cover": ""},
                },
            ],
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Dummy")
    client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
    client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
    hidden.restore_albums(hidden.SCOPE_UPGRADE, [
        hidden.album_fingerprint("Portishead", "Third")
    ])

    second = client.post("/upgrade/review", follow_redirects=False)

    assert second.headers["location"] == first.headers["location"]
    assert [c["title"] for c in job.candidates] == ["Dummy", "Third"]
    assert {c["title"]: c["selected"] for c in job.candidates} == {
        "Dummy": True,
        "Third": False,
    }
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1


def test_saved_review_sync_does_not_replace_candidates_after_approval(
        monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    job = jm.Job(title="Upgrade candidates")
    job.execute_kind = "upgrade"
    job.status = jm.JobStatus.PENDING
    job.add_candidate(
        kind="upgrade",
        title="Original",
        artist="Portishead",
        payload={"album_id": "old"},
        selected=True,
    )
    persisted = []
    notified = []
    monkeypatch.setattr(
        job_persistence, "persist", lambda _job: persisted.append(True) or True)
    monkeypatch.setattr(
        job, "notify_review_changed", lambda *_args: notified.append(True))

    webapp._sync_saved_review_job(
        job,
        "upgrade",
        {"candidates": [{
            "title": "Replacement",
            "artist": "Portishead",
            "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
            "payload": {"album_id": "new"},
        }]},
        "new-signature",
    )

    assert [candidate["title"] for candidate in job.candidates] == ["Original"]
    assert persisted == []
    assert notified == []


def test_upgrade_approve_resyncs_saved_review_before_execution(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    state = {
        "updated_at": time.time(),
        "complete": True,
        "candidates": [{
            "title": "Stale",
            "artist": "Portishead",
            "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
            "payload": {"album_id": "old", "year": "1994", "cover": ""},
        }],
    }
    monkeypatch.setattr("qobuz_librarian.quality.upgrade_state.load", lambda: state)

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    state["candidates"] = []

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == f"/jobs/{job.id}?noselection=1"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert job.candidates == []


def test_approve_refuses_parked_upgrade_review_without_credentials(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    creds = {"auth_token": "dummy", "user_id": "dummy"}
    monkeypatch.setattr(webapp, "_read_creds", lambda: dict(creds))
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    creds.clear()

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert any(c.get("selected") for c in job.candidates)


def test_approve_refuses_parked_library_review_without_credentials(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda j, chosen: None
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})
    jm.registry.add(job)
    try:
        r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/library?error=")
        assert job.status == jm.JobStatus.AWAITING_REVIEW
        assert job.candidates[0]["selected"]
    finally:
        _remove_job(job)


def test_auth_failure_before_any_import_reparks_the_review():
    """Qobuz dying on the FIRST album of an approved run must not consume the
    review — the picks go back to awaiting-review instead of a failed job."""
    from qobuz_librarian.api.auth import AuthLost

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})

    def _dies(j, chosen):
        raise AuthLost("token rejected")

    job._execute_fn = _dies
    jm.registry.add(job)
    try:
        jm.start_worker()
        assert jm.approve(job, None) is True
        assert _wait_for(lambda: any(
            "untouched" in line for line in job.log_lines))
        assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
        assert job.candidates[0]["selected"]
        assert job.finished_at is None
        assert job.error is None
    finally:
        _remove_job(job)


def test_auth_failure_after_an_import_keeps_fail_semantics():
    from qobuz_librarian.api.auth import AuthLost

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})

    def _dies_late(j, chosen):
        j._imported_any = True
        raise AuthLost("token rejected mid-run")

    job._execute_fn = _dies_late
    jm.registry.add(job)
    try:
        jm.start_worker()
        assert jm.approve(job, None) is True
        assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    finally:
        _remove_job(job)


def test_incomplete_upgrade_state_is_not_reviewable(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": False,
            "candidates": [{
                "title": "Partial",
                "artist": "Portishead",
                "detail": "stale",
                "payload": {"album_id": "up1"},
            }],
        },
    )

    r = client.post("/upgrade/review", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/upgrade"
    assert job_mgr.registry.awaiting_review() == []


def test_downsample_page_reviews_saved_shared_candidates(client, monkeypatch):
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                "album_dir": "/music/Portishead/Dummy",
                "est_saving": 1234,
            }],
        },
    )

    r = client.get("/downsample")

    assert r.status_code == 200
    assert "can be downsampled" in r.text
    assert "1 album can be downsampled" in r.text
    assert 'action="/downsample/review"' in r.text
    assert "Review candidates" in r.text
    assert "Start downsample scan" not in r.text


def test_downsample_review_post_uses_saved_state_without_scanning(client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                "album_dir": "/music/Portishead/Dummy",
                "est_saving": 1234,
            }],
        },
    )

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("Downsample review must not start a scan")

    monkeypatch.setattr("qobuz_librarian.web.flows.scan_downsamples", fail_scan)

    r = client.post("/downsample/review", follow_redirects=False)

    assert r.status_code == 303
    job_id = r.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    assert job is not None
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert job.execute_kind == "downsample"
    assert job.review_verb == "Downsample"
    assert len(job.candidates) == 1
    assert job.candidates[0]["selected"] is False


def test_downsample_review_post_reuses_existing_saved_review_job(
        client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                "album_dir": "/music/Portishead/Dummy",
                "est_saving": 1234,
            }],
        },
    )

    first = client.post("/downsample/review", follow_redirects=False)
    second = client.post("/downsample/review", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "downsample"
    ]) == 1


def test_downsample_saved_review_respects_hidden_candidates(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [
                {
                    "title": "Dummy",
                    "artist": "Portishead",
                    "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                    "album_dir": "/music/Portishead/Dummy",
                    "est_saving": 1234,
                },
                {
                    "title": "Third",
                    "artist": "Portishead",
                    "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                    "album_dir": "/music/Portishead/Third",
                    "est_saving": 5678,
                },
            ],
        },
    )

    first = client.post("/downsample/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Dummy")
    client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
    client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})

    store = hidden.load()
    assert hidden.is_hidden(hidden.SCOPE_DOWNSAMPLE, "Portishead", "Third", store)
    assert [c["title"] for c in job.candidates] == ["Dummy"]

    r = client.get("/downsample")
    assert r.status_code == 200
    assert "1 album can be downsampled" in r.text
    assert "2 albums can be downsampled" not in r.text

    second = client.post("/downsample/review", follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "downsample"
    ]) == 1


def test_downsample_saved_review_restore_updates_existing_job(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [
                {
                    "title": "Dummy",
                    "artist": "Portishead",
                    "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                    "album_dir": "/music/Portishead/Dummy",
                    "est_saving": 1234,
                },
                {
                    "title": "Third",
                    "artist": "Portishead",
                    "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                    "album_dir": "/music/Portishead/Third",
                    "est_saving": 5678,
                },
            ],
        },
    )

    first = client.post("/downsample/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Dummy")
    client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
    client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
    hidden.restore_albums(hidden.SCOPE_DOWNSAMPLE, [
        hidden.album_fingerprint("Portishead", "Third")
    ])

    second = client.post("/downsample/review", follow_redirects=False)

    assert second.headers["location"] == first.headers["location"]
    assert [c["title"] for c in job.candidates] == ["Dummy", "Third"]
    assert {c["title"]: c["selected"] for c in job.candidates} == {
        "Dummy": True,
        "Third": False,
    }
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "downsample"
    ]) == 1


def test_downsample_approve_resyncs_saved_review_before_execution(
        client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", "delete")
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    state = {
        "updated_at": time.time(),
        "complete": True,
        "candidates": [{
            "title": "Stale",
            "artist": "Portishead",
            "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
            "album_dir": "/music/Portishead/Stale",
            "est_saving": 1234,
        }],
    }
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

    first = client.post("/downsample/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    state["candidates"] = []

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == f"/jobs/{job.id}?noselection=1"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert job.candidates == []


def test_first_downsample_prompts_for_keep_choice_then_saves_it(
        client, monkeypatch, tmp_path):
    """With keep-originals still unchosen, approving a downsample shows the
    one-time prompt instead of rewriting anything; picking one saves it to the
    real setting and the run proceeds, so a returning user is never asked again."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(job_mgr._scan_queue, "put", lambda item: None)
    state = {
        "updated_at": time.time(), "complete": True,
        "candidates": [{
            "title": "Album", "artist": "Portishead",
            "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
            "album_dir": "/music/Portishead/Album", "est_saving": 1234,
        }],
    }
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

    first = client.post("/downsample/review", follow_redirects=False)
    job = job_mgr.registry.get(first.headers["location"].removeprefix("/jobs/"))
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)
    assert r.status_code == 200
    assert "Before your first downsample" in r.text
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None

    r2 = client.post(f"/jobs/{job.id}/approve",
                     data={"keep_choice": "keep"}, follow_redirects=False)
    assert r2.status_code == 303
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"


def test_first_downsample_keep_choice_applies_while_a_job_is_running(
        client, monkeypatch, tmp_path):
    """The keep/delete pick made at the first-downsample prompt must take
    effect for the run it launches even when another job is already active.
    settings_store.save() defers its in-memory apply while a job runs, so the
    approve path applies the choice itself — otherwise the run reads the still
    unset value and deletes the hi-res originals despite a 'keep' choice."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_pending_apply", None)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(job_mgr._scan_queue, "put", lambda item: None)
    # A job is running in the other lane, so save() takes its deferral branch —
    # the exact condition that used to strand the choice at its unset default.
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    state = {
        "updated_at": time.time(), "complete": True,
        "candidates": [{
            "title": "Album", "artist": "Portishead",
            "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
            "album_dir": "/music/Portishead/Album", "est_saving": 1234,
        }],
    }
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

    first = client.post("/downsample/review", follow_redirects=False)
    job = job_mgr.registry.get(first.headers["location"].removeprefix("/jobs/"))
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})

    r = client.post(f"/jobs/{job.id}/approve",
                    data={"keep_choice": "keep"}, follow_redirects=False)
    assert r.status_code == 303
    # Applied in-memory immediately despite the deferral: the launched run
    # reads "keep" and parks a restorable backup rather than deleting.
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"


def test_approve_refuses_parked_downsample_review_without_engine(
        client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                "album_dir": "/music/Portishead/Dummy",
                "est_saving": 1234,
            }],
        },
    )

    first = client.post("/downsample/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", False)

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/downsample"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW


def test_incomplete_downsample_state_is_not_reviewable(client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": False,
            "candidates": [{
                "title": "Partial",
                "artist": "Portishead",
                "detail": "stale",
                "album_dir": "/music/Portishead/Partial",
                "est_saving": 1234,
            }],
        },
    )

    r = client.post("/downsample/review", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/downsample"
    assert job_mgr.registry.awaiting_review() == []


def test_search_bar_stays_usable_on_phones(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    assert 'class="ql-search-row"' in r.text
    assert "Artist name" in r.text
    # No autofocus: popping the keyboard on every visit is hostile on mobile.
    assert "autofocus" not in r.text
    # The Search button keeps a visible text label at every width, instead of
    # collapsing to an icon behind a sm: breakpoint.
    assert '<span>Search</span>' in r.text
    assert '<span class="hidden sm:inline">Search</span>' not in r.text


def test_dashboard_first_run_offers_baseline_scan_with_skip(client, monkeypatch):
    # On first run the dashboard OFFERS the baseline scan (Scan / Skip) rather
    # than auto-starting it. The library folder must be ready for the offer to
    # render, so give it artists.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr("qobuz_librarian.library.scanner.list_library_artists",
                        lambda: ["Some Artist"])
    monkeypatch.setattr(cfg, "AUTO_LIBRARY_SCAN", True)
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    monkeypatch.setattr(new_releases, "auto_scan_attempted", lambda: False)

    r = client.get("/")

    assert r.status_code == 200
    assert "Scan library" in r.text
    assert "Builds the Missing Albums, Gap Fill, Upgrade, and Downsample reviews." in r.text
    assert 'action="/library/skip-setup"' in r.text and "Not now" in r.text
    # It's an offer, not an auto-started scan.
    assert "Your baseline scan is running" not in r.text


def test_dashboard_first_run_offer_does_not_submit_baseline_scan(
        client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import new_releases, scan_checkpoint
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr("qobuz_librarian.library.scanner.list_library_artists",
                        lambda: ["Some Artist"])
    monkeypatch.setattr(cfg, "AUTO_LIBRARY_SCAN", True)
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    monkeypatch.setattr(new_releases, "auto_scan_attempted", lambda: False)
    monkeypatch.setattr(scan_checkpoint, "pending", lambda: None)

    def fail_start_library_scan(*args, **kwargs):
        pytest.fail("dashboard first-run offer must not start a library scan")

    monkeypatch.setattr(webapp, "_start_library_scan", fail_start_library_scan)

    r = client.get("/")

    assert r.status_code == 200
    assert "Scan library" in r.text


def test_library_force_full_post_starts_forced_scan(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 1, "message": ""})
    started = {}

    def fake_start_library_scan(*, partial_only=False, force_full=False):
        job = jm.Job(title="scan")
        started["partial_only"] = partial_only
        started["force_full"] = force_full
        return job

    monkeypatch.setattr(webapp, "_start_library_scan", fake_start_library_scan)

    r = client.post(
        "/library",
        data={"mode": "missing_albums", "force_full": "1"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert started == {"partial_only": False, "force_full": True}


def test_non_htmx_search_post_returns_to_dashboard(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [])

    r = client.post("/search", data={"q": "Paysage d'Hiver"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_queue_empty_state_has_clear_actions(client):
    r = client.get("/queue")

    assert r.status_code == 200
    assert "Queue is empty." in r.text
    assert "Downloads and scans appear here" in r.text
    assert ">Search Qobuz</a>" in r.text
    assert ">View history</a>" in r.text


def test_queue_shows_empty_state_when_only_parked_reviews_exist(client):
    # Parked reviews render on their own surfaces, not in the queue — so a
    # registry holding nothing but a parked review must still show the queue's
    # empty state rather than a blank page (the stack wrapper with no sections).
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Downsample scan")
    review.execute_kind = "downsample"

    r = client.get("/queue")

    assert r.status_code == 200
    assert "Queue is empty." in r.text


def test_queue_job_actions_use_clear_labels(client):
    queued = _inject_job(jm.JobStatus.PENDING, "Queued scan")
    running = _inject_job(jm.JobStatus.RUNNING, "Running scan")
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Review scan")
    review.execute_kind = "library"
    review.add_candidate(kind="album", title="Dummy", artist="Portishead",
                         payload={"year": "1994"}, selected=False)
    downsample = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Downsample scan")
    downsample.execute_kind = "downsample"
    downsample.add_candidate(kind="album", title="Dummy", artist="Portishead",
                             payload={"year": "1994"}, selected=False)
    migration = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library migration")
    migration.execute_kind = "migration"
    migration.add_candidate(kind="album", title="Dummy", artist="Portishead",
                            payload={"year": "1994"}, selected=False)
    try:
        r = client.get("/queue")

        assert r.status_code == 200
        # Parked reviews live on their surfaces and in History, not the queue.
        assert 'id="queue-review-heading"' not in r.text
        assert "Review scan" not in r.text
        assert "Downsample scan" not in r.text
        assert 'id="queue-active-heading"' in r.text and "Running now" in r.text
        assert 'id="queue-waiting-heading"' in r.text and "Waiting" in r.text
        assert ">Clear the queue</button>" in r.text
        assert "parked reviews are untouched" in r.text
        assert "Starts automatically after the current job finishes." in r.text
        assert "hi-res album to review" not in r.text
        assert "album folder to review" not in r.text
        assert "Waiting for “Running scan”" not in r.text
        assert ">Remove</button>" in r.text
        assert 'aria-label="Remove from queue"' in r.text
        assert ">Cancel</button>" in r.text
        assert 'data-confirm="Cancel this job? It will stop after the current safe step."' in r.text
        assert 'data-confirm="Remove this waiting job from the queue?"' in r.text
        assert "Its 1 result are" not in r.text
        assert ">×</button>" not in r.text
        assert "loading loading-spinner" not in r.text
        assert "ql-btn-secondary" in r.text
        assert "btn-outline" not in r.text
    finally:
        _remove_job(queued)
        _remove_job(running)
        _remove_job(review)
        _remove_job(downsample)
        _remove_job(migration)


def test_history_empty_state_has_clear_action(client):
    r = client.get("/queue/history")

    assert r.status_code == 200
    assert "No finished jobs yet." in r.text
    assert "Completed downloads, scans, and reviews appear here." in r.text
    assert ">Back to Queue</a>" in r.text


def test_history_retry_shows_for_archived_failed_download_too(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(title="Archived failure", artist="Portishead",
                      album_id="archived")
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)

    live = jm.Job(title="Live failure", artist="Portishead", album_id="live")
    live.status = jm.JobStatus.FAILED
    live.finished_at = time.time()
    jm.registry.add(live)
    try:
        r = client.get("/queue/history")

        assert r.status_code == 200
        assert f'action="/jobs/{live.id}/retry"' in r.text
        assert f'action="/jobs/{archived.id}/retry"' in r.text
    finally:
        _remove_job(live)


def test_persist_never_records_half_updated_bulk_selection(monkeypatch):
    import json
    import threading

    from qobuz_librarian.web import job_persistence

    serializing = threading.Event()
    release_dump = threading.Event()
    selection_attempted = threading.Event()
    stored = {}

    class PauseDuringDump:
        def __str__(self):
            serializing.set()
            release_dump.wait(timeout=5)
            return "pause"

    class TrackedRLock:
        def __init__(self):
            self._lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "bulk-selection":
                selection_attempted.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_exc):
            self._lock.release()

    class Connection:
        def execute(self, _sql, values):
            stored["candidates"] = values[7]

        def commit(self):
            pass

    monkeypatch.setattr(job_persistence, "_get_conn", lambda: Connection())
    job = jm.Job(title="selection snapshot")
    job._lock = TrackedRLock()
    job.candidates = [
        {"cid": "c1", "selected": False, "pause": PauseDuringDump()},
        {"cid": "c2", "selected": False},
    ]

    persist = threading.Thread(
        target=job_persistence.persist, args=(job,), name="candidate-persist"
    )
    select = threading.Thread(
        target=job.set_all_selected, args=(True,), name="bulk-selection"
    )
    persist.start()
    assert serializing.wait(timeout=5)
    select.start()
    assert selection_attempted.wait(timeout=5)
    release_dump.set()
    persist.join(timeout=5)
    select.join(timeout=5)

    assert not persist.is_alive()
    assert not select.is_alive()
    values = [row["selected"] for row in json.loads(stored["candidates"])]
    assert values in ([False, False], [True, True])


def test_older_persist_cannot_overtake_newer_selection(monkeypatch):
    import json
    import threading

    from qobuz_librarian.web import job_persistence

    old_snapshot_ready = threading.Event()
    release_old = threading.Event()
    writes = []

    class PauseAfterCandidateDump:
        def __str__(self):
            if threading.current_thread().name == "older-persist":
                old_snapshot_ready.set()
                release_old.wait(timeout=5)
            return "pause"

    class Connection:
        def execute(self, _sql, values):
            writes.append([
                row["selected"] for row in json.loads(values[7])
            ])

        def commit(self):
            pass

    monkeypatch.setattr(job_persistence, "_get_conn", lambda: Connection())
    job = jm.Job(title="ordered selection saves")
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.candidates = [
        {"cid": "c1", "selected": False},
        {"cid": "c2", "selected": False},
    ]
    job.execute_args = {"pause": PauseAfterCandidateDump()}

    older = threading.Thread(
        target=job_persistence.persist, args=(job,), name="older-persist"
    )

    def select_and_persist():
        job.set_all_selected(True)
        job_persistence.persist(job)

    newer = threading.Thread(target=select_and_persist, name="newer-persist")
    older.start()
    assert old_snapshot_ready.wait(timeout=5)
    newer.start()
    watchdog = threading.Timer(0.2, release_old.set)
    watchdog.start()
    try:
        older.join(timeout=5)
        newer.join(timeout=5)
    finally:
        release_old.set()
        watchdog.cancel()

    assert not older.is_alive()
    assert not newer.is_alive()
    assert writes[-1] == [True, True]


def test_archived_job_page_keeps_retry_and_undo(client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    failed = jm.Job(title="Archived album", artist="Portishead",
                    album_id="album-id")
    failed.status = jm.JobStatus.FAILED
    failed.finished_at = time.time()
    job_persistence.persist(failed)

    single = jm.Job(title="Archived single", artist="Portishead")
    single.status = jm.JobStatus.DONE
    single.single = {
        "dir": "/music/Portishead/Dummy", "track_id": "t1",
        "owned_path": {
            "relative": "01 - Track.flac",
            "directories": [[1, 2]],
            "file": {
                "device": 1, "inode": 3, "size": 4,
                "modified_ns": 5, "changed_ns": 6,
            },
        },
    }
    single.finished_at = time.time()
    job_persistence.persist(single)

    r = client.get(f"/jobs/{failed.id}")
    assert r.status_code == 200
    assert "This job is archived." in r.text
    assert ">Retry</button>" in r.text

    r = client.get(f"/jobs/{single.id}")
    assert r.status_code == 200
    assert f'action="/jobs/{single.id}/undo"' in r.text


def test_retry_rebuilds_archived_failed_download(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        lambda album_id, token: {"title": "Dummy",
                                 "artist": {"name": "Portishead"},
                                 "tracks": {"items": []}})
    monkeypatch.setattr(webapp, "_make_download_run",
                        lambda album, token, treat_as_new=False: lambda j: None)

    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    new_id = r.headers["location"].removeprefix("/jobs/")
    assert new_id and new_id != archived.id
    new_job = jm.registry.get(new_id)
    assert new_job is not None and new_job.album_id == "al1"
    _remove_job(new_job)


def test_retry_keeps_the_new_edition_override(client, monkeypatch):
    # "Download this edition anyway" lives on the job (execute_args), not just
    # in the run closure — a retried edition download that lost the flag would
    # hit the owned-album skip and quietly do nothing.
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    archived.execute_args = {"new_edition": True}
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        lambda album_id, token: {"title": "Dummy",
                                 "artist": {"name": "Portishead"},
                                 "tracks": {"items": []}})
    seen = {}

    def fake_run(album, token, *, treat_as_new=False):
        seen["treat_as_new"] = treat_as_new
        return lambda j: None
    monkeypatch.setattr(webapp, "_make_download_run", fake_run)

    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    assert seen.get("treat_as_new") is True
    new_id = r.headers["location"].removeprefix("/jobs/")
    new_job = jm.registry.get(new_id)
    assert new_job is not None
    assert (new_job.execute_args or {}).get("new_edition") is True
    _remove_job(new_job)


def test_durable_retry_reuses_only_the_exact_interrupted_web_job(
        client, monkeypatch):
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.startup_recovery import (
        BlockedItemSettlementAction,
        StartupRecoveryAction,
        StartupRecoveryItem,
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    job = jm.Job(id="exact-resume", title="Album", album_id="al1")
    job.status = jm.JobStatus.FAILED
    job.attention = "recovery"
    jm.registry.add(job)
    attention = StartupRecoveryResult(
        StartupRecoveryStatus.ATTENTION_REQUIRED,
        (
            StartupRecoveryItem(
                "operation",
                "item",
                f"web-job:{job.id}",
                queue_state.QueuePhase.BLOCKED,
                StartupRecoveryAction.BLOCKED,
            ),
        ),
        "queue-item-blocked",
    )
    resume = StartupRecoveryResult(
        StartupRecoveryStatus.RESUME_REQUIRED,
        (
            StartupRecoveryItem(
                "operation",
                "item",
                f"web-job:{job.id}",
                queue_state.QueuePhase.PENDING,
                StartupRecoveryAction.PENDING,
            ),
        ),
    )
    saved_album = {
        "id": "al1",
        "title": "Saved Album",
        "artist": {"name": "Saved Artist"},
        "tracks": {"items": [{"id": "track-1", "title": "Saved Track"}]},
    }
    saved_planned = {
        "album": saved_album,
        "album_dir": None,
        "label": "Saved Artist — Saved Album",
        "missing": list(saved_album["tracks"]["items"]),
        "present": [],
        "upgrade_only": False,
        "auto_upgrade": False,
        "siblings_to_delete": [],
        "quality": None,
        "force_track_by_track": False,
    }
    queued_item = queue_state.JournalItem(
        "item",
        queue_state.QueuePhase.PENDING,
        saved_planned,
    )
    journal = queue_state.QueueJournal(
        "operation",
        f"web-job:{job.id}",
        "2026-07-13T00:00:00+00:00",
        (queued_item,),
    )
    recovery = [attention]
    binding_available = [False]
    monkeypatch.setattr(webapp, "_STARTUP_RECOVERY_RESULT", attention)
    monkeypatch.setattr(
        webapp,
        "_record_startup_recovery",
        lambda _authority: recovery[0],
    )
    monkeypatch.setattr(
        webapp,
        "_startup_recovery_origin_value",
        lambda: "web-job",
    )
    monkeypatch.setattr(
        webapp,
        "_durable_recovery_matches_job",
        lambda candidate: candidate is job,
    )
    monkeypatch.setattr(
        webapp,
        "_durable_resume_allowed",
        lambda candidate, **_kwargs: candidate == job.id,
    )
    monkeypatch.setattr(
        webapp,
        "_durable_recovery_control",
        lambda: {
            "job_id": job.id,
            "operation_id": "operation",
            "item_id": "item",
            "status": recovery[0].status.value,
        },
    )
    monkeypatch.setattr(
        webapp,
        "_startup_recovery_binding",
        lambda: (
            (resume.items[0], journal, queued_item, None)
            if recovery[0] is resume and binding_available[0]
            else None
        ),
    )
    settlements = []

    def settle(candidate, action):
        settlements.append((candidate, action))
        recovery[0] = resume
        webapp._STARTUP_RECOVERY_RESULT = resume
        return True, "The item can be retried."

    monkeypatch.setattr(webapp, "_settle_durable_web_recovery", settle)
    monkeypatch.setattr(webapp, "_find_job_touching_album", lambda _id: None)
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    album_calls = []

    def get_album(_album_id, _token):
        album_calls.append(_album_id)
        raise AssertionError(
            "durable Retry must not rebuild from mutable Qobuz data"
        )

    monkeypatch.setattr("qobuz_librarian.api.search.get_album", get_album)
    submitted = []
    monkeypatch.setattr(
        jm,
        "resubmit_failed",
        lambda candidate, candidate_run: (
            submitted.append((candidate, candidate_run)) or True
        ),
    )
    monkeypatch.setattr(
        jm,
        "submit",
        lambda *_args, **_kwargs: pytest.fail(
            "durable retry must not mint a replacement job"
        ),
    )
    try:
        stale = client.post(
            f"/jobs/{job.id}/retry",
            data={
                "recovery_operation_id": "operation",
                "recovery_item_id": "stale-item",
            },
            follow_redirects=False,
        )
        assert stale.status_code == 503
        assert submitted == []
        assert settlements == []

        page = client.get(f"/jobs/{job.id}")
        assert page.status_code == 200
        assert 'name="recovery_operation_id" value="operation"' in page.text
        assert 'name="recovery_item_id" value="item"' in page.text

        interrupted = client.post(
            f"/jobs/{job.id}/retry",
            data={
                "recovery_operation_id": "operation",
                "recovery_item_id": "item",
            },
            follow_redirects=False,
        )
        assert interrupted.status_code == 503
        assert settlements == [(job, BlockedItemSettlementAction.RETRY)]
        assert submitted == []
        assert album_calls == []

        resumed_page = client.get(f"/jobs/{job.id}")
        assert 'name="recovery_operation_id" value="operation"' in resumed_page.text
        binding_available[0] = True
        response = client.post(
            f"/jobs/{job.id}/retry",
            data={
                "recovery_operation_id": "operation",
                "recovery_item_id": "item",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert response.headers["location"] == f"/jobs/{job.id}"
        assert len(submitted) == 1 and submitted[0][0] is job
        assert settlements == [(job, BlockedItemSettlementAction.RETRY)]
        assert album_calls == []

        # Execute the submitted closure far enough to prove it reconstructs
        # the exact journal plan. Mutable catalogue/local discovery must not
        # run again, and executor equality remains the final admission gate.
        from contextlib import nullcontext

        monkeypatch.setattr(jm, "staging_lock", lambda: nullcontext())
        monkeypatch.setattr(
            "qobuz_librarian.library.catalog.find_existing_tracks",
            lambda *_a, **_k: pytest.fail(
                "durable Retry must not rediscover mutable local state"
            ),
        )
        monkeypatch.setattr(
            "qobuz_librarian.modes.process.process_album",
            lambda *_a, **_k: pytest.fail(
                "durable Retry must use the exact queue executor"
            ),
        )
        executed = []

        def execute(queue, _args, token, **_kwargs):
            executed.append((queue[0], token))
            return ([{"result": "retry", "imported": False}], False)

        monkeypatch.setattr(
            "qobuz_librarian.queue.executor._execute_download_queue",
            execute,
        )
        monkeypatch.setattr(
            webapp,
            "_durable_completion_status",
            lambda _job: False,
        )
        submitted[0][1](job)

        assert len(executed) == 1 and executed[0][1] == "tok"
        assert queue_state._serialize_queue_item(executed[0][0]) == saved_planned
    finally:
        _remove_job(job)


def test_retry_refreshes_recovery_before_acknowledged_job_is_reconciled(
        client, monkeypatch):
    from qobuz_librarian.completion import RecoveryOwner
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    job = jm.Job(id="stale-clear", title="Album", album_id="al1")
    job.status = jm.JobStatus.FAILED
    jm.registry.add(job)
    assert job_persistence.acknowledge_durable_completion(
        job.id,
        RecoveryOwner("operation", "item"),
        album_id="al1",
        completion_hash="a" * 64,
    )
    attention = StartupRecoveryResult(
        StartupRecoveryStatus.ATTENTION_REQUIRED,
        reason="startup-recovery-unsettled",
    )
    refreshed = []

    def record(authority):
        refreshed.append(authority)
        webapp._STARTUP_RECOVERY_RESULT = attention
        return attention

    monkeypatch.setattr(webapp, "_record_startup_recovery", record)
    monkeypatch.setattr(
        webapp,
        "_reconcile_acknowledged_job",
        lambda *_a, **_k: pytest.fail(
            "stale CLEAR promoted an acknowledged job"),
    )
    try:
        response = client.post(
            f"/jobs/{job.id}/retry", follow_redirects=False)

        assert response.status_code == 503
        assert refreshed == [webapp._RUN_LOCK_HANDLE]
        assert job.status is jm.JobStatus.FAILED
        assert "recovery proof is not settled" in response.text
    finally:
        _remove_job(job)


def test_retry_rechecks_recovery_after_album_fetch_before_submit(
        client, monkeypatch):
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp

    job = jm.Job(id="retry-race", title="Album", album_id="al1")
    job.status = jm.JobStatus.FAILED
    jm.registry.add(job)
    states = [
        StartupRecoveryResult(StartupRecoveryStatus.CLEAR),
        StartupRecoveryResult(StartupRecoveryStatus.ATTENTION_REQUIRED),
    ]
    refreshed = []

    def record(authority):
        refreshed.append(authority)
        result = states.pop(0)
        webapp._STARTUP_RECOVERY_RESULT = result
        return result

    monkeypatch.setattr(webapp, "_record_startup_recovery", record)
    monkeypatch.setattr(webapp, "_durable_completion_status", lambda _j: False)
    monkeypatch.setattr(webapp, "_find_job_touching_album", lambda _id: None)
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        lambda _album_id, _token: {
            "title": "Album",
            "artist": {"name": "Artist"},
            "tracks": {"items": []},
        },
    )
    monkeypatch.setattr(
        jm,
        "submit",
        lambda *_a, **_k: pytest.fail(
            "stale CLEAR recovery must not submit a replacement job"
        ),
    )
    try:
        response = client.post(
            f"/jobs/{job.id}/retry", follow_redirects=False)

        assert response.status_code == 503
        assert "needs recovery attention" in response.text
        assert refreshed == [
            webapp._RUN_LOCK_HANDLE,
            webapp._RUN_LOCK_HANDLE,
        ]
    finally:
        _remove_job(job)


def test_recovery_attention_job_has_no_backend_retry_lane(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    job = jm.Job(id="recovery-attention", title="Album", album_id="al1")
    job.status = jm.JobStatus.FAILED
    job.attention = "recovery"
    jm.registry.add(job)
    monkeypatch.setattr(webapp, "_durable_completion_status", lambda _j: False)
    monkeypatch.setattr(
        webapp,
        "_get_token",
        lambda: pytest.fail("recovery-attention job reached album fetch"),
    )
    try:
        response = client.post(
            f"/jobs/{job.id}/retry", follow_redirects=False)

        assert response.status_code == 503
        assert "exact recovery Retry control" in response.text
    finally:
        _remove_job(job)


def test_undo_burns_the_one_shot_in_the_archive(client, monkeypatch, tmp_path):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    gone = tmp_path / "Portishead" / "Dummy"
    job = jm.Job(title="Dummy", artist="Portishead")
    job.status = jm.JobStatus.DONE
    job.single = {"dir": str(gone), "track_id": "t1", "title": "Glory Box"}
    job.finished_at = time.time()
    job_persistence.persist(job)

    r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)

    assert r.status_code == 303
    row = job_persistence.load_one(job.id)
    assert row is not None
    assert row["single"].get("removed") is True


def test_undo_bounces_when_the_staging_mutex_is_held(client, monkeypatch, tmp_path):
    """Undo behind a long staging-lock holder (library-wide Lyrics scan,
    migration) must bounce naming the holder instead of hanging the request
    until the holder finishes — the DONE job page can't show progress, so a
    blocking wait is invisible. The timer below is a watchdog: without the fix
    the request blocks on the held lock, the timer releases it, the undo runs
    to completion and the 503 assert fails instead of the test hanging."""
    import threading

    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    gone = tmp_path / "Portishead" / "Dummy"
    job = jm.Job(title="Dummy", artist="Portishead")
    job.status = jm.JobStatus.DONE
    job.single = {"dir": str(gone), "track_id": "t1", "title": "Glory Box"}
    job.finished_at = time.time()
    job_persistence.persist(job)

    lock = jm.staging_lock()
    lock.acquire()
    jm.set_staging_holder("Lyrics scan")
    release_timer = threading.Timer(3.0, lock.release)
    release_timer.start()
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)

        assert r.status_code == 503
        assert "Lyrics scan" in r.text
        row = job_persistence.load_one(job.id)
        assert not row["single"].get("removed")
    finally:
        jm.set_staging_holder(None)
        release_timer.cancel()
        try:
            lock.release()
        except RuntimeError:
            pass


def test_hidden_empty_state_points_back_to_library(client):
    r = client.get("/library/hidden")

    assert r.status_code == 200
    assert "No dismissed results." in r.text
    assert ">Go to Library</a>" in r.text


def test_repair_history_empty_state_points_back_to_repair(client):
    r = client.get("/repair/history")

    assert r.status_code == 200
    assert "Nothing repaired yet." in r.text
    assert ">Back to Repair</a>" in r.text


# ── per-job cancel button on queue page ───────────────────────────────

def _inject_job(status, title="Test Job"):
    """Add a job directly to the shared registry and return it.
    Caller must remove the job in a finally block."""
    job = jm.Job(title=title, status=status)
    jm.registry.add(job)
    return job


def _remove_job(job):
    with jm.registry._lock:
        jm.registry._jobs.pop(job.id, None)
        try:
            jm.registry._order.remove(job.id)
        except ValueError:
            pass


def test_dashboard_active_non_download_job_uses_neutral_wording(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    job = jm.Job(title="Lyrics sweep", status=jm.JobStatus.RUNNING)
    job.execute_kind = "lyrics"
    job.push_progress("Fetching lyrics", 3, 12, unit="track")
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: [job])

    r = client.get("/")

    assert r.status_code == 200
    assert "Fetching lyrics" in r.text
    assert "Fetching lyrics 3 / 12 tracks" in r.text
    assert 'aria-label="Cancel job"' in r.text
    assert ">Cancel</button>" in r.text
    assert 'data-confirm="Cancel this job? It will stop after the current safe step."' in r.text
    assert "Cancel download" not in r.text
    assert "Cancel scan" not in r.text


def test_library_hide_then_restore_round_trip(client, monkeypatch, tmp_path):
    """Dismissing an artist from a library review writes the durable store and
    drops those candidates; the Dismissed albums and Gap Fill view then restores them."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    c_dummy = job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                                payload={"year": "1994"}, selected=False)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007"}, selected=False)
    try:
        # Selection is server-backed: tick Dummy via the select endpoint, then
        # dismissing unselected Portishead albums drops only Third, keeps the
        # ticked Dummy, and never touches Burial.
        r = client.post(f"/jobs/{job.id}/select",
                        data={"cid": c_dummy, "checked": "1"})
        assert r.status_code == 200 and r.json()["selected"] == 1
        r = client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
        assert r.status_code == 200
        survivors = {c["artist"] + "/" + c["title"]: c["selected"]
                     for c in job.candidates}
        assert survivors == {"Portishead/Dummy": True, "Burial/Untrue": False}
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Burial", "Untrue", store)

        r = client.get("/library/hidden")
        assert r.status_code == 200
        assert "Portishead" in r.text
        assert 'href="/?kind=artist&q=Portishead"' in r.text
        assert 'href="/artist?artist=Portishead"' not in r.text

        r = client.post("/library/hidden/restore", data={"artist": "Portishead"})
        assert r.status_code == 200  # follows the 303 to the dismissed-items view
        assert hidden.count(hidden.SCOPE_MISSING) == 0
    finally:
        _remove_job(job)


def test_library_hide_scoped_to_review_tab(client, monkeypatch, tmp_path):
    """A library review with both missing albums and Gap Fill splits into tabs,
    and dismissing an artist's unselected rows from one tab must not silently
    drop that artist's candidates on the other tab."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      detail="1994 · CD 16-bit/44.1kHz · gap-fill: 2 missing of 11",
                      payload={"year": "1994", "gap_fill": 2}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        assert "Missing Albums" in r.text and "Gap Fill" in r.text
        # The default tab shows only the missing album, not the gap fill row.
        assert "Third" in r.text and "Dummy" not in r.text
        r = client.get(f"/jobs/{job.id}/review", params={"tab": "gaps"},
                       headers={"HX-Request": "true"})
        assert "Dummy" in r.text and "Third" not in r.text

        r = client.post(f"/jobs/{job.id}/hide",
                        data={"artist": "Portishead", "tab": "missing"})
        assert r.status_code == 200
        assert [c["title"] for c in job.candidates] == ["Dummy"]
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    finally:
        _remove_job(job)


def test_library_approve_scoped_to_tab_splits_off_other_tab(client, monkeypatch):
    """Downloading from one tab must consume only that tab: the other tab's
    candidates (and their saved ticks) split into their own parked review
    instead of dying with the executing job."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"album_id": "third", "year": "2008"},
                      selected=True)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"album_id": "dummy", "year": "1994",
                               "gap_fill": 2}, selected=True)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"album_id": "untrue", "year": "2007",
                               "gap_fill": 1}, selected=False)
    split = None
    try:
        r = client.post(f"/jobs/{job.id}/approve", data={"tab": "missing"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?approved=1"
        # The approved job carries only the active tab's candidates.
        assert [c["title"] for c in job.candidates] == ["Third"]
        assert job.status != jm.JobStatus.AWAITING_REVIEW
        # The gap candidates live on in a new parked review, ticks intact.
        split = next(j for j in jm.registry.all()
                     if j is not job and j.execute_kind == "library"
                     and j.status == jm.JobStatus.AWAITING_REVIEW)
        titles = {c["title"]: c["selected"] for c in split.candidates}
        assert titles == {"Dummy": True, "Untrue": False}
        assert split._execute_fn is not None
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


@pytest.mark.parametrize("with_unique", [False, True])
def test_library_approve_keeps_albums_claimed_by_a_direct_download(
        client, monkeypatch, with_unique):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda _item: None)

    def duplicate_under_admission_lock(album_id, *_args, **_kwargs):
        assert webapp._DOWNLOAD_SUBMIT_LOCK.locked()
        return object() if album_id == "claimed" else None

    monkeypatch.setattr(
        webapp,
        "_duplicate_download_job",
        duplicate_under_admission_lock,
    )
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda _job, _chosen: None
    job.add_candidate(
        "album", "Claimed", "Artist",
        payload={"album_id": " claimed "}, selected=True,
    )
    if with_unique:
        job.add_candidate(
            "album", "Available", "Artist",
            payload={"album_id": "available"}, selected=True,
        )
    remnant = None
    try:
        response = client.post(
            f"/jobs/{job.id}/approve",
            follow_redirects=False,
        )

        if not with_unique:
            assert response.headers["location"] == "/library?noselection=1"
            assert job.status is jm.JobStatus.AWAITING_REVIEW
            assert [c["title"] for c in job.candidates] == ["Claimed"]
        else:
            assert response.headers["location"] == "/library?approved=1"
            assert [c["title"] for c in job.candidates] == ["Available"]
            remnant = next(
                candidate_job for candidate_job in jm.registry.awaiting_review()
                if candidate_job.id != job.id
                and candidate_job.execute_kind == "library"
            )
            assert [c["title"] for c in remnant.candidates] == ["Claimed"]
            assert remnant.candidates[0]["selected"] is True
    finally:
        _remove_job(job)
        if remnant is not None:
            _remove_job(remnant)


def test_search_download_prunes_parked_library_review(client, monkeypatch, tmp_path):
    """A Search download that imports an album must drop that album from a
    parked library review — otherwise the stale review offers to download it
    again. Other candidates and their ticks stay put."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "n_ok": 9})

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Third", artist="Portishead",
                         payload={"album_id": "q123", "year": "2008"},
                         selected=True)
    parked.add_candidate(kind="album", title="Dummy", artist="Portishead",
                         payload={"album_id": "q456", "year": "1994"},
                         selected=True)
    runner = _inject_job(jm.JobStatus.RUNNING)
    try:
        album = {"id": "q123", "title": "Third",
                 "artist": {"name": "Portishead"}}
        webapp._make_download_run(album, token="tok")(runner)
        assert runner.status != jm.JobStatus.FAILED
        flags = {c["title"]: c["selected"] for c in parked.candidates}
        assert flags == {"Dummy": True}
        assert parked.status == jm.JobStatus.AWAITING_REVIEW
    finally:
        _remove_job(parked)
        _remove_job(runner)


def test_library_approve_skips_candidates_already_on_disk(client, monkeypatch):
    """Approving a parked review re-checks the disk: a missing-album candidate
    whose folder appeared while the review sat parked is dropped (and counted
    in the redirect note) instead of downloaded again. Gap Fill candidates are
    exempt — their folder exists by definition."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/Third") if alb.get("id") == "q123"
        else None)
    # The faked folder stands in for a real one, so treat it as holding audio.
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog._count_audio_files_in", lambda d: 1)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"album_id": "q123", "year": "2008"},
                      selected=True)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"album_id": "q456", "year": "1994"},
                      selected=True)
    # An owned-looking gap candidate must survive the disk check.
    job.add_candidate(kind="album", title="Roseland NYC Live",
                      artist="Portishead",
                      payload={"album_id": "q123", "gap_fill": 2},
                      selected=False)
    split = None
    try:
        r = client.post(f"/jobs/{job.id}/approve", data={"tab": "missing"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?approved=1&skipped=1"
        assert [c["title"] for c in job.candidates] == ["Dummy"]
        split = next(j for j in jm.registry.all()
                     if j is not job and j.execute_kind == "library"
                     and j.status == jm.JobStatus.AWAITING_REVIEW)
        assert [c["title"] for c in split.candidates] == ["Roseland NYC Live"]
        # The note is rendered on /library.
        r = client.get("/library?approved=1&skipped=1")
        assert "1 album already in your library — skipped." in r.text
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


def test_library_approve_when_everything_is_already_on_disk(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/x"))
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog._count_audio_files_in", lambda d: 1)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"album_id": "q123"}, selected=True)
    try:
        r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?skipped=1"
        # Nothing left to review or download; the review completed quietly.
        assert job.candidates == []
        assert job.status != jm.JobStatus.AWAITING_REVIEW
    finally:
        _remove_job(job)


def test_drop_owned_keeps_a_missing_album_whose_folder_is_an_empty_shell(
        tmp_path, monkeypatch):
    """A fully-missing candidate whose only on-disk match is an empty folder —
    a failed download or deleted tracks that left the directory behind — stays
    in the review. A name-matching shell with no audio isn't ownership, and the
    scanner still lists that album missing; dropping it would hide a real gap."""
    from qobuz_librarian.web import flows

    shell = tmp_path / "Runnin' Wild (2019)"
    shell.mkdir()
    real = tmp_path / "Real Album (2010)"
    real.mkdir()
    (real / "01 - track.flac").write_bytes(b"\x00")

    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: shell if alb.get("id") == "empty1"
        else real if alb.get("id") == "real1" else None)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Runnin' Wild", artist="Airbourne",
                      payload={"album_id": "empty1"}, selected=True)
    job.add_candidate(kind="album", title="Real Album", artist="Airbourne",
                      payload={"album_id": "real1"}, selected=True)
    try:
        dropped = flows.drop_owned_missing_candidates(job)
        titles = [c["title"] for c in job.candidates]
        assert "Runnin' Wild" in titles
        assert "Real Album" not in titles
        assert dropped == 1
    finally:
        _remove_job(job)


def test_library_select_all_scoped_to_tab(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994", "gap_fill": 2}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "1", "scope": "all", "tab": "missing"})
        assert r.status_code == 200
        c = r.json()
        assert (c["missing_selected"], c["gap_selected"]) == (1, 0)
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": True, "Dummy": False}
    finally:
        _remove_job(job)


def test_review_select_rejects_a_job_that_already_left_review(
        client, monkeypatch):
    job = _inject_job(jm.JobStatus.PENDING)
    job.execute_kind = "library"
    cid = job.add_candidate(
        kind="album", title="Third", artist="Portishead", selected=False)
    persisted = []
    notified = []
    monkeypatch.setattr(jm, "persist_soon", lambda _job: persisted.append(True))
    monkeypatch.setattr(
        job, "notify_review_changed", lambda *_args: notified.append(True))
    try:
        response = client.post(
            f"/jobs/{job.id}/select", data={"cid": cid, "checked": "1"})

        assert response.status_code == 409
        assert job.candidates[0]["selected"] is False
        assert persisted == []
        assert notified == []
    finally:
        _remove_job(job)


def test_review_select_all_rejects_a_job_that_already_left_review(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    job = _inject_job(jm.JobStatus.PENDING)
    job.execute_kind = "library"
    job.add_candidate(
        kind="album", title="Third", artist="Portishead", selected=False)
    persisted = []
    notified = []
    monkeypatch.setattr(
        job_persistence, "persist", lambda _job: persisted.append(True) or True)
    monkeypatch.setattr(
        job, "notify_review_changed", lambda *_args: notified.append(True))
    try:
        response = client.post(
            f"/jobs/{job.id}/select-all", data={"on": "1", "scope": "all"})

        assert response.status_code == 409
        assert job.candidates[0]["selected"] is False
        assert persisted == []
        assert notified == []
    finally:
        _remove_job(job)


def test_select_all_reports_its_own_persistence_failure(client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    monkeypatch.setattr(job_persistence, "persist", lambda _job: False)
    try:
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "1", "scope": "all", "tab": "missing"})

        assert r.status_code == 200
        assert r.json()["persist_failed"] is True
        assert job.candidates[0]["selected"] is True
    finally:
        _remove_job(job)


def test_lazy_review_group_renders_current_artist_selection(client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007"}, selected=False)
    monkeypatch.setattr(job_persistence, "persist", lambda _job: True)
    try:
        page = client.get(f"/jobs/{job.id}")
        assert page.status_code == 200
        assert "data-lazy-items" in page.text
        assert 'class="ql-checkbox cb"' not in page.text

        selected = client.post(
            f"/jobs/{job.id}/select-all",
            data={"on": "1", "scope": "artist", "artist": "Portishead",
                  "tab": "missing"},
        )
        assert selected.status_code == 200

        items = client.get(
            f"/jobs/{job.id}/review-group-items",
            params={"artist": "Portishead", "tab": "missing"},
        )
        assert items.status_code == 200
        assert 'value="c0"' in items.text
        assert "checked" in items.text
        assert "Untrue" not in items.text
    finally:
        _remove_job(job)


def test_select_all_scoped_to_the_active_filter(client):
    """With a filter showing 3 rows, Select all must not silently flip
    the other thousand — and Deselect must scope the same way so a filtered
    select-all can be undone filtered."""
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                      payload={"year": "2006"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "1", "scope": "all", "tab": "missing",
                              "q": "agalloch"})
        assert r.status_code == 200
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": False, "Ashes": True}
        # Empty query keeps the whole-tab behavior.
        client.post(f"/jobs/{job.id}/select-all",
                    data={"on": "1", "scope": "all", "tab": "missing", "q": ""})
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": True, "Ashes": True}
    finally:
        _remove_job(job)


def test_dismiss_rest_scoped_to_the_active_filter(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                      payload={"year": "2006"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/dismiss-rest",
                        data={"tab": "missing", "q": "agalloch"})
        assert r.status_code == 200
        assert r.json()["hidden"] == 1
        titles = [c["title"] for c in job.candidates]
        assert titles == ["Third"]
    finally:
        from qobuz_librarian.library import hidden as hidden_mod
        hidden_mod.restore(hidden_mod.SCOPE_MISSING, ["Agalloch"])
        _remove_job(job)


def test_review_pages_split_on_a_candidate_budget():
    """Pagination counts candidates, not just artists — a few prolific
    artists must not put thousands of rows in one page's DOM. Whole groups
    stay together; a single over-budget group still gets its own page."""
    from qobuz_librarian.web import app as webapp

    big = [("Artist %d" % i, [{"cid": f"c{i}-{j}"} for j in range(900)])
           for i in range(4)]
    page1, page, n_pages = webapp._paginate_groups(big, 1)
    assert n_pages == 4  # 900+900 > 1500, so one group per page
    assert [a for a, _ in page1] == ["Artist 0"]
    monster = [("Huge", [{"cid": f"c{j}"} for j in range(3000)]),
               ("Small", [{"cid": "s1"}])]
    p1, _, n = webapp._paginate_groups(monster, 1)
    assert n == 2 and [a for a, _ in p1] == ["Huge"]


def test_mangled_query_param_renders_the_error_page(client):
    """A mangled page param (/library?page=abc) must answer with the styled
    error page, not raw
    framework validation JSON; API routes keep the JSON detail."""
    r = client.get("/library?page=abc")
    assert r.status_code == 400
    assert "text/html" in r.headers.get("content-type", "")
    assert "Bad request" in r.text
    r2 = client.get("/api/jobs?status=nonsense")
    assert "application/json" in r2.headers.get("content-type", "")


def test_filter_with_no_matches_says_so(client):
    """A filter matching nothing must render its message, not a void."""
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={})
    try:
        r = client.get(f"/jobs/{job.id}/review?q=zzz")
        assert r.status_code == 200
        assert "match your filter" in r.text
        # An empty tab with no filter keeps its own zero-state instead.
        r2 = client.get(f"/jobs/{job.id}/review?q=&tab=gaps")
        assert "match your filter" not in r2.text
    finally:
        _remove_job(job)


def test_discard_confirm_names_the_lost_picks(client):
    """The one guarded door in front of clearing the user's ticks must name the
    stake — not reassure about files. For a library review it also names the
    real recovery path (Bring all back), which the finished Library page adds."""
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={})
    try:
        r = client.get(f"/jobs/{job.id}")
        assert "Your ticks are cleared" in r.text
        assert "bring the whole review back" in r.text
        assert "Run the scan again to see these results later" not in r.text
    finally:
        _remove_job(job)


def test_review_zero_selection_has_clear_disabled_action(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        assert "Select candidates to download" in r.text
        assert "Download 1 selected" not in r.text
    finally:
        _remove_job(job)


def test_non_album_reviews_use_specific_action_language(client):
    repair = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Repair scan")
    repair.execute_kind = "repair"
    repair.review_verb = "Repair"
    repair.add_candidate(kind="repair", title="Damaged Album",
                         artist="Portishead",
                         detail="1 truncated track", selected=False)

    migration = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library migration")
    migration.execute_kind = "migration"
    migration.review_verb = "Move"
    migration.add_candidate(kind="migrate", title="Dummy (1994)",
                            artist="Portishead",
                            detail="10 tracks -> Portishead/Dummy (1994)",
                            selected=False)
    try:
        r = client.get(f"/jobs/{repair.id}")
        assert r.status_code == 200
        assert "Select repairs to run" in r.text
        assert "Discard repair review" in r.text
        assert "Select albums to repair" not in r.text
        assert "Discard scan" not in r.text

        r = client.get(f"/jobs/{migration.id}")
        assert r.status_code == 200
        assert "Select folders to move" in r.text
        assert "Discard migration preview" in r.text
        assert "Select albums to move" not in r.text
        assert "Discard scan" not in r.text
    finally:
        _remove_job(repair)
        _remove_job(migration)


def test_review_footer_renders_all_actions(client):
    # The footer carries every review action; the dismissed-albums link lives in
    # the summary row, not the action bar. (Layout/stacking is checked in a browser.)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=True)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        assert 'id="review-submit"' in r.text
        assert "Download 1 selected" in r.text
        assert 'id="review-dismiss-rest"' in r.text
        assert "Dismiss unselected (1)" in r.text
        assert "Dismissed albums and Gap Fill" in r.text
        assert ">Back to Library</a>" in r.text
        assert ">Discard library review</button>" in r.text
        assert 'href="/library/hidden"' in r.text
    finally:
        _remove_job(job)


def test_review_artist_header_carries_group_controls(client):
    # The select-artist checkbox lives in the group header (its own activation
    # runs instead of the summary's); the dismiss button must stay OUTSIDE the
    # summary, where a click can't fold the group.
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=True)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        summary = r.text.split('<summary class="ql-review-summary">', 1)[1].split("</summary>", 1)[0]
        assert 'data-artist-select value="Portishead"' in summary
        assert "data-hide" not in summary
        assert 'data-hide data-artist="Portishead"' in r.text
    finally:
        _remove_job(job)


def test_review_hides_page_select_when_everything_is_on_one_page(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=True)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        assert 'data-select-all="1"' in r.text
        assert "data-select-page" not in r.text
    finally:
        _remove_job(job)


def test_downsample_review_uses_keep_hi_res_language(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "downsample"
    job.review_verb = "Downsample"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994", "est_saving": 10}, selected=True)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008", "est_saving": 20}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        assert "Downsample 1 selected" in r.text
        assert "Keep hi-res (1)" in r.text
        assert "Kept hi-res" in r.text
    finally:
        _remove_job(job)


def test_library_dismiss_rest_hides_everything_unselected(client, monkeypatch, tmp_path):
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    keep = job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                             payload={"year": "1994"}, selected=False)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007"}, selected=False)
    job.add_candidate(kind="album", title="Mezzanine", artist="Massive Attack",
                      payload={"year": "1998"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
        assert r.status_code == 200

        r = client.post(f"/jobs/{job.id}/dismiss-rest")
        assert r.status_code == 200
        body = r.json()
        assert body["hidden"] == 3
        assert body["total"] == 1
        assert body["selected"] == 1
        assert body["review_done"] is False

        survivors = {c["artist"] + "/" + c["title"]: c["selected"] for c in job.candidates}
        assert survivors == {"Portishead/Dummy": True}

        store = hidden.load()
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Burial", "Untrue", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Massive Attack", "Mezzanine", store)
    finally:
        _remove_job(job)


def test_sse_done_event_carries_final_status(client):
    """The done event reports the job's real terminal status so the page can
    flip the badge to failed/canceled instead of assuming success."""
    job = jm.Job(title="failed-job")
    job.status = jm.JobStatus.FAILED
    jm.registry.add(job)
    try:
        with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
            assert r.status_code == 200
            seen = ""
            for chunk in r.iter_text():
                seen += chunk
                if "event: done" in seen:
                    break
            else:
                pytest.fail("SSE stream never sent 'event: done'")
        assert "data: failed" in seen
    finally:
        _remove_job(job)


def test_persistence_restores_awaiting_review_with_candidates(monkeypatch):
    """The headline reliability win: a completed scan's candidates survive a
    container restart — the user can still approve them instead of re-scanning
    from artist 1."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    # Simulate a scan that parked AWAITING_REVIEW before the container died.
    saved = jm.Job(title="Artist scan", artist="Foo")
    saved.kind = "scan"
    saved.execute_kind = "album"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.add_candidate("album", "Bar", "Foo", payload={"album_id": "abc"})
    job_persistence.persist(saved)

    # Drop the in-memory state to mimic the new process.
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    executed = {}

    def _factory(job, _args):
        return lambda j, chosen: executed.setdefault("ids", [
            c["payload"]["album_id"] for c in chosen])

    jm.restore_jobs({"album": _factory})

    restored = jm.registry.get(saved.id)
    assert restored is not None
    assert restored.status == jm.JobStatus.AWAITING_REVIEW
    assert len(restored.candidates) == 1
    assert restored.candidates[0]["payload"] == {"album_id": "abc"}

    # And the user can still approve — the execute_fn was rebound from the
    # kind registry rather than vanishing with the dead closure.
    jm.start_worker()
    assert jm.approve(restored, ["c0"]) is True
    assert _wait_for(lambda: restored.status == jm.JobStatus.DONE)
    assert executed.get("ids") == ["abc"]


@pytest.mark.parametrize("recovery_clear", [True, False])
def test_restore_promotes_acknowledged_download_only_after_clear_recovery(
        client, monkeypatch, recovery_clear):
    from qobuz_librarian.completion import RecoveryOwner
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(id="durable-web-job", title="Album", album_id="123")
    saved.status = jm.JobStatus.RUNNING
    assert job_persistence.persist(saved)
    assert job_persistence.acknowledge_durable_completion(
        saved.id,
        RecoveryOwner("operation", "item"),
        album_id="123",
        completion_hash="a" * 64,
    )

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({}, durable_recovery_clear=recovery_clear)

    restored = jm.registry.get(saved.id)
    assert restored is not None
    if recovery_clear:
        assert restored.status is jm.JobStatus.DONE
        assert restored.error is None
        assert job_persistence.load_one(saved.id)["status"] == "done"
    else:
        assert restored.status is jm.JobStatus.FAILED
        assert restored.attention == "recovery"
        assert "recovery" in restored.error.lower()
        assert job_persistence.load_one(saved.id)["status"] == "failed"
        assert f'action="/jobs/{saved.id}/retry"' not in client.get(
            f"/jobs/{saved.id}"
        ).text
        assert f'action="/jobs/{saved.id}/retry"' not in client.get(
            "/queue/history"
        ).text


def test_restore_retains_exact_durable_owner_past_finished_job_cap(monkeypatch):
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    owner = jm.Job(id="exact-resume", title="Interrupted", album_id="album-1")
    owner.status = jm.JobStatus.RUNNING
    assert job_persistence.persist(owner)
    for index in range(jm.JobRegistry.MAX_FINISHED + 5):
        ordinary = jm.Job(
            id=f"interrupted-{index}",
            title=f"Interrupted {index}",
            album_id=f"album-{index + 2}",
        )
        ordinary.status = jm.JobStatus.RUNNING
        assert job_persistence.persist(ordinary)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    monkeypatch.setattr(jm, "_durable_recovery_job_id", owner.id)
    jm.restore_jobs({})

    restored = jm.registry.get(owner.id)
    assert restored is not None
    assert restored.status is jm.JobStatus.FAILED
    assert len(jm.registry.finished()) == jm.JobRegistry.MAX_FINISHED + 1


def test_durable_completion_ack_survives_ordinary_job_saves(monkeypatch):
    from qobuz_librarian.completion import RecoveryOwner
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    job = jm.Job(id="ack-bound-job", album_id="123")
    assert job_persistence.persist(job)
    owner = RecoveryOwner("operation", "item")
    acknowledge = lambda completion_hash: (
        job_persistence.acknowledge_durable_completion(
            job.id,
            owner,
            album_id=job.album_id,
            completion_hash=completion_hash,
        )
    )

    assert acknowledge("a" * 64)
    job.status = jm.JobStatus.FAILED
    assert job_persistence.persist(job)
    assert acknowledge("a" * 64)
    assert not acknowledge("b" * 64)
    assert job_persistence.durable_completion_acknowledged(
        job.id,
        job_created_at=job.created_at,
        album_id=job.album_id,
    ) is True
    assert job_persistence.durable_completion_acknowledged(
        job.id,
        job_created_at=job.created_at + 1,
        album_id=job.album_id,
    ) is False
    assert job_persistence.durable_completion_acknowledged(
        job.id,
        job_created_at=job.created_at,
        album_id="124",
    ) is False


def test_persistence_never_restores_a_recovery_as_reviewable(monkeypatch):
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Repair scan", kind="scan")
    saved.execute_kind = "repair"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.recoveries = [_repair_recovery_record("/backups/repair-originals")]
    saved.add_candidate("repair", "Album", "Artist", payload={})
    job_persistence.persist(saved)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({"repair": lambda _job, _args: pytest.fail(
        "an unresolved recovery must not be rebound for approval")})

    restored = jm.registry.get(saved.id)
    assert restored.status == jm.JobStatus.FAILED
    assert restored.recoveries == saved.recoveries
    assert "restore" in restored.error.lower()
    assert job_persistence.load_one(saved.id)["status"] == "failed"


def test_rehydrated_review_never_mints_colliding_cids(monkeypatch):
    """A job rebuilt with pre-existing candidates (restart, tab split) must
    advance its cid counter past them — a fresh c0/c1 colliding with inherited
    rows made a cid-keyed dismiss delete unrelated, even ticked, candidates."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Library scan")
    saved.kind = "scan"
    saved.execute_kind = "library"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.candidates = [
        {"cid": "c57", "seq": 57, "kind": "album", "title": "A", "artist": "X",
         "detail": "", "payload": {}, "selected": True},
        # A legacy row persisted before seq existed — recovered from the cid.
        {"cid": "c656", "kind": "album", "title": "B", "artist": "Y",
         "detail": "", "payload": {}, "selected": False},
    ]
    job_persistence.persist(saved)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({"library": lambda job, args: (lambda j, chosen: None)})

    restored = jm.registry.get(saved.id)
    restored.add_candidate("album", "C", "Z")
    restored.add_candidate("album", "D", "W")
    cids = [c["cid"] for c in restored.candidates]
    assert len(set(cids)) == len(cids)
    assert restored.candidates[-1]["seq"] > 656


def test_tab_split_review_never_mints_colliding_cids():
    from qobuz_librarian.web import app as webapp

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Missing", "X", payload={})
    job.add_candidate("gap", "Gappy", "Y", payload={"gap_fill": True})
    other = None
    try:
        with job._lock:
            other = webapp._build_unapproved_review(job, "missing")
        assert other is not None
        other.add_candidate("gap", "New find", "Z", payload={"gap_fill": True})
        cids = [c["cid"] for c in other.candidates]
        assert len(set(cids)) == len(cids)
    finally:
        _remove_job(job)
        if other is not None:
            _remove_job(other)


def test_library_download_parks_unselected_and_keeps_only_picks():
    """A partial approval downloads only the picks and preserves the rest."""
    from qobuz_librarian.web import app as webapp

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Picked", "X", payload={}, selected=True)
    job.add_candidate("album", "Unpicked", "X", payload={}, selected=False)
    job.add_candidate("gap", "OtherTab", "Y", payload={"gap_fill": True})
    other = None
    try:
        # Missing tab active: only the ticked "Picked" downloads; the unticked
        # missing album AND the whole Gap Fill tab stay parked.
        with job._lock:
            other = webapp._build_unapproved_review(job, "missing")
        assert other is not None
        kept = {c["title"] for c in job.candidates}
        parked = {c["title"] for c in other.candidates}
        assert kept == {"Picked"}
        assert parked == {"Unpicked", "OtherTab"}
        assert other.status == jm.JobStatus.AWAITING_REVIEW
        assert other.execute_kind == "library"
    finally:
        _remove_job(job)
        if other is not None:
            _remove_job(other)


def test_library_review_rebuilds_from_saved_state_when_no_live_job():
    """F1: with the baseline complete but no live library job (swept cancel,
    discarded scan job, corrupt restart row), the Missing Albums / Gap Fill
    review must rebuild from saved scan state — never 'Baseline ready' + no
    tabs. Retiring the review (discard / worked-through) blocks the rebuild."""
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import app as webapp

    library_scan_state.save_kind("missing", artists={
        "Agalloch": {"fingerprint": "fp", "artist_id": "a1", "catalog_ids": [],
                     "candidates": [
            {"kind": "album", "title": "The Mantle", "artist": "Agalloch",
             "detail": "2002 · fully missing", "payload": {"album_id": "m1"}},
            {"kind": "album", "title": "Ashes", "artist": "Agalloch",
             "detail": "gap-fill: 2 missing",
             "payload": {"album_id": "m2", "gap_fill": 2}},
        ]},
    }, complete=True)
    job = None
    try:
        job = webapp._review_job_from_library_state()
        assert job is not None
        assert job.execute_kind == "library"
        assert job.status == jm.JobStatus.AWAITING_REVIEW
        assert {c["title"] for c in job.candidates} == {"The Mantle", "Ashes"}
        assert all(not c["selected"] for c in job.candidates)
        # Retire it (as a discard / empty would) → no rebuild from stale state.
        _remove_job(job)
        job = None
        library_scan_state.mark_review_retired(now=time.time() + 60)
        assert webapp._review_job_from_library_state() is None
    finally:
        library_scan_state.mark_review_retired(now=0)
        library_scan_state.save_kind("missing", artists={}, complete=False)
        if job is not None:
            _remove_job(job)


def test_partial_scan_does_not_resurrect_a_retired_library_review():
    """A partial scan does not revive a discarded Library review."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.web import app as webapp

    original = lss.load()
    try:
        # missing scanned at t0, review discarded at t1 > t0, then a later
        # gap-fill scan bumps the GLOBAL stamp to t2 > t1 (missing stays t0).
        lss._write_state({
            "version": lss.STATE_VERSION,
            "updated_at": 3000.0,            # bumped by the gap-fill save
            "review_retired_at": 2000.0,     # discard, after the missing scan
            "kinds": {
                "missing": {
                    "updated_at": 1000.0,    # missing kind's own stamp
                    "complete": True,
                    "hidden_signature": "", "quality_signature": "",
                    "artists": {"Agalloch": {
                        "fingerprint": "fp", "artist_id": "a1",
                        "catalog_ids": [], "candidates": [
                            {"kind": "album", "title": "The Mantle",
                             "artist": "Agalloch", "detail": "2002",
                             "payload": {"album_id": "m1"}}]}},
                },
                "gaps": {"updated_at": 3000.0, "complete": True,
                         "hidden_signature": "", "quality_signature": "",
                         "artists": {}},
            },
        })
        # Candidates are present, so a rebuild WOULD produce a job — proving the
        # None is the retirement block holding, not an empty candidate list.
        assert webapp._review_job_from_library_state() is None
    finally:
        lss._write_state(original)


def test_bring_back_lifts_a_retired_library_review():
    """Bring all back rebuilds a retired review from its saved state."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.web import app as webapp

    original = lss.load()
    try:
        lss.save_kind("missing", artists={
            "Agalloch": {"fingerprint": "fp", "artist_id": "a1", "catalog_ids": [],
                         "candidates": [
                {"kind": "album", "title": "The Mantle", "artist": "Agalloch",
                 "detail": "2002", "payload": {"album_id": "m1"}}]},
        }, complete=True)
        lss.mark_review_retired(now=time.time() + 60, reason="discarded")
        assert webapp._review_job_from_library_state() is None

        assert lss.clear_review_retired() is True
        job = webapp._review_job_from_library_state()
        assert job is not None
        assert {c["title"] for c in job.candidates} == {"The Mantle"}
        _remove_job(job)
        # Idempotent — nothing left to lift once it's cleared.
        assert lss.clear_review_retired() is False
    finally:
        lss._write_state(original)


def test_cancel_folds_unrun_picks_back_into_the_review():
    """Cancellation returns unstarted picks to the living review."""
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    try:
        unrun = [
            {"cid": "c9", "seq": 9, "kind": "album", "title": "Unrun One",
             "artist": "Abigail", "detail": "", "payload": {"album_id": "r1"},
             "selected": True},
            {"cid": "c10", "seq": 10, "kind": "album", "title": "Unrun Two",
             "artist": "Abigail", "detail": "", "payload": {"album_id": "r2"},
             "selected": True},
        ]
        assert flows.refold_into_living_review(unrun) == 2
        by_title = {c["title"]: c for c in parked.candidates}
        assert {"Unrun One", "Unrun Two"} <= set(by_title)
        # They rejoin ticked; the existing leftover is untouched.
        assert by_title["Unrun One"]["selected"] is True
        assert by_title["Unrun Two"]["selected"] is True
        assert by_title["Left Unticked"]["selected"] is False
    finally:
        _remove_job(parked)


def test_cancel_mid_download_folds_the_in_flight_pick_too(monkeypatch):
    """Cancellation preserves both the in-flight and unstarted picks."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running.add_candidate(kind="album", title="In Flight", artist="Abigail",
                          payload={"album_id": "r1"}, selected=True)
    running.add_candidate(kind="album", title="Never Started", artist="Abigail",
                          payload={"album_id": "r2"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)

    def fake_process(_full, *_a, **_k):
        # The first (and only reached) album is mid-download when the cancel lands.
        running.cancel_requested = True
        return {"result": "cancelled"}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    try:
        flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        # Both the in-flight album AND the never-started one rejoin, ticked.
        assert by_title.get("In Flight", {}).get("selected") is True
        assert by_title.get("Never Started", {}).get("selected") is True
    finally:
        _remove_job(parked)
        _remove_job(running)


def test_whole_review_download_retires_and_reparks_failures(monkeypatch, tmp_path):
    """A whole review retires successes and re-parks failures for retry."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    original = lss.load()
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = True   # set by _split_and_approve at approve
    running.add_candidate(kind="album", title="Downloaded OK", artist="Agalloch",
                          payload={"album_id": "ok1"}, selected=True)
    running.add_candidate(kind="album", title="Failed One", artist="Agalloch",
                          payload={"album_id": "fail1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)

    def fake_process(full, *_a, **_k):
        if full["id"] == "fail1":
            return {"result": "error", "imported": False, "n_ok": 0}
        return {"imported": True, "n_ok": 1, "n_fail": 0, "result": "downloaded",
                "dir": str(tmp_path)}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    parked = None
    try:
        flows.execute_albums(running, chosen, "tok")
        # The worked-through review is retired → the rebuild won't resurrect it.
        assert lss.load().get("review_retired_reason") == "worked_through"
        # The failure is re-parked, ticked; the successful download is NOT.
        reviews = [j for j in jm.registry.awaiting_review()
                   if getattr(j, "execute_kind", "") == "library"
                   and any((c.get("payload") or {}).get("album_id") == "fail1"
                           for c in j.candidates)]
        assert len(reviews) == 1
        parked = reviews[0]
        assert {c["title"]: c["selected"] for c in parked.candidates} == {
            "Failed One": True}
    finally:
        lss._write_state(original)
        _remove_job(running)
        if parked is not None:
            _remove_job(parked)


def test_reparked_failures_resolve_the_token_at_approve_time(monkeypatch, tmp_path):
    """The retry review parked for failed downloads must look the Qobuz token
    up when it is APPROVED, not reuse the value from the run that failed —
    otherwise a token replaced in Settings never reaches it and every retry
    fails with the same 'update it in Settings' error until a restart."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    original = lss.load()
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = True
    running.add_candidate(kind="album", title="Failed One", artist="Agalloch",
                          payload={"album_id": "fail1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)
    monkeypatch.setattr(process_mod, "process_album",
                        lambda *a, **k: {"result": "error", "imported": False,
                                         "n_ok": 0})
    parked = None
    try:
        flows.execute_albums(running, chosen, "stale-tok")
        parked = next(j for j in jm.registry.awaiting_review()
                      if getattr(j, "execute_kind", "") == "library"
                      and any((c.get("payload") or {}).get("album_id") == "fail1"
                              for c in j.candidates))
        # Token replaced in Settings after the failed run; the retry must use it.
        monkeypatch.setattr(flows, "load_qobuz_token",
                            lambda: ("uid", "fresh-tok"))
        seen = {}
        monkeypatch.setattr(flows, "execute_albums",
                            lambda j, ch, token: seen.setdefault("token", token))
        parked._execute_fn(parked, list(parked.candidates))
        assert seen["token"] == "fresh-tok"
    finally:
        lss._write_state(original)
        _remove_job(running)
        if parked is not None:
            _remove_job(parked)


def test_partial_run_failure_folds_back_into_the_living_review(monkeypatch, tmp_path):
    """On a partial approve (only some picks ticked), a living split-off
    review still holds the unticked picks. An album that FAILS on that run must
    fold back into it, ticked, to retry — matching the whole-review re-park —
    instead of surviving only as the job's error line until a manual refresh."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = False   # partial approve — the remnant lives
    running.add_candidate(kind="album", title="Downloaded OK", artist="Agalloch",
                          payload={"album_id": "ok1"}, selected=True)
    running.add_candidate(kind="album", title="Failed One", artist="Agalloch",
                          payload={"album_id": "fail1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)

    def fake_process(full, *_a, **_k):
        if full["id"] == "fail1":
            return {"result": "error", "imported": False, "n_ok": 0}
        return {"imported": True, "n_ok": 1, "n_fail": 0, "result": "downloaded",
                "dir": str(tmp_path)}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    try:
        flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        # The failure rejoined the living review, ticked; the leftover is untouched
        # and the successful download was NOT parked.
        assert by_title.get("Failed One", {}).get("selected") is True
        assert by_title["Left Unticked"]["selected"] is False
        assert "Downloaded OK" not in by_title
    finally:
        _remove_job(running)
        _remove_job(parked)


def test_new_release_run_recoveries_never_touch_the_library_review(monkeypatch):
    """A failed or cancelled NEW-RELEASE download run must not fold its albums
    into the parked Library review — new-release results never enter the
    Library tabs. Guards both fold-back call sites in execute_albums, which
    also runs new-release batches."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)
    try:
        # A run where an album fails outright.
        failing = _inject_job(jm.JobStatus.RUNNING, "New-release check")
        failing.execute_kind = "new_releases"
        failing.add_candidate(kind="album", title="NR Failed", artist="Agalloch",
                              payload={"album_id": "nr1"}, selected=True)
        monkeypatch.setattr(process_mod, "process_album",
                            lambda full, *_a, **_k: {"result": "error",
                                                     "imported": False, "n_ok": 0})
        try:
            flows.execute_albums(failing, list(failing.candidates), "tok")
        finally:
            _remove_job(failing)

        # A run cancelled mid-batch with a pick it never reached.
        cancelled = _inject_job(jm.JobStatus.RUNNING, "New-release check")
        cancelled.execute_kind = "new_releases"
        cancelled.add_candidate(kind="album", title="NR In Flight", artist="Agalloch",
                                payload={"album_id": "nr2"}, selected=True)
        cancelled.add_candidate(kind="album", title="NR Unreached", artist="Agalloch",
                                payload={"album_id": "nr3"}, selected=True)

        def cancelling(full, *_a, **_k):
            cancelled.cancel_requested = True
            return {"result": "cancelled", "imported": False, "n_ok": 0}

        monkeypatch.setattr(process_mod, "process_album", cancelling)
        try:
            flows.execute_albums(cancelled, list(cancelled.candidates), "tok")
        finally:
            _remove_job(cancelled)

        assert [c["title"] for c in parked.candidates] == ["Left Unticked"]
    finally:
        _remove_job(parked)


def test_auth_death_mid_batch_folds_unfinished_picks_back(monkeypatch, tmp_path):
    """A token death / Qobuz outage AFTER the first import fails the job
    (approve's no-harm re-park only covers the nothing-landed case), so on a
    partial approve the picks the run never finished must fold back into the
    living split-off review, ticked. Before anything lands the fold must NOT
    fire — approve() restores the whole review instead, and folding here too
    would offer the same picks twice."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = False
    for title, aid in (("Landed", "ok1"), ("Died Mid-Rip", "die1"),
                       ("Never Started", "ns1")):
        running.add_candidate(kind="album", title=title, artist="Agalloch",
                              payload={"album_id": aid}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)

    def fake_process(full, *_a, **_k):
        if full["id"] == "die1":
            raise AuthLost("token expired")
        return {"imported": True, "n_ok": 1, "n_fail": 0, "result": "downloaded",
                "dir": str(tmp_path)}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    try:
        with pytest.raises(AuthLost):
            flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        # The album that died and the one never reached rejoin ticked; what
        # landed stays out; the untouched leftover keeps its state.
        assert by_title.get("Died Mid-Rip", {}).get("selected") is True
        assert by_title.get("Never Started", {}).get("selected") is True
        assert by_title["Left Unticked"]["selected"] is False
        assert "Landed" not in by_title
    finally:
        _remove_job(running)
        _remove_job(parked)

    # Nothing landed: the no-harm re-park recovers the whole job, so the fold
    # must stay out of it.
    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = False
    running.add_candidate(kind="album", title="Died First", artist="Agalloch",
                          payload={"album_id": "die1"}, selected=True)
    try:
        with pytest.raises(AuthLost):
            flows.execute_albums(running, list(running.candidates), "tok")
        assert [c["title"] for c in parked.candidates] == ["Left Unticked"]
    finally:
        _remove_job(running)
        _remove_job(parked)


def test_bulk_cancel_pending_never_touches_parked_reviews():
    """Bulk cancellation leaves parked reviews untouched."""
    from qobuz_librarian.web import app as webapp

    review = jm.Job(title="Library scan")
    review.execute_kind = "library"
    review.status = jm.JobStatus.AWAITING_REVIEW
    review.add_candidate("album", "Keep me", "X", payload={})
    queued = jm.Job(title="Album", artist="A", album_id="q1")
    queued.status = jm.JobStatus.PENDING
    jm.registry.add(review)
    jm.registry.add(queued)
    try:
        asyncio.run(webapp.queue_cancel_pending())
        assert review.status == jm.JobStatus.AWAITING_REVIEW
        assert len(review.candidates) == 1
        assert queued.cancel_requested is True
    finally:
        _remove_job(review)
        _remove_job(queued)


def test_restart_interrupt_message_matches_the_retry_affordance(monkeypatch):
    # A job rebadged FAILED by a restart must not tell the user to "submit this
    # job again" unless it actually offers a Retry button. Album downloads do; a
    # lyrics backfill / migration / library execute does not — point those at the
    # page they re-run from instead of promising a control that isn't there.
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    album = jm.Job(title="Album", artist="Artist", album_id="abc")
    album.status = jm.JobStatus.RUNNING
    job_persistence.persist(album)

    lyrics = jm.Job(title="Lyrics backfill")
    lyrics.execute_kind = "lyrics"
    lyrics.status = jm.JobStatus.RUNNING
    job_persistence.persist(lyrics)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})

    a = jm.registry.get(album.id)
    ly = jm.registry.get(lyrics.id)
    assert a.status == jm.JobStatus.FAILED and ly.status == jm.JobStatus.FAILED
    assert "Retry" in a.error
    assert "Submit this" not in ly.error
    assert "Lyrics" in ly.error


def test_interrupted_scan_summary_matches_the_real_resume_path(monkeypatch):
    """Only a pre-baseline library scan auto-resumes; post-baseline the
    summary must point at the manual resume notice instead."""
    from qobuz_librarian.web import job_persistence

    for complete, expect in ((False, "next time you open the app"),
                             (True, "notice on the Search page")):
        job_persistence._reset_for_tests()
        monkeypatch.setattr(job_persistence, "_disabled", False)
        job_persistence.init()
        monkeypatch.setattr(
            "qobuz_librarian.library.new_releases.is_baseline_complete",
            lambda complete=complete: complete)
        scan = jm.Job(title="Library scan")
        scan.execute_kind = "library"
        scan.status = jm.JobStatus.SCANNING
        job_persistence.persist(scan)
        monkeypatch.setattr(jm, "registry", jm.JobRegistry())
        jm.restore_jobs({})
        restored = jm.registry.get(scan.id)
        assert expect in restored.summary


def test_repair_recovery_survives_cancel_restart_and_history(
        client, monkeypatch, tmp_path):
    from argparse import Namespace

    from qobuz_librarian.library.backup import BackupResult
    from qobuz_librarian.modes import repair as repair_mod
    from qobuz_librarian.web import flows, job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    backup = BackupResult(
        tmp_path / "backups" / "repair-originals",
        complete=True,
        receipt={"kind": "gap-fill", "exact": "receipt"},
        requested=1,
        backed_up=1,
    )
    recovery = repair_mod.RepairRecovery(
        backup=backup,
        album_dir=tmp_path / "Music" / "Artist" / "Album",
        stage="refill",
        reason="Repair stopped while downloading the replacement.",
    )

    job = jm.Job(title="Repair scan", kind="scan")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.RUNNING
    jm.registry.add(job)

    def stop_with_recovery(*_args, recovery_checkpoint=None, **_kwargs):
        assert recovery_checkpoint is not None
        assert recovery_checkpoint(recovery) is True
        job.cancel_requested = True
        raise repair_mod.RepairRecoveryRequired(
            recovery, OSError("replacement download stopped"))

    monkeypatch.setattr(repair_mod, "repair_album_dir", stop_with_recovery)
    monkeypatch.setattr(flows, "build_args", lambda: Namespace())
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *_a, **_k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda _seconds: None)
    chosen = [{
        "kind": "repair",
        "title": "Album",
        "payload": {
            "album_dir": str(recovery.album_dir),
            "artist_name": "Artist",
            "verified_truncated": [{}],
        },
    }]

    jm._run_task(job, lambda current: flows.execute_repairs(
        current, chosen, "token"))

    assert job.status == jm.JobStatus.CANCELED
    assert "1 kept for recovery" in job.summary
    row = job_persistence.load_one(job.id)
    assert row["recoveries"][0]["receipt"] == backup.receipt

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})
    restored = jm.registry.get(job.id)
    assert restored.recoveries == row["recoveries"]

    response = client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    assert "1 of 1 original file" in response.text
    assert "Settings → Diagnostics" in response.text
    assert "/settings#diagnostics-list" in response.text
    assert str(backup.path) not in response.text
    assert recovery.reason not in response.text
    assert '"exact": "receipt"' not in response.text


def test_unresolved_recovery_survives_caps_pruning_and_clear_history(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    registry = jm.JobRegistry()
    registry.MAX_FINISHED = 1
    monkeypatch.setattr(jm, "registry", registry)

    recovery = jm.Job(title="Originals still held")
    recovery.execute_kind = "repair"
    recovery.status = jm.JobStatus.FAILED
    recovery.finished_at = 1.0
    recovery.recoveries = [
        _repair_recovery_record("/backups/protected", {"token": "exact"})
    ]
    registry.add(recovery)

    for index in range(2):
        ordinary = jm.Job(title=f"Ordinary {index}")
        ordinary.execute_kind = "repair"
        ordinary.status = jm.JobStatus.DONE
        ordinary.finished_at = 10.0 + index
        registry.add(ordinary)
    assert registry.get(recovery.id) is recovery

    for index in range(2, 41):
        ordinary = jm.Job(title=f"Ordinary {index}")
        ordinary.execute_kind = "repair"
        ordinary.status = jm.JobStatus.DONE
        ordinary.finished_at = 10.0 + index
        assert job_persistence.persist(ordinary)
    response = client.get("/queue/history")
    assert response.status_code == 200
    assert "Originals still held" in response.text
    assert "Settings → Diagnostics" in response.text
    assert "/settings#diagnostics-list" in response.text
    assert "/backups/protected" not in response.text

    job_persistence.prune_finished(1)
    assert job_persistence.load_one(recovery.id) is not None

    response = client.post("/queue/clear", follow_redirects=False)
    assert response.status_code == 303
    assert registry.get(recovery.id) is recovery
    assert job_persistence.load_one(recovery.id)["recoveries"] == recovery.recoveries


@pytest.mark.parametrize("raw_recoveries", ("{not-json", "{}"))
def test_unreadable_recovery_record_stays_visible_and_clear_safe(
        client, monkeypatch, raw_recoveries):
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(
        title="Repair recovery with unreadable details", album_id="album-1")
    saved.execute_kind = "repair"
    saved.status = jm.JobStatus.FAILED
    saved.finished_at = time.time()
    saved.recoveries = [_repair_recovery_record("/backups/protected")]
    assert job_persistence.persist(saved)
    with job_persistence._lock:
        conn = job_persistence._get_conn()
        conn.execute(
            "UPDATE jobs SET recoveries=? WHERE id=?",
            (raw_recoveries, saved.id),
        )
        conn.commit()
    assert job_persistence.load_all() == []

    response = client.get("/queue/history")
    assert response.status_code == 200
    assert "Repair recovery with unreadable details" in response.text
    assert "Saved recovery details could not be read" in response.text
    assert "/settings#diagnostics-list" in response.text
    assert f"/jobs/{saved.id}/retry" not in response.text

    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert any(job["id"] == saved.id for job in response.json()["jobs"])

    response = client.post("/queue/clear", follow_redirects=False)
    assert response.status_code == 303
    with job_persistence._lock:
        row = job_persistence._get_conn().execute(
            "SELECT recoveries FROM jobs WHERE id=?", (saved.id,),
        ).fetchone()
    assert row == (raw_recoveries,)


def test_clear_history_refuses_unsettled_durable_download(
        client, monkeypatch):
    from qobuz_librarian.completion import RecoveryOwner
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    owner = jm.Job(id="exact-resume", title="Interrupted", album_id="album-1")
    owner.status = jm.JobStatus.FAILED
    owner.finished_at = time.time()
    jm.registry.add(owner)
    assert job_persistence.acknowledge_durable_completion(
        owner.id,
        RecoveryOwner("operation", "item"),
        album_id=owner.album_id,
        completion_hash="a" * 64,
    )
    monkeypatch.setattr(
        webapp,
        "_STARTUP_RECOVERY_RESULT",
        StartupRecoveryResult(StartupRecoveryStatus.ATTENTION_REQUIRED),
    )
    monkeypatch.setattr(jm, "_durable_recovery_job_id", owner.id)

    response = client.post("/queue/clear", follow_redirects=False)

    assert response.status_code == 503
    assert jm.registry.get(owner.id) is owner
    assert job_persistence.load_one(owner.id) is not None
    assert job_persistence.durable_completion_acknowledged(
        owner.id,
        job_created_at=owner.created_at,
        album_id=owner.album_id,
    ) is True


def test_clear_history_serializes_with_durable_resume_publication(
        client, monkeypatch):
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    owner = jm.Job(id="resume-race", title="In progress", album_id="album-1")
    owner.status = jm.JobStatus.RUNNING
    jm.registry.add(owner)
    monkeypatch.setattr(
        webapp,
        "_STARTUP_RECOVERY_RESULT",
        StartupRecoveryResult(StartupRecoveryStatus.CLEAR),
    )
    monkeypatch.setattr(webapp, "_STARTUP_RECOVERY_UNKNOWN", False)
    jm.set_durable_recovery_job_id(None)

    clear_entered = threading.Event()
    publication_attempted = threading.Event()
    real_clear_finished = jm.registry.clear_finished

    def pause_clear_finished():
        clear_entered.set()
        assert publication_attempted.wait(2)
        real_clear_finished()

    monkeypatch.setattr(jm.registry, "clear_finished", pause_clear_finished)

    def publish_resume():
        assert clear_entered.wait(2)
        publication_attempted.set()
        with webapp._STARTUP_RECOVERY_LOCK:
            webapp._STARTUP_RECOVERY_RESULT = StartupRecoveryResult(
                StartupRecoveryStatus.RESUME_REQUIRED)
            jm.set_durable_recovery_job_id(owner.id)
            with owner._lock:
                owner.status = jm.JobStatus.FAILED
                owner.finished_at = time.time()
            assert job_persistence.persist(owner)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        publication = executor.submit(publish_resume)
        response = client.post("/queue/clear", follow_redirects=False)
        publication.result(timeout=3)

    assert response.status_code == 303
    assert jm.durable_recovery_job_id() == owner.id
    assert jm.registry.get(owner.id) is owner
    assert job_persistence.load_one(owner.id) is not None


def test_recovery_resolution_holds_live_state_through_database_commit(
        monkeypatch):
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    registry = jm.JobRegistry()
    monkeypatch.setattr(jm, "registry", registry)

    job = jm.Job(title="Repair recovery")
    job.status = jm.JobStatus.FAILED
    job.attention = "recovery"
    job.recoveries = [
        _repair_recovery_record("/backups/protected", {"token": "exact"})
    ]
    registry.add(job)
    plan = jm.prepare_recovery_resolution(
        "/backups/protected", {"token": "exact"})
    real_resolve = job_persistence.resolve_recovery_resolution
    lock_was_held = []

    def checked_resolve(candidate):
        acquired = job._lock.acquire(blocking=False)
        lock_was_held.append(not acquired)
        if acquired:
            job._lock.release()
        return real_resolve(candidate)

    monkeypatch.setattr(
        job_persistence, "resolve_recovery_resolution", checked_resolve)

    assert jm.resolve_recovery_resolution(plan) is True
    assert lock_was_held == [True]
    assert job.recoveries == []
    assert job_persistence.load_one(job.id)["recoveries"] == []


def test_persist_survives_non_json_candidate_payload(monkeypatch):
    """A stray non-JSON value in a candidate payload (a Path, say) must coerce
    to text at the write boundary, not raise TypeError — that escaped the
    sqlite guard, killed the worker, and lost the whole parked review."""
    from pathlib import Path

    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    job = jm.Job(title="Review")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Album", "Artist",
                      payload={"album_id": "A1", "dir": Path("/music/A/B")})
    job_persistence.persist(job)

    row = job_persistence.load_one(job.id)
    assert row is not None
    assert row["candidates"][0]["payload"]["album_id"] == "A1"


def test_download_partial_album_proceeds_to_gap_fill(client, monkeypatch):
    from pathlib import Path

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "gap1", "title": "Gappy", "artist": {"name": "A"},
             "tracks": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    monkeypatch.setattr(search_mod, "get_album", lambda _i, _t: album)
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda _a: Path("/music/A/Gappy"))
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda _a, **_kw: ([{"id": 1}], None))
    monkeypatch.setattr(cat_mod, "compute_missing",
                        lambda q, e: ([{"id": 2}, {"id": 3}], [{"id": 1}]))
    monkeypatch.setattr(proc_mod, "process_album",
                        lambda *a, **k: {"result": "downloaded",
                                         "imported": True, "n_fail": 0})

    jm.start_worker()
    r = client.post("/download", data={"album_id": "gap1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "already complete" not in r.text.lower()
    new_jobs = [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "gap1"]
    assert len(new_jobs) == 1
    job = new_jobs[0]
    try:
        _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
    finally:
        _remove_job(job)


def test_settings_save_rejects_out_of_enum_quality(tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)

    assert ss.save({"STREAMRIP_QUALITY": "99"})[0] is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk.get("STREAMRIP_QUALITY") != "99"
    # A valid value still persists.
    assert ss.save({"STREAMRIP_QUALITY": "2"})[0] is True
    assert json.loads((tmp_path / "s.json").read_text())["STREAMRIP_QUALITY"] == "2"


def test_settings_omits_cli_only_consolidation_setting(
        client, tmp_path, monkeypatch):
    import json

    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"user_id": "u", "auth_token": "t"})
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "CONSOLIDATE", True)

    r = client.get("/settings")

    assert r.status_code == 200
    assert "Consolidate duplicate folders" not in r.text
    assert "CLI only" not in r.text
    assert "CONSOLIDATE" not in r.text

    data = {"form_complete": "1", "CONSOLIDATE": "1"}
    data.update({key: "" for key in ss.TEXT_KEYS})
    r = client.post("/settings/behavior", data=data, follow_redirects=False)

    assert r.status_code == 303
    assert "CONSOLIDATE" not in json.loads((tmp_path / "s.json").read_text())


def test_settings_renders_both_forms_and_mode_switch(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"user_id": "u", "auth_token": "t"})
    r = client.get("/settings")

    assert r.status_code == 200
    assert 'data-toggle-password="auth_token"' in r.text
    assert ">Save &amp; connect</button>" in r.text
    assert "Switch to terminal mode" in r.text
    assert ">Save behaviour</button>" in r.text
    assert "CLI command" in r.text
    assert "CLI entrypoint" not in r.text
    assert "Paths currently in use" in r.text
    assert "QL_MUSIC_DIR" not in r.text
    assert "QL_STAGING_DIR" not in r.text
    assert "MUSIC_ROOT" not in r.text
    assert "STAGING_DIR" not in r.text


def test_settings_shows_host_location_for_interrupted_backup_cleanup(
        client, tmp_path, monkeypatch):
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.web import app as app_mod

    wrapper = tmp_path / f".ql-dispose-backup-{'a' * 64}"
    held = wrapper / "held"
    held.mkdir(parents=True)
    origin = tmp_path / "music" / "Artist" / "Album"
    host_location = "/home/me/backups/interrupted/held"
    monkeypatch.setattr(
        backup_mod,
        "find_only_copy_backups",
        lambda: [(wrapper, origin)],
    )
    monkeypatch.setattr(
        app_mod,
        "_resolve_host_path",
        lambda _path: (host_location, True),
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Interrupted backup cleanup" in response.text
    assert host_location in response.text
    assert str(held) not in response.text
    assert f'name="backup" value="{wrapper.name}"' not in response.text


def test_beets_diagnostic_distinguishes_verification_failure_from_missing_launcher(
        monkeypatch):
    from qobuz_librarian.integrations import beets as beets_mod
    from qobuz_librarian.web import app as app_mod

    class Runtime:
        python = "/verified/python"

    monkeypatch.setattr(
        beets_mod,
        "_beets_python_from_launcher",
        lambda: Runtime.python,
    )
    monkeypatch.setattr(beets_mod, "_checked_beets_runtime", lambda _path: Runtime())
    monkeypatch.setattr(beets_mod, "_configured_beets_plugins", lambda _runtime: None)

    check = next(
        item for item in app_mod._diagnostics()
        if item["label"] == "Beets 2.12.0 runtime"
    )

    assert check["ok"] is False
    assert "Could not verify a Beets 2.12.0 runtime" in check["detail"]
    assert "launcher was found" not in check["detail"]


def test_settings_empty_token_warning_names_localuser_token(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    r = client.post("/settings", data={"user_id": "user@example.com",
                                       "auth_token": ""})

    assert r.status_code == 200
    assert "Token cannot be empty" in r.text
    assert "localuser" in r.text
    assert "<code>token</code>" in r.text
    assert "user_auth_token" not in r.text


# ── CLI/web mode hand-off ───────────────────────────────────────────────────────


def test_web_pauses_new_writes_if_its_run_lock_is_displaced(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    class LostAuthority:
        @staticmethod
        def intact():
            return False

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", LostAuthority())
    monkeypatch.setattr(app_mod, "_CLI_MODE", False)
    monkeypatch.setattr(app_mod, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(app_mod, "_LOCK_UNENFORCEABLE", False)
    monkeypatch.setattr(app_mod, "_UNWRITABLE_VOLUMES", [])
    monkeypatch.setattr(app_mod, "_SHUTTING_DOWN", False)

    assert app_mod._web_writes_paused() is True


def test_shutdown_keeps_run_lock_until_workers_and_direct_writes_settle(
        monkeypatch):
    import threading

    import qobuz_librarian.web.app as app_mod

    worker_joined = threading.Event()
    release_worker = threading.Event()

    class Worker:
        def is_alive(self):
            return True

        def join(self):
            worker_joined.set()
            release_worker.wait(timeout=5)

    class Handle:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    handle = Handle()
    stop_event = threading.Event()
    monkeypatch.setattr(jm, "_download_worker_thread", Worker())
    monkeypatch.setattr(jm, "_scan_worker_thread", None)
    monkeypatch.setattr(jm, "_stop_event", stop_event)
    monkeypatch.setattr(jm, "_library_operations_accepting", True)
    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", handle)

    operation = jm.begin_library_operation("Restore")
    assert operation is not None
    shutdown = threading.Thread(target=app_mod._shutdown_web_mutations)
    shutdown.start()
    assert worker_joined.wait(timeout=2)
    assert handle.closed is False
    assert jm.begin_library_operation("Late write") is None

    release_worker.set()
    shutdown.join(timeout=0.1)
    assert shutdown.is_alive()
    assert handle.closed is False

    jm.end_library_operation(operation)
    shutdown.join(timeout=2)
    assert not shutdown.is_alive()
    assert handle.closed is True
    assert app_mod._RUN_LOCK_HANDLE is None


def test_parked_review_does_not_block_cli_handoff(client):
    """The staging race the handoff guards against needs a running worker; a
    review waiting on the user has none, and it can wait for weeks — refusing
    on it would make terminal mode unreachable."""
    import qobuz_librarian.web.app as app_mod
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    review.execute_kind = "downsample"

    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?mode=cli"
    assert app_mod._CLI_MODE is True
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303
    assert app_mod._CLI_MODE is False


def test_resuming_web_mode_restores_saved_jobs_before_unpausing(
        client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import run_lock

    class Lease:
        @staticmethod
        def intact():
            return True

    lease = Lease()
    restored_under = []

    def restore_jobs(factories, *, durable_recovery_clear):
        assert factories is app_mod._RESUME_EXECUTE
        assert durable_recovery_clear is True
        restored_under.append((app_mod._CLI_MODE, app_mod._RUN_LOCK_HANDLE))

    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(jm, "restore_jobs", restore_jobs)
    monkeypatch.setattr(app_mod, "_CLI_MODE", True)
    monkeypatch.setattr(app_mod, "_JOBS_RESTORED", False)

    response = client.post(
        "/settings/mode",
        data={"target": "web"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?mode=web"
    assert restored_under == [(True, lease)]
    assert app_mod._CLI_MODE is False


def test_mode_handoff_to_cli_pauses_web_downloads(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    # No active job (the registry is a shared singleton across tests).
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: [])
    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?mode=cli"
    assert app_mod._CLI_MODE is True
    # The banner shows everywhere, and download/scan endpoints are paused.
    assert "Terminal (CLI) mode" in client.get("/").text
    blocked = client.post("/download", data={"album_id": "123"},
                          follow_redirects=False)
    assert blocked.status_code == 503 and "Terminal (CLI) mode" in blocked.text
    # Resume restores web mode.
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/settings?mode=web"
    assert app_mod._CLI_MODE is False


@pytest.mark.parametrize("operation", ["undo", "restore"])
def test_cli_handoff_refuses_while_a_direct_library_mutation_runs(
        operation, client, monkeypatch, tmp_path):
    import os
    import threading

    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.backup as backup_mod
    import qobuz_librarian.library.scanner as scanner_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    entered = threading.Event()
    release = threading.Event()
    responses, errors = [], []

    class Handle:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    handle = Handle()
    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", handle)
    monkeypatch.setattr(app_mod, "_CLI_MODE", False)
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running", lambda: [])

    if operation == "undo":
        album = tmp_path / "music" / "Artist" / "Album"
        album.mkdir(parents=True)
        track = album / "01 - Track.flac"
        track.write_bytes(b"audio")
        job = jm.Job(title="Track", artist="Artist", album_id="album")
        job.status = jm.JobStatus.DONE
        job.single = {
            "dir": str(album), "track_id": "track", "isrc": "ISRC1",
            "track_no": 1, "disc_no": 1, "title": "Track",
            "artist": "Artist", "album": "Album", "marked": False,
            "new_folder": False,
            "owned_path": app_mod._bind_owned_path(album, track),
        }
        jm.registry.add(job)
        monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [{
            "path": str(track), "isrc": "ISRC1", "tracknumber": 1,
            "discnumber": 1,
        }])

        def block_forget(_paths):
            entered.set()
            release.wait(timeout=5)
            return 1

        monkeypatch.setattr(beets_mod, "forget_beets_entries", block_forget)
        path, data = f"/jobs/{job.id}/undo", {}
    else:
        backups = tmp_path / "backups"
        backup = backups / "20260101_000000_Album"
        origin = tmp_path / "music" / "Artist" / "Album"
        backup.mkdir(parents=True)
        monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", backups)
        carried = backup_mod.BackupResult(
            backup,
            complete=True,
            receipt={"kind": "upgrade", "origin": str(origin)},
            requested=1,
            backed_up=1,
        )
        monkeypatch.setattr(
            backup_mod, "load_backup_result", lambda _p: carried)

        def block_restore(_backup, _origin):
            entered.set()
            release.wait(timeout=5)
            return True

        monkeypatch.setattr(backup_mod, "restore_upgrade_backup", block_restore)
        path, data = "/backups/restore", {"backup": backup.name}

    def run_operation():
        try:
            responses.append(client.post(path, data=data, follow_redirects=False))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_operation, daemon=True)
    worker.start()
    assert entered.wait(timeout=3), f"{operation} never reached its mutation"
    try:
        handoff = client.post("/settings/mode", data={"target": "cli"},
                              follow_redirects=False)
        assert handoff.status_code == 303
        assert handle.closed is False
        assert app_mod._CLI_MODE is False
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors
    assert responses and responses[0].status_code in (200, 303)


# ── web/auth.py: optional login ────────────────────────────────────────────────


def _enable_auth(monkeypatch, tmp_path, *, configure=True):
    """Turn auth on for one test against an isolated credential file. Returns
    a client bound to the app. The session-wide conftest default of
    WEB_AUTH=none is restored on teardown by monkeypatch."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setenv("WEB_AUTH", "")
    _run_web_executors_inline(monkeypatch, app_mod)
    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    if configure:
        assert web_auth.set_credentials("admin", "hunter2hunter")
    return _SameThreadASGIClient(app_mod.app)


def test_login_form_has_a_password_toggle(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/login")

    assert r.status_code == 200
    assert r.text.count('class="ql-secret-toggle"') == 1


def test_setup_form_has_two_password_toggles(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path, configure=False) as c:
        r = c.get("/setup")

    assert r.status_code == 200
    assert r.text.count('class="ql-secret-toggle"') == 2


def test_login_rejects_wrong_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "nope",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 401
        assert "qf_session" not in r.cookies
        # Still locked out afterwards.
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_accepts_correct_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # The session cookie now opens a protected route.
        assert c.get("/", follow_redirects=False).status_code == 200


def test_authenticated_pages_are_not_stored_in_the_browser_cache(
        monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("qf_csrf")
        c.post(
            "/login",
            data={"username": "admin", "password": "hunter2hunter",
                  "_csrf_token": tok},
            headers={"X-CSRF-Token": tok},
            follow_redirects=False,
        )

        page = c.get("/settings")
        assert page.headers["cache-control"] == "no-store"

        jobs = c.get("/api/jobs")
        assert jobs.status_code == 200
        assert jobs.headers["cache-control"] == "no-store"

        logout_response = c.post(
            "/logout",
            data={"_csrf_token": tok},
            headers={"X-CSRF-Token": tok},
            follow_redirects=False,
        )
        assert logout_response.status_code == 303
        assert logout_response.headers["cache-control"] == "no-store"

        assert c.get("/sw.js").headers["cache-control"] == "no-cache"
        assert "no-store" not in c.get("/static/app.js").headers.get(
            "cache-control", ""
        )
        assert "no-store" not in c.get("/static/offline.html").headers.get(
            "cache-control", ""
        )


def test_login_returns_to_the_page_that_bounced(monkeypatch, tmp_path):
    # A deep link opened while logged out should survive the login bounce:
    # /queue → /login?next=/queue → sign in → land on /queue, not the dashboard.
    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/queue", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=/queue"
        r = c.get("/login?next=/queue")
        assert 'name="next" value="/queue"' in r.text
        tok = c.cookies.get("qf_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok, "next": "/queue"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/queue"


def test_login_next_cannot_leave_the_app(monkeypatch, tmp_path):
    # The next field is attacker-writable (it rides links and the login form),
    # so anything that could land off-site or loop must fall back to "/".
    from qobuz_librarian.web import auth as web_auth

    for bad in ("//evil.example", "/\\evil.example", "https://evil.example",
                "javascript:alert(1)", "/login", "/setup", "/api/jobs",
                "/a\r\nSet-Cookie:x=1", "queue", ""):
        assert web_auth.safe_next_path(bad) == "", bad
    assert web_auth.safe_next_path("/queue") == "/queue"
    assert web_auth.safe_next_path("/jobs/abc?x=1") == "/jobs/abc?x=1"

    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok, "next": "//evil.example"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_malformed_host_cannot_bypass_auth(monkeypatch, tmp_path):
    # CVE-2026-48710: Starlette rebuilds request.url.path from the client Host
    # header, so a host like "example.com/login?x=" can make the auth middleware
    # read the path as "/login" and wave a protected route through with no
    # session. The gate reads request.scope["path"] (the real routed path),
    # which a forged Host cannot touch — protected routes stay closed.
    with _enable_auth(monkeypatch, tmp_path) as c:
        bad = {"host": "example.com/login?x="}
        # Page route: redirected to login, never served.
        r = c.get("/settings", headers=bad, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login")
        # JSON route: 401, not a 200 leaking state.
        r = c.get("/api/jobs", headers=bad, follow_redirects=False)
        assert r.status_code == 401
        # Write route is unreachable too (never a 200).
        r = c.post("/queue/cancel-pending", headers=bad,
                   follow_redirects=False)
        assert r.status_code != 200


def test_artist_sort_key_files_articles_under_the_real_letter():
    # "The Beatles" must sort under B, not T (owner acceptance criterion);
    # leading the/a/an are ignored, the rest of the name is not.
    from qobuz_librarian.web.app import _artist_sort_key
    names = ["The Beatles", "Bob Dylan", "ABBA", "Adele", "The Who",
             "A Tribe Called Quest", "an Evening"]
    ordered = sorted(names, key=_artist_sort_key)
    assert ordered.index("Adele") < ordered.index("The Beatles") < ordered.index("Bob Dylan")
    assert ordered[-1] == "The Who"            # "who" sorts last
    assert _artist_sort_key("The Beatles") == "beatles"
    assert _artist_sort_key("A Tribe Called Quest") == "tribe called quest"
    assert _artist_sort_key("Adele") == "adele"   # no leading "a " to strip


def test_review_artist_groups_use_library_sort_order():
    from qobuz_librarian.web.app import _review_artist_groups

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.add_candidate(kind="album", title="Revolver", artist="The Beatles",
                      payload={"album_id": "beatles"})
    job.add_candidate(kind="album", title="Highway 61 Revisited",
                      artist="Bob Dylan", payload={"album_id": "dylan"})
    job.add_candidate(kind="album", title="Low", artist="David Bowie",
                      payload={"album_id": "bowie"})

    assert [artist for artist, _items in _review_artist_groups(job)] == [
        "The Beatles",
        "Bob Dylan",
        "David Bowie",
    ]


def test_migrate_post_submits_a_creds_free_job(client, monkeypatch, tmp_path):
    import qobuz_librarian.config as cfg
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(cfg, "MIGRATE_SRC", str(src))
    monkeypatch.setattr(cfg, "MIGRATE_DEST", str(tmp_path / "dest"))
    r = client.post("/migrate", data={"in_place": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    job_id = r.headers["location"].split("/jobs/")[1].split("?")[0]
    job = jm.registry.get(job_id)
    assert job is not None
    assert job.review_verb == "Move"                  # in-place toggle carried through
    _remove_job(job)


def test_migrate_offers_a_preview(client, monkeypatch, tmp_path):
    import qobuz_librarian.config as cfg

    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(cfg, "MIGRATE_SRC", str(src))
    monkeypatch.setattr(cfg, "MIGRATE_DEST", str(tmp_path / "dest"))

    r = client.get("/migrate")

    assert r.status_code == 200
    assert ">Preview migration</button>" in r.text


def test_settings_path_resolver_maps_container_paths_to_host_bind_mounts(
    monkeypatch, tmp_path
):
    from qobuz_librarian.web.app import _resolve_host_path

    fake_mountinfo = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:2 /home/me/music /music rw - ext4 /dev/sda1 rw\n"
        "3 1 0:3 /home/me/stack/config /config rw - ext4 /dev/sda1 rw\n"
    )
    fake = tmp_path / "mountinfo"
    fake.write_text(fake_mountinfo)
    import builtins
    real_open = builtins.open
    def patched_open(path, *a, **kw):
        if path == "/proc/self/mountinfo":
            return real_open(fake, *a, **kw)
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", patched_open)

    assert _resolve_host_path("/music") == ("/home/me/music", True)
    assert _resolve_host_path("/config/beets/musiclibrary.db") == (
        "/home/me/stack/config/beets/musiclibrary.db", True)
    assert _resolve_host_path("/anonymous-volume") == ("/anonymous-volume", False)
    from pathlib import Path
    assert _resolve_host_path(Path("/music")) == ("/home/me/music", True)


def test_session_tokens_are_per_login_and_revocable():
    from qobuz_librarian.web import auth as web_auth
    web_auth.revoke_all_sessions()
    t1 = web_auth.mint_session()
    t2 = web_auth.mint_session()
    assert t1 != t2                              # per-login, not one shared secret
    assert web_auth.verify_session(t1) and web_auth.verify_session(t2)
    web_auth.revoke_session(t1)                  # logout of one browser
    assert not web_auth.verify_session(t1)       # ...that session is dead...
    assert web_auth.verify_session(t2)           # ...the other still works
    web_auth.revoke_all_sessions()               # e.g. on a password change
    assert not web_auth.verify_session(t2)
    assert web_auth.verify_session("") is False


def test_restore_backup_rejects_path_shaped_names(client, tmp_path, monkeypatch):
    # The Restore form posts a bare directory name; anything path-shaped is a
    # probe, not a backup the diagnostics list rendered — it must not resolve
    # outside the backup dir or restore anything.
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    r = client.post("/backups/restore", data={"backup": "../../etc"})
    assert r.status_code == 200
    assert "isn't there anymore" in r.text


def test_restore_backup_moves_the_files_home(client, tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    origin = tmp_path / "music" / "Artist" / "Album (2020)"
    origin.mkdir(parents=True)
    (origin / "01 - Song.flac").write_bytes(b"data")
    carried = backup_mod.backup_album_dir(origin)
    assert carried is not None and carried.complete is True
    job = jm.Job(title="Repair needing recovery")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.FAILED
    job.finished_at = time.time()
    job.attention = "recovery"
    job.recoveries = [_repair_recovery_record(carried.path, carried.receipt)]
    jm.registry.add(job)
    try:
        r = client.post("/backups/restore", data={"backup": carried.name})
        assert r.status_code == 200
        assert (origin / "01 - Song.flac").read_bytes() == b"data"
        assert not carried.exists()
        assert "Restored the album" in r.text
        assert job.recoveries == []
        assert job.attention == ""
        assert job_persistence.load_one(job.id)["recoveries"] == []
    finally:
        _remove_job(job)


def test_restore_backup_stops_before_mutation_if_recovery_state_is_unavailable(
        client, tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    origin = tmp_path / "music" / "Artist" / "Album (2020)"
    origin.mkdir(parents=True)
    source = origin / "01 - Song.flac"
    source.write_bytes(b"data")
    carried = backup_mod.backup_album_dir(origin)
    assert carried is not None and carried.exists()
    monkeypatch.setattr(
        app_mod.job_mgr,
        "prepare_recovery_resolution",
        lambda *_args, **_kwargs: None,
        raising=False,
    )

    response = client.post("/backups/restore", data={"backup": carried.name})

    assert response.status_code == 200
    assert "saved recovery records could not be checked" in response.text
    assert carried.exists()
    assert not source.exists()


def test_restore_backup_does_not_claim_resolution_if_the_record_commit_fails(
        client, tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    origin = tmp_path / "music" / "Artist" / "Album (2020)"
    origin.mkdir(parents=True)
    source = origin / "01 - Song.flac"
    source.write_bytes(b"data")
    carried = backup_mod.backup_album_dir(origin)
    assert carried is not None and carried.exists()
    monkeypatch.setattr(
        app_mod.job_mgr,
        "resolve_recovery_resolution",
        lambda _plan: False,
    )

    response = client.post("/backups/restore", data={"backup": carried.name})

    assert response.status_code == 200
    assert source.read_bytes() == b"data"
    assert not carried.exists()
    assert "saved recovery status could not be updated" in response.text
    assert "Restored the album" not in response.text


def test_auth_loss_fires_the_hook_once_per_transition(monkeypatch):
    # Only the healthy→rejected edge should push a notification; the 401s
    # that follow are the same outage, and a recovery re-arms it.
    from qobuz_librarian.web import app as app_mod
    calls = []
    monkeypatch.setattr(app_mod.job_mgr, "fire_auth_lost_hook",
                        lambda: calls.append(1))
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    app_mod._on_auth_state(False)
    app_mod._on_auth_state(False)
    assert len(calls) == 1
    app_mod._on_auth_state(True)
    app_mod._on_auth_state(False)
    assert len(calls) == 2


def test_refresh_folds_into_parked_library_review(monkeypatch):
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Dummy", artist="Portishead",
                         detail="1994 · 16-bit/44.1 kHz · 11 tracks",
                         payload={"album_id": "al1"}, selected=False)
    parked.add_candidate(kind="album", title="Third", artist="Portishead",
                         detail="2008 · 24-bit/44.1 kHz · 10 tracks",
                         payload={"album_id": "al2"}, selected=False)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.set_selected(parked.candidates[0]["cid"], True)
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="Dummy", artist="Portishead",
                       detail="1994 · 16-bit/44.1 kHz · 11 tracks",
                       payload={"album_id": "al1"}, selected=False)
    scan.add_candidate(kind="album", title="Roseland NYC Live",
                       artist="Portishead",
                       detail="1998 · 16-bit/44.1 kHz · 11 tracks",
                       payload={"album_id": "al3"}, selected=False)
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        assert scan.status == jm.JobStatus.DONE
        assert scan.candidates == []
        assert "Folded 1 new find" in (scan.summary or "")
        ids = [c["payload"]["album_id"] for c in parked.candidates]
        assert ids == ["al1", "al2", "al3"]
        ticked = [c["payload"]["album_id"]
                  for c in parked.candidates if c.get("selected")]
        assert ticked == ["al1"]
        assert parked.status == jm.JobStatus.AWAITING_REVIEW
        library_reviews = [j for j in jm.registry.awaiting_review()
                           if j.execute_kind == "library"]
        assert library_reviews == [parked]
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_fold_swaps_candidate_class_and_keeps_the_tick(monkeypatch):
    """An album that changed on disk while the review sat parked (missing →
    partially added, or a gapped album deleted by hand) must swap to the fresh
    candidate class instead of being silently swallowed as a duplicate key —
    and the user's tick must survive the swap."""
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="The White EP", artist="Agalloch",
                         detail="2019 · fully missing · 8 tracks",
                         payload={"album_id": "wx1"}, selected=True)
    parked.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                         detail="gap-fill: 3 of 10 tracks missing",
                         payload={"album_id": "ax1", "gap_fill": True},
                         selected=False)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    # The White EP appeared on disk with some tracks → now a gap candidate;
    # Ashes was deleted by hand → now fully missing.
    scan.add_candidate(kind="album", title="The White EP", artist="Agalloch",
                       detail="gap-fill: 5 of 8 tracks missing",
                       payload={"album_id": "wx1", "gap_fill": True},
                       selected=False)
    scan.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                       detail="2005 · fully missing · 10 tracks",
                       payload={"album_id": "ax1"}, selected=False)
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        from qobuz_librarian.web import flows
        by_id = {c["payload"]["album_id"]: c for c in parked.candidates}
        assert flows.is_gap_candidate(by_id["wx1"])
        assert by_id["wx1"]["selected"] is True
        assert not flows.is_gap_candidate(by_id["ax1"])
        assert by_id["ax1"]["selected"] is False
        assert "Updated 2" in (scan.summary or "")
        assert "up to date" not in (scan.summary or "")
        cids = [c["cid"] for c in parked.candidates]
        assert len(set(cids)) == len(cids)
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_fold_carries_the_scan_honesty_caveat(monkeypatch):
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="A", artist="X",
                         payload={"album_id": "a1"}, selected=False)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan._unchecked_artists = 10
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        assert "10 artists couldn't be checked" in (scan.summary or "")
        assert "up to date" not in (scan.summary or "")
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_fold_does_not_resurrect_albums_dismissed_during_the_refresh(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="Dismissed Mid-Scan", artist="X",
                       payload={"album_id": "d1"}, selected=False)
    jm.registry.add(scan)
    # Hidden AFTER the scan built its candidate list — the stale-snapshot case.
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("X", "Dismissed Mid-Scan", "")])
    try:
        webapp._fold_into_parked_library_review(scan)

        assert parked.candidates == []
        assert "No new finds" in (scan.summary or "")
    finally:
        hidden_mod.restore(hidden_mod.SCOPE_MISSING, ["X"])
        _remove_job(parked)
        _remove_job(scan)


def test_fold_skips_a_review_approved_mid_refresh(monkeypatch):
    """Approve flips the review out of AWAITING_REVIEW between scan finish
    and fold — the refresh must keep its candidates and park normally instead
    of leaking finds into the executing job."""
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="A", artist="X",
                         payload={"album_id": "a1"})
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="B", artist="Y",
                       payload={"album_id": "b1"}, selected=False)
    jm.registry.add(scan)
    try:
        parked.status = jm.JobStatus.PENDING  # approve won the race
        webapp._fold_into_parked_library_review(scan)

        assert scan.status == jm.JobStatus.SCANNING
        assert len(scan.candidates) == 1
        assert len(parked.candidates) == 1
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_restore_refolds_into_the_parked_library_review():
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)
    library_scan_state.save_kind("missing", artists={
        "Agalloch": {"fingerprint": "fp", "candidates": [
            {"kind": "album", "title": "Ashes Against the Grain",
             "artist": "Agalloch", "detail": "2006 · fully missing",
             "payload": {"album_id": "ag1"}},
        ]},
    }, complete=True)
    try:
        added = flows.refold_restored_missing(["Agalloch"], [])
        assert added == 1
        assert parked.candidates[0]["payload"]["album_id"] == "ag1"
        assert parked.candidates[0]["selected"] is False
    finally:
        library_scan_state.save_kind("missing", artists={}, complete=False)
        _remove_job(parked)


def test_refresh_without_parked_review_parks_normally(monkeypatch):
    from qobuz_librarian.web import app as webapp

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="Dummy", artist="Portishead",
                       payload={"album_id": "al1"}, selected=False)
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        assert scan.status == jm.JobStatus.SCANNING
        assert len(scan.candidates) == 1
    finally:
        _remove_job(scan)


def test_post_baseline_library_scan_control_recedes(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.library.new_releases.is_baseline_complete",
        lambda: True)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 3, "message": ""})
    monkeypatch.setattr(webapp, "_last_scan_age", lambda: "2 days ago")
    monkeypatch.setattr(webapp, "_census_view", lambda: None)

    r = client.get("/library")

    assert r.status_code == 200
    assert 'class="ql-header-refresh"' in r.text
    assert "Scan for music added outside the app" in r.text
    assert ">Scan library</button>" not in r.text
    assert ">Check new releases</button>" in r.text


def test_pre_baseline_library_keeps_the_scan_hero(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.library.new_releases.is_baseline_complete",
        lambda: False)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 3, "message": ""})

    r = client.get("/library")

    assert r.status_code == 200
    assert ">Scan library</button>" in r.text
    assert 'class="ql-header-refresh"' not in r.text


def test_quality_shortfall_marks_history_until_the_job_is_opened(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    job = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    job.status = jm.JobStatus.DONE
    job.attention = "quality"
    job.finished_at = time.time()
    job_persistence.persist(job)

    r = client.get("/queue/history")
    assert r.status_code == 200
    assert "Below target quality" in r.text
    assert "data-attention-badge" in r.text

    r = client.get(f"/jobs/{job.id}")
    assert r.status_code == 200

    row = job_persistence.load_one(job.id)
    assert row["attention"] == ""
    r = client.get("/queue/history")
    assert "Below target quality" not in r.text
    assert "data-attention-badge" not in r.text


def test_note_quality_shortfall_flags_the_running_job():
    job = jm.Job(title="Dummy")
    jm._TLS.current_job = job
    try:
        jm.note_quality_shortfall()
    finally:
        jm._TLS.current_job = None
    assert job.attention == "quality"
    assert jm._queue_executor.on_quality_shortfall is jm.note_quality_shortfall


def test_new_release_approve_parks_the_unticked_remnant(client, monkeypatch):
    """A new release stays in the New Releases review until it's downloaded or
    dismissed: approving 1 of 2 must park the other as its own new-release
    review, not consume it (the persistent baseline already recorded it, so
    nothing else would ever offer it again)."""
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: True)
    job = jm.Job(title="New-release check")
    job.execute_kind = "new_releases"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Wanted", "X", payload={"album_id": "a1"},
                      selected=True)
    job.add_candidate("album", "Later", "X", payload={"album_id": "a2"},
                      selected=False)
    job._execute_fn = lambda j, chosen: None
    jm.registry.add(job)
    remnant = None
    try:
        r = client.post(f"/jobs/{job.id}/approve", data={"tab": ""},
                        follow_redirects=False)
        assert r.status_code == 303
        assert {c["title"] for c in job.candidates} == {"Wanted"}
        remnant = next(
            (j for j in jm.registry.awaiting_review()
             if j.id != job.id and j.execute_kind == "new_releases"), None)
        assert remnant is not None
        assert {c["title"] for c in remnant.candidates} == {"Later"}
    finally:
        _remove_job(job)
        if remnant is not None:
            _remove_job(remnant)


def test_failed_new_release_pick_returns_to_a_new_release_review():
    """A failed new-release download wasn't downloaded — it folds back into
    the parked New Releases review (or re-parks a fresh one), ticked, instead
    of being consumed with the dead job. It must never land in a Library
    review."""
    from qobuz_librarian.web import flows

    parked = jm.Job(title="New-release check")
    parked.execute_kind = "new_releases"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.add_candidate("album", "Untouched", "X",
                         payload={"album_id": "a1"}, selected=False)
    jm.registry.add(parked)
    library = jm.Job(title="Library scan")
    library.kind = "scan"
    library.execute_kind = "library"
    library.status = jm.JobStatus.AWAITING_REVIEW
    library.add_candidate("album", "LibThing", "Y", payload={"album_id": "L1"})
    jm.registry.add(library)
    try:
        flows._return_new_release_picks(
            [{"kind": "album", "title": "Failed NR", "artist": "X",
              "detail": "", "payload": {"album_id": "a2"}}])
        titles = {c["title"] for c in parked.candidates}
        assert titles == {"Untouched", "Failed NR"}
        failed = next(c for c in parked.candidates if c["title"] == "Failed NR")
        assert failed["selected"] is True
        assert {c["title"] for c in library.candidates} == {"LibThing"}
    finally:
        _remove_job(parked)
        _remove_job(library)


def test_partial_import_folds_an_instant_gap_fill_candidate():
    """An album that lands with some tracks failed becomes a Gap Fill
    candidate in the living Library review immediately — unticked, honest
    detail — instead of waiting for the next manual refresh."""
    from qobuz_librarian.web import flows

    parked = jm.Job(title="Library scan")
    parked.kind = "scan"
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.add_candidate("album", "Existing", "Y", payload={"album_id": "L1"})
    jm.registry.add(parked)
    try:
        flows._fold_partial_gap_fill(
            {"id": "a9", "title": "Short Album", "tracks_count": 10},
            "Artist", 3)
        gap = next(c for c in parked.candidates
                   if c["title"] == "Short Album")
        assert gap["selected"] is False
        assert (gap.get("payload") or {}).get("gap_fill") == 3
        assert "gap-fill: 3 missing of 10" in (gap.get("detail") or "")
    finally:
        _remove_job(parked)


def test_partial_new_release_download_returns_to_the_nr_review(monkeypatch):
    """A New Releases download that lands only partly isn't downloaded — the
    release goes back to the New Releases review (ticked, like a failure), and
    its remainder must NOT leak into the Library review as Gap Fill."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked_nr = _inject_job(jm.JobStatus.AWAITING_REVIEW, "New-release check")
    parked_nr.execute_kind = "new_releases"
    parked_lib = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked_lib.execute_kind = "library"
    running = _inject_job(jm.JobStatus.RUNNING, "New-release check")
    running.execute_kind = "new_releases"
    running.add_candidate(kind="album", title="Fresh Drop", artist="Abigail",
                          payload={"album_id": "nr1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album",
                        lambda aid, _t: {"id": aid, "title": "Fresh Drop",
                                         "tracks_count": 10})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(process_mod, "process_album",
                        lambda *_a, **_k: {"imported": True, "n_ok": 7,
                                           "n_fail": 3, "result": "downloaded"})
    try:
        flows.execute_albums(running, chosen, "tok")
        titles = {c["title"] for c in parked_nr.candidates}
        assert "Fresh Drop" in titles
        back = next(c for c in parked_nr.candidates if c["title"] == "Fresh Drop")
        assert back["selected"] is True
        assert parked_lib.candidates == []
    finally:
        _remove_job(parked_nr)
        _remove_job(parked_lib)
        _remove_job(running)


def test_dismiss_honours_a_tick_saved_during_the_store_write(monkeypatch):
    """dismiss_albums writes the durable store outside the job lock; a tick
    landing in that window was promised "keep the ticked ones". The row must
    survive AND its just-written dismissal must be taken back out of the
    store."""
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.web import flows

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Kept", artist="Abigail",
                      payload={"album_id": "k1", "year": 2020}, selected=False)
    cand = job.candidates[0]
    restored = []

    def hide_and_race(scope, specs):
        # The user's tick lands while the store write is in flight.
        cand["selected"] = True
        return len(list(specs))

    monkeypatch.setattr(hidden_mod, "hide", hide_and_race)
    monkeypatch.setattr(hidden_mod, "restore_albums",
                        lambda scope, fps: restored.extend(fps))
    try:
        n = flows.dismiss_albums(job, "Abigail")
        assert n == 0
        assert [c["title"] for c in job.candidates] == ["Kept"]
        assert job.candidates[0]["selected"] is True
        assert restored == [hidden_mod.album_fingerprint("Abigail", "Kept")]

        # A route can pass its opening status check and then lose a race to
        # approval before the off-thread store write begins. The flow itself
        # must reject that stale action instead of hiding rows from a run that
        # has already claimed its review.
        cand["selected"] = False
        job.status = jm.JobStatus.PENDING
        assert flows.dismiss_albums(job, "Abigail") is None
        assert cand["selected"] is False
    finally:
        _remove_job(job)


def test_new_edition_download_folds_onto_an_identical_running_job():
    """"Get this edition too" deliberately skips the owned-album fold, but two
    identical new-edition submits are the same tap twice — the second folds
    onto the in-flight job instead of queueing a concurrent duplicate."""
    from qobuz_librarian.web import app as web_app

    running = _inject_job(jm.JobStatus.RUNNING, "Album — edition")
    running.album_id = "ALB9"
    running.execute_args = {"new_edition": True}
    other = _inject_job(jm.JobStatus.RUNNING, "Other — edition")
    other.album_id = "OTHER"
    other.execute_args = {"new_edition": True}
    try:
        assert web_app._duplicate_download_job("ALB9", "", True) is running
        assert web_app._duplicate_download_job("UNSEEN", "", True) is None
    finally:
        _remove_job(running)
        _remove_job(other)


def test_approve_rechecks_the_write_pause_after_awaits(client, monkeypatch):
    """set_mode('cli') can land between approve's opening gate and the enqueue
    (form parsing and disk probes await in between); the not-yet-approved
    review is invisible to the handoff's active-job check, so only a recheck
    right before consuming the review can see the pause."""
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    job = job_mgr.Job(title="Library migration")
    job.execute_kind = "migration"
    job.status = job_mgr.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda j, chosen: None
    job.add_candidate("album", "A", "Artist", payload={"id": 1})
    job.candidates[0]["selected"] = True
    job_mgr.registry.add(job)

    # Simulate losing the race: the opening gate already passed, then the
    # CLI handoff flipped the mode before the enqueue.
    monkeypatch.setattr(webapp, "_lock_busy_response", lambda req: None)
    monkeypatch.setattr(webapp, "_CLI_MODE", True)

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code in (200, 303, 503)
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert any(c.get("selected") for c in job.candidates)
