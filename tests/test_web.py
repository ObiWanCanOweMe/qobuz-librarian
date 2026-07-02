"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""
import asyncio
import concurrent.futures
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


def test_per_artist_rescan_supersedes_only_that_artists_parked_review():
    # Two artists each have a scan parked for review. Submitting a *different*
    # artist's scan of the same kind must leave both parked reviews untouched —
    # the dedup keys on artist+kind, not kind alone. Keying on kind alone would
    # silently throw away the first artist's un-reviewed candidates. Re-scanning
    # the *same* artist still supersedes that artist's now-stale review.
    from qobuz_librarian.web import app as app_mod

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
    from qobuz_librarian.web import app as app_mod
    _run_web_executors_inline(monkeypatch, app_mod)
    with _SameThreadASGIClient(app_mod.app) as c:
        c.get("/queue")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c


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
            ("/artist", {"artist": "Radiohead"}),
            ("/library", {}),
            ("/upgrade", {}),
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
        "candidates": [{
            "title": "Dummy",
            "artist": "Portishead",
            "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
            "payload": {"album_id": "up1"},
        }],
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
    from qobuz_librarian.web import jobs as job_mgr

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


def test_downsample_artist_route_does_not_start_separate_scan(client):
    r = client.post("/downsample/artist", data={"artist": "Portishead"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/downsample"
    assert jm.registry.awaiting_review() == []


def test_upgrade_artist_route_does_not_start_separate_scan(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.post("/upgrade/artist", data={"artist": "Portishead"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/upgrade"
    assert jm.registry.awaiting_review() == []


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
    assert "Scan once to refresh missing albums, Gap Fill candidates, upgrades, and downsample candidates." in r.text
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


def test_search_path_redirects_to_the_dashboard(client):
    # Search is on the dashboard; /search redirects there.
    r = client.get("/search", follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/"


def test_non_htmx_search_post_returns_to_dashboard(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [])

    r = client.post("/search", data={"q": "Paysage d'Hiver"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_artist_page_redirects_to_dashboard_artist_search(client):
    r = client.get("/artist", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/?kind=artist"


def test_artist_page_redirect_preserves_artist_query(client):
    r = client.get("/artist?artist=Paysage%20d%27Hiver",
                   follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/?kind=artist&q=Paysage+d%27Hiver"


def test_queue_empty_state_has_clear_actions(client):
    r = client.get("/queue")

    assert r.status_code == 200
    assert "Queue is empty." in r.text
    assert "Downloads, scans, and reviews appear here" in r.text
    assert ">Search Qobuz</a>" in r.text
    assert ">View history</a>" in r.text


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
        assert 'id="queue-review-heading"' in r.text and "Needs review" in r.text
        assert 'id="queue-active-heading"' in r.text and "Running now" in r.text
        assert 'id="queue-waiting-heading"' in r.text and "Waiting" in r.text
        assert ">Clear queue and reviews</button>" in r.text
        assert "Queued jobs are removed, reviews are discarded" in r.text
        assert "Starts automatically after the current job finishes." in r.text
        assert "1 candidate found." in r.text
        assert "1 album can be downsampled." in r.text
        assert "Migration preview ready." in r.text
        assert "hi-res album to review" not in r.text
        assert "album folder to review" not in r.text
        assert "Waiting for “Running scan”" not in r.text
        assert ">Remove</button>" in r.text
        assert 'aria-label="Remove from queue"' in r.text
        assert ">Cancel</button>" in r.text
        assert 'data-confirm="Cancel this job? It will stop after the current safe step."' in r.text
        assert 'data-confirm="Remove this waiting job from the queue?"' in r.text
        assert ">Discard</button>" in r.text
        assert "Its 1 result are" not in r.text
        assert "No files will change. Run the scan again to see these results later." in r.text
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
    assert ">Back to queue</a>" in r.text


def test_history_retry_only_shows_for_live_failed_download(client, monkeypatch):
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
        assert f'action="/jobs/{archived.id}/retry"' not in r.text
    finally:
        _remove_job(live)


def test_archived_job_page_hides_live_only_actions(client, monkeypatch):
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
    single.single = {"dir": "/music/Portishead/Dummy", "track_id": "t1"}
    single.finished_at = time.time()
    job_persistence.persist(single)

    r = client.get(f"/jobs/{failed.id}")
    assert r.status_code == 200
    assert "This job is archived." in r.text
    assert ">Retry</button>" not in r.text

    r = client.get(f"/jobs/{single.id}")
    assert r.status_code == 200
    assert "Undo (removes track)" not in r.text


def test_hidden_empty_state_points_back_to_library(client):
    r = client.get("/library/hidden")

    assert r.status_code == 200
    assert "No dismissed albums or Gap Fill." in r.text
    assert ">Go to Library</a>" in r.text


def test_repair_history_empty_state_points_back_to_repair(client):
    r = client.get("/repair/history")

    assert r.status_code == 200
    assert "Nothing repaired yet." in r.text
    assert ">Back to repair</a>" in r.text


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
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=True)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994", "gap_fill": 2}, selected=True)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007", "gap_fill": 1}, selected=False)
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
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/Third") if alb.get("id") == "q123"
        else None)

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
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/x"))
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
        assert r.headers["location"] == "/login"
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
